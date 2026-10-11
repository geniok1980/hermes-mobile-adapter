"""Клиент протокола Hermes Mobile Adapter для Python (asyncio + websockets).

Зачем: боты, скрипты, серверные интеграции и автотесты — всё, где WebSocket нужен вне
мобильного приложения. Протокол тот же, что у TypeScript-SDK (``docs/protocol.md``).

Пример:

    client = HermesMobileClient("ws://127.0.0.1:8787/ws", token="…",
                               device=Device(id="bot-1", name="Бот"))
    await client.connect()
    await client.send_text("привет, Hermes")
    answer = await client.wait_message(timeout=120)      # собрать один полный ответ
    print("".join(answer))
    await client.aclose()

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

# Типы кадров (совпадают с adapter/hermes_mobile/protocol.py).
C_HELLO, C_TEXT, C_AUDIO, C_TYPING, C_PING, C_ACK = "hello", "text", "audio", "typing", "ping", "ack"
S_READY, S_TYPING, S_MESSAGE, S_AVATAR, S_ERROR, S_PONG = "ready", "typing", "message", "avatar", "error", "pong"


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
    id: str
    text: str
    index: int = 0
    total: int = 1
    final: bool = True


class HermesMobileClient:
    """Клиент адаптера: подключение, отправка, приём ответов и событий аватара."""

    def __init__(
        self,
        url: str,
        token: str,
        device: Optional[Device] = None,
        *,
        user: Optional[Dict[str, str]] = None,
        on_message: Optional[Callable[[AgentMessage], Union[None, Awaitable[None]]]] = None,
        on_avatar: Optional[Callable[[str], Union[None, Awaitable[None]]]] = None,
        on_typing: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
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
        self._on_avatar = on_avatar
        self._on_typing = on_typing
        self._on_error = on_error
        self._on_ready = on_ready
        self._socket: Any = None
        self._reader: Optional[asyncio.Task] = None
        self._inbox: asyncio.Queue = asyncio.Queue()

    # ── жизненный цикл ───────────────────────────────────────────────────────

    async def connect(self) -> Ready:
        """Открывает соединение, проходит ``hello`` и возвращает кадр ``ready``."""
        separator = "&" if "?" in self.url else "?"
        socket = await websockets.connect(
            f"{self.url}{separator}token={self.token}",
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

    # ── отправка ─────────────────────────────────────────────────────────────

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

    async def wait_avatar(self, timeout: float = 30.0) -> str:
        """Ждёт кадр состояния аватара (``idle``/``thinking``/``speaking``) — удобно в тестах."""
        while True:
            frame = await asyncio.wait_for(self._control.get(), timeout=timeout)
            if frame.get("t") == S_AVATAR:
                return str(frame.get("state") or "idle")

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
                    )
                    await self._inbox.put(message)
                    if self._on_message:
                        await _maybe_await(self._on_message(message))
                elif kind == S_AVATAR:
                    await self._control.put(frame)
                    if self._on_avatar:
                        await _maybe_await(self._on_avatar(str(frame.get("state") or "idle")))
                elif kind == S_TYPING:
                    await self._control.put(frame)
                    if self._on_typing:
                        await _maybe_await(self._on_typing())
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

    @property
    def _control(self) -> asyncio.Queue:
        """Очередь служебных кадров (avatar/typing) — отдельно от ответов агента."""
        if not hasattr(self, "_control_queue"):
            self._control_queue: asyncio.Queue = asyncio.Queue()
        return self._control_queue


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        await value
