"""Клиент протокола Hermes Mobile Adapter для Python (asyncio + websockets).

Зачем: боты, скрипты, серверные интеграции и автотесты — всё, где WebSocket нужен вне
мобильного приложения. Протокол тот же, что у TypeScript-SDK (``site-docs/protocol/index.md``).

Что умеет: текст, голос, карточки-вопросы (одобрение команды / clarify / слэш-команда),
медиа в обе стороны (фото, документы, голос — с автоматической нарезкой по 3 МБ),
приём правок сообщений (``edit``) и остановки индикатора «печатает» (``typing.stop``).

Пример — обычный диалог:

    client = HermesMobileClient("ws://127.0.0.1:8787/ws", token="…",
                               device=Device(id="bot-1", name="Бот"))
    await client.connect()
    await client.send_text("привет, Hermes")
    answer = await client.wait_message(timeout=120)      # собрать один полный ответ
    print("".join(answer))
    await client.aclose()

Пример — карточка-вопрос и медиа:

    def on_prompt(prompt):                    # пришла карточка с вариантами
        print(prompt.kind, prompt.text, [o.id for o in prompt.options])
        # отвечаем из того же процесса:
        asyncio.create_task(client.answer_prompt(prompt.id, option_id="once"))

    def on_media(message):                     # агент прислал файл
        path = client.save_media(message, "~/загрузки")   # -> Path
        print("сохранён:", path)

    client = HermesMobileClient(url, token, on_prompt=on_prompt, on_media=on_media)
    await client.connect()
    await client.send_image("~/скрин.png", caption="вот ошибка")   # путь → base64
    await client.send_document(b"%PDF-1.4 …", filename="отчёт.pdf")
    await client.send_voice("~/answer.ogg")

Требуется пакет ``websockets`` (>= 12) — в окружении Hermes он уже есть.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

try:  # websockets >= 12 (asyncio-реализация)
    import websockets
except ImportError as exc:  # pragma: no cover
    raise ImportError("Нужен пакет websockets: pip install websockets>=12") from exc

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
CLOSE_POLICY_VIOLATION = 1008

# Нарезка исходящих медиафайлов: кусок base64 ≈ 4 МБ — с запасом влезает в WS-кадр.
MEDIA_CHUNK_BYTES = 3 * 1024 * 1024

# Типы кадров (совпадают с adapter/hermes_mobile/protocol.py).
C_HELLO, C_TEXT, C_AUDIO, C_TYPING, C_PING, C_ACK = "hello", "text", "audio", "typing", "ping", "ack"
C_PROMPT, C_MEDIA = "prompt", "media"
S_READY, S_TYPING, S_MESSAGE, S_AVATAR, S_ERROR, S_PONG = "ready", "typing", "message", "avatar", "error", "pong"
S_PROMPT, S_PROMPT_DONE, S_MEDIA = "prompt", "prompt_done", "media"

# Типы карточек и допустимые медиа (совпадают с protocol.py).
INBOUND_MEDIA_KINDS = ("image", "video", "document", "audio")
CLARIFY_OTHER = "other"


class HermesProtocolError(RuntimeError):
    """Ошибка протокола, пришедшая от адаптера (или отказ в доступе)."""

    def __init__(self, code: str, message: str, *, fatal: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fatal = fatal


@dataclass
class Device:
    """Устройство/приложение, как оно будет видно в Hermes (pairing, allowlist, cron)."""

    id: str
    name: str = ""
    platform: str = "python"
    app_version: str = "1.0.0"


@dataclass
class Ready:
    v: int
    session: str
    device: Dict[str, Any] = field(default_factory=dict)
    avatar_url: str = ""
    capabilities: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentMessage:
    """Кусок ответа агента. ``edit=True`` — это правка уже показанного сообщения по ``id``."""

    id: str
    text: str
    index: int = 0
    total: int = 1
    final: bool = True
    edit: bool = False


@dataclass
class PromptOption:
    """Вариант ответа на карточку (кнопка)."""

    id: str
    label: str = ""
    style: str = ""


@dataclass
class Prompt:
    """Карточка-вопрос от агента: одобрение команды, clarify или подтверждение слэш-команды."""

    id: str
    kind: str
    text: str = ""
    options: List[PromptOption] = field(default_factory=list)
    session: str = ""
    timeout_seconds: int = 0

    def option_ids(self) -> List[str]:
        return [option.id for option in self.options]


@dataclass
class PromptDone:
    """Карточка закрыта: ``option_id`` — применённый вариант, ``expired=True`` — истекла."""

    id: str
    option_id: Optional[str] = None
    expired: bool = False


@dataclass
class MediaMessage:
    """Медиа от агента. ``data`` — уже собранные байты целого файла (все части вместе)."""

    id: str
    kind: str
    data: bytes = b""
    mime: str = ""
    filename: str = ""
    caption: str = ""
    part: int = 0
    parts: int = 1
    final: bool = True
    size: int = 0
    url: str = ""
    reply_to: str = ""

    def data_url(self) -> str:
        """Data-URL для показа картинки в браузере/WebView без сохранения на диск."""
        mime = self.mime or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(self.data).decode()}"


class HermesMobileClient:
    """Клиент адаптера: подключение, отправка, приём ответов, карточек и медиа."""

    def __init__(
        self,
        url: str,
        token: str,
        device: Optional[Device] = None,
        *,
        user: Optional[Dict[str, str]] = None,
        on_message: Optional[Callable[[AgentMessage], Union[None, Awaitable[None]]]] = None,
        on_message_edit: Optional[Callable[[AgentMessage], Union[None, Awaitable[None]]]] = None,
        on_avatar: Optional[Callable[[str], Union[None, Awaitable[None]]]] = None,
        on_typing: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
        on_typing_stop: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
        on_prompt: Optional[Callable[[Prompt], Union[None, Awaitable[None]]]] = None,
        on_prompt_done: Optional[Callable[[PromptDone], Union[None, Awaitable[None]]]] = None,
        on_media: Optional[Callable[[MediaMessage], Union[None, Awaitable[None]]]] = None,
        on_error: Optional[Callable[[HermesProtocolError], Union[None, Awaitable[None]]]] = None,
        on_ready: Optional[Callable[[Ready], Union[None, Awaitable[None]]]] = None,
        open_timeout: float = 15.0,
        request_timeout: float = 15.0,
    ):
        if not token:
            raise ValueError("Нужен токен приложения (MOBILE_TOKEN на стороне агента)")
        self.url = url
        self.token = token
        self.device = device or Device(id=f"py-{uuid.uuid4().hex[:8]}", name="Python-клиент")
        self.user = user or {}
        self.open_timeout = open_timeout
        self.request_timeout = request_timeout
        self.ready: Optional[Ready] = None

        self._on_message = on_message
        self._on_message_edit = on_message_edit
        self._on_avatar = on_avatar
        self._on_typing = on_typing
        self._on_typing_stop = on_typing_stop
        self._on_prompt = on_prompt
        self._on_prompt_done = on_prompt_done
        self._on_media = on_media
        self._on_error = on_error
        self._on_ready = on_ready
        self._socket: Any = None
        self._reader: Optional[asyncio.Task] = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._prompts: asyncio.Queue = asyncio.Queue()
        self._media_in: asyncio.Queue = asyncio.Queue()
        # Сборка многочастных медиа от агента: id → {parts, chunks:{index: bytes}, meta}.
        self._media_buffers: Dict[str, Dict[str, Any]] = {}

    # ── жизненный цикл ───────────────────────────────────────────────────────

    async def connect(self) -> Ready:
        """Открывает соединение, проходит ``hello`` и возвращает кадр ``ready``."""
        # Токен не уходит в URL (query-строки видны в логах прокси) — только в кадре hello.
        socket = await websockets.connect(
            self.url,
            max_size=MAX_FRAME_BYTES,
            open_timeout=self.open_timeout,
        )
        self._socket = socket
        await self._send(
            {
                "t": C_HELLO,
                "v": PROTOCOL_VERSION,
                "token": self.token,
                "device": {
                    "id": self.device.id,
                    "name": self.device.name,
                    "platform": self.device.platform,
                    "app_version": self.device.app_version,
                },
                "user": self.user,
            }
        )
        frame = await asyncio.wait_for(self._recv(), timeout=self.request_timeout)
        kind = str(frame.get("t") or "")
        if kind == S_ERROR:
            raise HermesProtocolError(
                str(frame.get("code") or "error"), str(frame.get("message") or ""), fatal=bool(frame.get("fatal"))
            )
        if kind != S_READY:
            raise HermesProtocolError("bad_frame", f"Ожидался ready, получено {kind!r}", fatal=True)
        self.ready = Ready(
            v=int(frame.get("v") or 0),
            session=str(frame.get("session") or ""),
            device=dict(frame.get("device") or {}),
            avatar_url=str(frame.get("avatar_url") or ""),
            capabilities=dict(frame.get("capabilities") or {}),
        )
        self._reader = asyncio.create_task(self._read_loop())
        if self._on_ready:
            await _maybe_await(self._on_ready(self.ready))
        return self.ready

    async def aclose(self) -> None:
        """Закрывает соединение и останавливает цикл чтения."""
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
            self._socket = None

    async def __aenter__(self) -> "HermesMobileClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # ── отправка текста/голоса ───────────────────────────────────────────────

    async def send_text(self, text: str, message_id: Optional[str] = None) -> str:
        """Сообщение агенту. Повтор с тем же ``message_id`` адаптер проигнорирует."""
        if not str(text).strip():
            raise ValueError("Пустое сообщение")
        mid = message_id or uuid.uuid4().hex
        await self._send({"t": C_TEXT, "id": mid, "text": text})
        return mid

    async def send_audio(
        self,
        audio: Union[bytes, bytearray, str, Path],
        fmt: str = "wav",
        transcript: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> str:
        """Голосовое сообщение: WAV/OGG в base64 (можно передать путь к файлу)."""
        if isinstance(audio, (str, Path)):
            payload = Path(audio).read_bytes()
        else:
            payload = bytes(audio)
        if fmt not in ("wav", "ogg"):
            raise ValueError("Поддерживаются форматы wav и ogg")
        mid = message_id or uuid.uuid4().hex
        await self._send(
            {"t": C_AUDIO, "id": mid, "format": fmt, "data": base64.b64encode(payload).decode(), "text": transcript}
        )
        return mid

    async def send_typing(self) -> None:
        await self._send({"t": C_TYPING})

    async def ping(self, ts: Optional[int] = None) -> None:
        await self._send({"t": C_PING, "ts": ts or 0})

    async def acknowledge(self, message_id: str) -> None:
        await self._send({"t": C_ACK, "id": message_id})

    # ── карточки-вопросы ─────────────────────────────────────────────────────

    async def answer_prompt(self, prompt_id: str, option_id: Optional[str] = None, text: Optional[str] = None) -> None:
        """Ответ на карточку-вопрос.

        Для ``exec_approval`` — ``option_id`` один из ``once|session|always|deny``;
        для ``slash_confirm`` — ``once|always|cancel``; для ``clarify`` — ``c0..cN``
        или ``other`` с текстом в ``text``. Незнакомое значение адаптер заменит на
        безопасное (``deny`` / ``cancel``), но лучше слать корректное.
        """
        if not str(prompt_id).strip():
            raise ValueError("Нужен prompt_id карточки")
        if not (option_id and str(option_id).strip()) and not (text and str(text).strip()):
            raise ValueError("Укажите option_id или text для ответа на карточку")
        frame: Dict[str, Any] = {"t": C_PROMPT, "prompt_id": str(prompt_id)}
        if option_id:
            frame["option_id"] = str(option_id).strip().lower()
        if text:
            frame["text"] = str(text)
        await self._send(frame)

    # ── отправка медиа ───────────────────────────────────────────────────────

    async def send_media(
        self,
        kind: str,
        payload: Union[bytes, bytearray, str, Path, None] = None,
        *,
        mime: Optional[str] = None,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        message_id: Optional[str] = None,
        chunk_bytes: int = MEDIA_CHUNK_BYTES,
    ) -> str:
        """Отправляет медиа агенту, автоматически нарезая файл по ``chunk_bytes`` (3 МБ).

        ``payload`` — байты или путь к файлу (``str``/``Path``). Куски идут кадрами
        ``media`` с общим ``id`` и полями ``part``/``parts`` — адаптер собирает их в
        один файл, поэтому большие файлы не упираются в лимит кадра.
        """
        kind = str(kind or "").strip().lower()
        if kind not in INBOUND_MEDIA_KINDS:
            raise ValueError(f"Недопустимый тип медиа {kind!r} (можно: {', '.join(INBOUND_MEDIA_KINDS)})")
        data, name = _read_media_source(payload, filename)
        if not data:
            raise ValueError("Пустой файл — отправлять нечего")
        mid = message_id or uuid.uuid4().hex
        mime = _guess_mime(mime, name or filename or "")
        total = max(1, (len(data) + chunk_bytes - 1) // chunk_bytes)
        for index in range(total):
            chunk = data[index * chunk_bytes : (index + 1) * chunk_bytes]
            frame: Dict[str, Any] = {
                "t": C_MEDIA,
                "id": mid,
                "kind": kind,
                "mime": mime,
                "data": base64.b64encode(chunk).decode(),
                "part": index,
                "parts": total,
            }
            if filename or name:
                frame["filename"] = filename or name
            if caption:
                frame["caption"] = caption
            await self._send(frame)
        return mid

    async def send_image(self, payload: Union[bytes, str, Path], caption: Optional[str] = None, **kwargs: Any) -> str:
        """Фото: путь к файлу или байты (``kind=image``)."""
        return await self.send_media("image", payload, caption=caption, **kwargs)

    async def send_document(self, payload: Union[bytes, str, Path], caption: Optional[str] = None, **kwargs: Any) -> str:
        """Файл (PDF, zip…): путь или байты (``kind=document``)."""
        return await self.send_media("document", payload, caption=caption, **kwargs)

    async def send_voice(self, payload: Union[bytes, str, Path], caption: Optional[str] = None, **kwargs: Any) -> str:
        """Голосовое от приложения (``kind=audio``; ogg/mp3/m4a/wav)."""
        return await self.send_media("audio", payload, caption=caption, **kwargs)

    # ── приём ────────────────────────────────────────────────────────────────

    async def wait_message(self, timeout: float = 120.0) -> List[str]:
        """Ждёт один полный ответ агента и возвращает его части (для ботов и тестов)."""
        deadline = asyncio.get_running_loop().time() + timeout
        parts: List[str] = []
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("Ответ агента не пришёл")
            message: AgentMessage = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            parts.append(message.text)
            if message.final:
                return parts

    async def wait_prompt(self, timeout: float = 30.0) -> Prompt:
        """Ждёт карточку-вопрос от агента — удобно в тестах и ботах."""
        return await asyncio.wait_for(self._prompts.get(), timeout=timeout)

    async def wait_media(self, timeout: float = 30.0) -> MediaMessage:
        """Ждёт полностью собранный медиафайл от агента (все части вместе)."""
        return await asyncio.wait_for(self._media_in.get(), timeout=timeout)

    async def wait_avatar(self, timeout: float = 30.0) -> str:
        """Ждёт кадр состояния аватара (``idle``/``thinking``/``speaking``) — удобно в тестах."""
        while True:
            frame = await asyncio.wait_for(self._control.get(), timeout=timeout)
            if frame.get("t") == S_AVATAR:
                return str(frame.get("state") or "idle")

    def save_media(self, message: MediaMessage, directory: Union[str, Path]) -> Path:
        """Сохраняет принятый медиафайл на диск и возвращает путь (``~`` раскрывается)."""
        directory = Path(directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        name = _safe_filename(message.filename or f"{message.id}{_suffix_for(message.mime)}")
        path = directory / name
        path.write_bytes(message.data)
        return path

    # ── внутреннее ───────────────────────────────────────────────────────────

    async def _send(self, frame: Dict[str, Any]) -> None:
        if self._socket is None:
            raise RuntimeError("Соединение не открыто: вызовите connect()")
        await self._socket.send(json.dumps(frame, ensure_ascii=False))

    async def _recv(self) -> Dict[str, Any]:
        raw = await self._socket.recv()
        return json.loads(raw)

    async def _read_loop(self) -> None:
        """Читает кадры и раскладывает их по колбэкам/очередям."""
        try:
            while True:
                frame = await self._recv()
                kind = str(frame.get("t") or "")
                if kind == S_MESSAGE:
                    chunk = frame.get("chunk") or {}
                    message = AgentMessage(
                        id=str(frame.get("id") or ""),
                        text=str(frame.get("text") or ""),
                        index=int(chunk.get("index") or 0),
                        total=int(chunk.get("total") or 1),
                        final=bool(frame.get("final")),
                        edit=bool(frame.get("edit")),
                    )
                    if message.edit:
                        if self._on_message_edit:
                            await _maybe_await(self._on_message_edit(message))
                    else:
                        await self._inbox.put(message)
                        if self._on_message:
                            await _maybe_await(self._on_message(message))
                elif kind == S_AVATAR:
                    await self._control.put(frame)
                    if self._on_avatar:
                        await _maybe_await(self._on_avatar(str(frame.get("state") or "idle")))
                elif kind == S_TYPING:
                    await self._control.put(frame)
                    if frame.get("stop"):
                        if self._on_typing_stop:
                            await _maybe_await(self._on_typing_stop())
                    elif self._on_typing:
                        await _maybe_await(self._on_typing())
                elif kind == S_PROMPT:
                    prompt = _parse_prompt(frame)
                    await self._prompts.put(prompt)
                    if self._on_prompt:
                        await _maybe_await(self._on_prompt(prompt))
                elif kind == S_PROMPT_DONE:
                    done = PromptDone(
                        id=str(frame.get("id") or ""),
                        option_id=(str(frame["option_id"]) if frame.get("option_id") else None),
                        expired=bool(frame.get("expired")),
                    )
                    if self._on_prompt_done:
                        await _maybe_await(self._on_prompt_done(done))
                elif kind == S_MEDIA:
                    message = self._accumulate_media(frame)
                    if message is not None:
                        await self._media_in.put(message)
                        if self._on_media:
                            await _maybe_await(self._on_media(message))
                elif kind == S_ERROR:
                    error = HermesProtocolError(
                        str(frame.get("code") or "error"),
                        str(frame.get("message") or ""),
                        fatal=bool(frame.get("fatal")),
                    )
                    if self._on_error:
                        await _maybe_await(self._on_error(error))
                    else:
                        logger.warning("Адаптер вернул ошибку: %s", error)
                # pong и неизвестные кадры игнорируем — так требует протокол
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # соединение закрылось
            if self._on_error and getattr(exc, "code", None) == CLOSE_POLICY_VIOLATION:
                await _maybe_await(
                    self._on_error(HermesProtocolError("auth_failed", "Шлюз закрыл соединение (код 1008)", fatal=True))
                )

    def _accumulate_media(self, frame: Dict[str, Any]) -> Optional[MediaMessage]:
        """Собирает части многочастного медиа от агента в целый файл.

        Возвращает ``MediaMessage`` только когда пришли все части (для одночастных —
        сразу). Промежуточные куски наверх не отдаём: иначе ``save_media`` сохранил бы
        обрывок.
        """
        mid = str(frame.get("id") or "")
        parts = int(frame.get("parts") or 1)
        part = int(frame.get("part") or 0)
        chunk = base64.b64decode(frame["data"]) if frame.get("data") else b""
        if parts <= 1:
            return self._build_media_message(frame, chunk, part, parts, b"")
        buffer = self._media_buffers.setdefault(mid, {"parts": parts, "chunks": {}, "meta": frame})
        buffer["chunks"][part] = chunk
        buffer["meta"] = frame  # последний кадр несёт url/reply_to/caption
        if len(buffer["chunks"]) < parts:
            return None
        payload = b"".join(buffer["chunks"][i] for i in sorted(buffer["chunks"]))
        meta = buffer["meta"]
        self._media_buffers.pop(mid, None)
        return self._build_media_message(meta, payload, part, parts, b"")

    def _build_media_message(
        self, frame: Dict[str, Any], payload: bytes, part: int, parts: int, _extra: bytes
    ) -> MediaMessage:
        return MediaMessage(
            id=str(frame.get("id") or ""),
            kind=str(frame.get("kind") or ""),
            data=payload,
            mime=str(frame.get("mime") or ""),
            filename=str(frame.get("filename") or ""),
            caption=str(frame.get("caption") or ""),
            part=part,
            parts=parts,
            final=bool(frame.get("final")),
            size=int(frame.get("size") or len(payload)),
            url=str(frame.get("url") or ""),
            reply_to=str(frame.get("reply_to") or ""),
        )

    @property
    def _control(self) -> asyncio.Queue:
        """Очередь служебных кадров (avatar/typing) — отдельно от ответов агента."""
        if not hasattr(self, "_control_queue"):
            self._control_queue: asyncio.Queue = asyncio.Queue()
        return self._control_queue


# ── модульные помощники ──────────────────────────────────────────────────────

def _parse_prompt(frame: Dict[str, Any]) -> Prompt:
    options = [
        PromptOption(id=str(item.get("id") or ""), label=str(item.get("label") or ""), style=str(item.get("style") or ""))
        for item in (frame.get("options") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    return Prompt(
        id=str(frame.get("id") or ""),
        kind=str(frame.get("kind") or ""),
        text=str(frame.get("text") or ""),
        options=options,
        session=str(frame.get("session") or ""),
        timeout_seconds=int(frame.get("timeout_seconds") or 0),
    )


def _read_media_source(payload: Any, filename: Optional[str]) -> tuple:
    """Приводит источник медиа к ``(байты, имя файла)``: путь читаем с диска."""
    if payload is None:
        return b"", filename or ""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload), filename or ""
    if isinstance(payload, (str, Path)):
        path = Path(payload).expanduser()
        if path.is_file():
            return path.read_bytes(), filename or path.name
        # Не путь — значит это уже base64-строка (например, из WebView/камеры).
        try:
            return base64.b64decode(str(payload), validate=True), filename or ""
        except Exception as exc:
            raise ValueError(f"Не файл и не base64: {payload!r}") from exc
    raise ValueError(f"Неподдерживаемый источник медиа: {type(payload).__name__}")


def _guess_mime(mime: Optional[str], filename: str) -> str:
    """MIME: явный, иначе по расширению, иначе общий поток байтов."""
    if mime:
        return str(mime)[:120]
    table = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".heic": "image/heic", ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
        ".wav": "audio/wav", ".opus": "audio/opus", ".pdf": "application/pdf", ".txt": "text/plain",
        ".zip": "application/zip",
    }
    low = str(filename or "").lower()
    for ext, value in table.items():
        if low.endswith(ext):
            return value
    return "application/octet-stream"


def _suffix_for(mime: str) -> str:
    table = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "image/heic": ".heic", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/wav": ".wav",
        "application/pdf": ".pdf", "text/plain": ".txt",
    }
    return table.get(str(mime or "").lower(), ".bin")


def _safe_filename(name: str) -> str:
    """Оставляет от имени только безопасные символы (никаких ``../`` из сети)."""
    clean = Path(str(name or "")).name
    return "".join(ch for ch in clean if ch.isalnum() or ch in "._- ") or "media.bin"


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        await value
