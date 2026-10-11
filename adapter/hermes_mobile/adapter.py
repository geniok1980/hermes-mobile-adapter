"""Платформенный адаптер **hermes-mobile** — WebSocket-мост между мобильным
приложением и Hermes Agent.

Как это работает
----------------

Адаптер поднимает внутри шлюза WebSocket-сервер (по умолчанию ``0.0.0.0:8787/ws``).
Любое мобильное приложение подключается к нему, проходит авторизацию по
токену, и дальше:

* **клиент → агент**: ``text`` (сообщение), ``audio`` (голос в WAV/OGG), ``typing``,
  ``ping``. Каждое сообщение превращается в ``MessageEvent`` и уходит в агента через
  ``self.handle_message(event)`` — то есть агент, история, инструменты и сессии те же,
  что и в остальных платформах.
* **агент → клиент**: текст ответа нарезается на куски (``message`` + ``chunk``),
  приложение получает ``typing`` и события аватара (``avatar: thinking/speaking/idle``),
  чтобы отрисовать «наш» аватар во время ответа.
* **вне процесса агента** (cron, ``hermes send --to mobile:<устройство>``): адаптер
  умеет отправить сообщение, подключившись к уже работающему шлюзу как служебный
  клиент (``standalone_sender_fn``).

Конфигурация
------------

Обязательно: ``MOBILE_TOKEN`` — общий секрет приложения (``openssl rand -hex 32``).
Опционально: ``MOBILE_HOST`` (0.0.0.0), ``MOBILE_PORT`` (8787), ``MOBILE_PATH`` (/ws),
``MOBILE_AVATAR_URL``, ``MOBILE_HOME_DEVICE``, ``MOBILE_ALLOWED_USERS``,
``MOBILE_EXTRA_TOKENS``; то же самое можно задать в ``config.yaml`` ->
``gateway.platforms.mobile.extra`` (env имеет приоритет).

Безопасность: без валидного токена соединение закрывается с кодом 1008, сам токен в
логи никогда не попадает (``protocol.redact_token``); устройство можно дополнительно
ограничить белым списком ``MOBILE_ALLOWED_USERS``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote

try:  # обычная загрузка как пакет плагина
    from . import protocol as proto
except ImportError:  # pragma: no cover — загрузка адаптера как отдельного модуля
    import protocol as proto  # type: ignore

from gateway.config import Platform
from gateway.platforms._shared import (
    coerce_port,
    get_scoped_secret as _get_scoped_secret,
    seed_extra_from_env as _seed_extra_from_env,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, cancel_task

logger = logging.getLogger(__name__)

PLATFORM_NAME = "mobile"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787
DEFAULT_PATH = "/ws"
DEFAULT_HELLO_TIMEOUT = 15.0
DEFAULT_TYPING_IDLE_SECONDS = 3.0
CLOSE_UNAUTHORIZED = 1008  # policy violation — WebSocket-стандарт для отказа в доступе


@dataclass
class MobileDevice:
    """Подключённое устройство. ``chat_id`` — идентификатор сессии в терминах Hermes."""

    device_id: str
    chat_id: str
    name: str = ""
    platform: str = ""
    app_version: str = ""
    user_id: str = ""
    user_name: str = ""
    connected_at: float = 0.0
    last_seen: float = 0.0

    def display_name(self) -> str:
        return self.name or self.user_name or self.device_id


def _env_or_extra(extra: Dict[str, Any], env_key: str, extra_key: str, default: Any = None) -> Any:
    """Значение env имеет приоритет над ``config.yaml`` → ``extra``."""
    raw = _get_scoped_secret(env_key)
    if raw not in (None, ""):
        return raw
    value = extra.get(extra_key)
    return default if value in (None, "") else value


class HermesMobileAdapter(BasePlatformAdapter):
    """WebSocket-адаптер мобильного приложения (платформа hermes-mobile)."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = getattr(config, "extra", {}) or {}

        self.token = str(_env_or_extra(extra, "MOBILE_TOKEN", "token", "") or "").strip()
        raw_extra_tokens = _env_or_extra(extra, "MOBILE_EXTRA_TOKENS", "extra_tokens", "")
        if isinstance(raw_extra_tokens, (list, tuple, set)):
            self.extra_tokens = [str(t).strip() for t in raw_extra_tokens if str(t).strip()]
        else:
            self.extra_tokens = proto.split_tokens(raw_extra_tokens)
        self.host = str(_env_or_extra(extra, "MOBILE_HOST", "host", DEFAULT_HOST) or DEFAULT_HOST)
        self.port = coerce_port(_env_or_extra(extra, "MOBILE_PORT", "port", DEFAULT_PORT), DEFAULT_PORT)
        raw_path = str(_env_or_extra(extra, "MOBILE_PATH", "path", DEFAULT_PATH) or DEFAULT_PATH)
        self.path = "/" + raw_path.strip().strip("/")
        self.avatar_url = str(_env_or_extra(extra, "MOBILE_AVATAR_URL", "avatar_url", "") or "").strip()

        allowed = extra.get("allowed_users") or proto.split_tokens(
            _get_scoped_secret("MOBILE_ALLOWED_USERS", "")
        )
        self.allowed_users: List[str] = [str(a).strip() for a in allowed if str(a).strip()]
        self.home_device = str(extra.get("home_device") or _get_scoped_secret("MOBILE_HOME_DEVICE", "") or "").strip()

        max_len = extra.get("max_message_length")
        if max_len is None:
            with contextlib.suppress(Exception):
                from gateway.platform_registry import platform_registry
                max_len = platform_registry.get(PLATFORM_NAME).max_message_length
        self.max_message_length = int(max_len or proto.DEFAULT_CHUNK_LIMIT)

        self.hello_timeout = float(extra.get("hello_timeout", DEFAULT_HELLO_TIMEOUT) or DEFAULT_HELLO_TIMEOUT)
        self.typing_idle_seconds = float(extra.get("typing_idle_seconds", DEFAULT_TYPING_IDLE_SECONDS))

        self._server: Any = None
        self.listener_base: Optional[str] = None  # ws://host:port/path — публикуется в статусе шлюза
        self._sessions: Dict[str, MobileDevice] = {}          # chat_id -> устройство
        self._connections: Dict[str, Any] = {}                # chat_id -> websocket
        self._lock = asyncio.Lock()
        self._dedup = MessageDeduplicator(max_size=4000, ttl_seconds=600)
        self._idle_tasks: Dict[str, asyncio.Task] = {}
        self._media_dir = Path(
            os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        ).expanduser() / "mobile-media"

    # ── служебное ────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Mobile App"

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    def _all_tokens(self) -> List[str]:
        return [t for t in [self.token, *self.extra_tokens] if t]

    # ── жизненный цикл ───────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Поднимает WebSocket-сервер для приложения."""
        if not self.token:
            return self._fail(
                "config_missing",
                "MOBILE_TOKEN не задан: мобильное приложение не сможет авторизоваться "
                "(сгенерируйте токен: openssl rand -hex 32)",
                retryable=False,
            )
        if not self._acquire_platform_lock(
            PLATFORM_NAME, f"{self.host}:{self.port}", f"адаптер hermes-mobile на {self.host}:{self.port}"
        ):
            return False
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover — ставится ensure_deps_fn
            return self._fail("deps_missing", f"не установлен пакет websockets: {exc}", retryable=False)
        try:
            self._server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port,
                max_size=proto.MAX_FRAME_BYTES,
                ping_interval=20,
                ping_timeout=20,
            )
        except OSError as exc:
            return self._fail("bind_failed", f"не удалось занять {self.host}:{self.port} — {exc}", retryable=True)
        self.listener_base = f"ws://{self.host}:{self.port}{self.path}"
        self._mark_connected(listener_base=self.listener_base)
        logger.info(
            "hermes-mobile: слушаю ws://%s:%s%s (устройств в белом списке: %d)",
            self.host, self.port, self.path, len(self.allowed_users),
        )
        return True

    async def disconnect(self) -> None:
        """Закрывает сервер и все активные соединения."""
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for chat_id, websocket in list(self._connections.items()):
            with contextlib.suppress(Exception):
                await websocket.close(code=1001, reason="gateway shutting down")
            self._connections.pop(chat_id, None)
        for task in list(self._idle_tasks.values()):
            await cancel_task(task)
        self._idle_tasks.clear()
        self._sessions.clear()
        self._mark_disconnected()

    # ── соединение приложения ────────────────────────────────────────────────

    async def _handle_connection(self, websocket) -> None:
        """Обслуживает одно подключение приложения до разрыва."""
        device: Optional[MobileDevice] = None
        try:
            if not self._path_matches(websocket):
                await self._reject(websocket, proto.E_UNSUPPORTED, f"Неизвестный путь {self._request_path(websocket)}")
                return
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.hello_timeout)
            frame = proto.loads(raw)
            if proto.frame_type(frame) != proto.C_HELLO:
                await self._reject(websocket, proto.E_BAD_FRAME, "Первым кадром должен быть hello")
                return
            token = self._token_from(frame, websocket)
            if not proto.is_authorized(token, self._all_tokens()):
                logger.warning("hermes-mobile: отклонено подключение, токен %s", proto.redact_token(token))
                await self._reject(websocket, proto.E_AUTH_FAILED, "Неверный токен приложения")
                return

            info = proto.parse_device(frame)
            candidate = info.get("id") or proto.parse_user(frame).get("id") or ""
            if not proto.device_allowed(candidate, self.allowed_users):
                logger.warning("hermes-mobile: устройство %s не в списке разрешённых", candidate or "<без id>")
                await self._reject(websocket, proto.E_AUTH_FAILED, "Устройство не в списке разрешённых")
                return

            device = self._register(frame, websocket)
            await self._push(
                device,
                {
                    "t": proto.S_READY,
                    "v": proto.PROTOCOL_VERSION,
                    "session": device.chat_id,
                    "device": {"id": device.device_id, "name": device.display_name()},
                    "avatar_url": self.avatar_url,
                    "capabilities": {"text": True, "voice": True, "typing": True, "avatar_states": list(proto.AVATAR_STATES)},
                },
            )
            logger.info("hermes-mobile: подключилось устройство %s (%s)", device.device_id, device.display_name())

            async for raw in websocket:
                await self._dispatch(device, websocket, raw)
        except proto.ProtocolError as exc:
            logger.warning("hermes-mobile: ошибка протокола — %s", exc.message)
            with contextlib.suppress(Exception):
                await websocket.send(proto.dumps(exc.to_frame()))
            if exc.fatal:
                with contextlib.suppress(Exception):
                    await websocket.close(code=CLOSE_UNAUTHORIZED, reason=exc.code)
        except asyncio.TimeoutError:
            logger.warning("hermes-mobile: приветствие не получено за %.0f с", self.hello_timeout)
            with contextlib.suppress(Exception):
                await websocket.close(code=CLOSE_UNAUTHORIZED, reason="hello timeout")
        except asyncio.CancelledError:  # shuts down cleanly
            raise
        except Exception:
            logger.exception("hermes-mobile: соединение завершилось ошибкой")
        finally:
            if device is not None:
                await self._unregister(device)

    async def _dispatch(self, device: MobileDevice, websocket, raw: Any) -> None:
        """Разбирает кадр и выполняет действие. Неизвестные кадры не рвут соединение."""
        device.last_seen = time.time()
        frame = proto.loads(raw)
        kind = proto.frame_type(frame)
        try:
            if kind == proto.C_TEXT:
                await self._on_text(device, frame)
            elif kind == proto.C_AUDIO:
                await self._on_audio(device, frame)
            elif kind == proto.C_TYPING:
                return
            elif kind == proto.C_PING:
                await websocket.send(proto.dumps({"t": proto.S_PONG, "ts": frame.get("ts")}))
            elif kind == proto.C_DELIVER:
                await self._on_deliver(device, frame)
            elif kind == proto.C_ACK:
                return
            elif kind == proto.C_HELLO:
                raise proto.ProtocolError(proto.E_BAD_FRAME, "Повторное приветствие в одном соединении")
            else:
                logger.debug("hermes-mobile: неизвестный кадр %r — игнорирую", proto.frame_type(frame))
        except proto.ProtocolError as exc:
            logger.warning("hermes-mobile: %s (%s)", exc.message, exc.code)
            await websocket.send(proto.dumps(exc.to_frame()))

    # ── входящие сообщения ───────────────────────────────────────────────────

    async def _on_text(self, device: MobileDevice, frame: Dict[str, Any]) -> None:
        message_id = proto.client_id(frame) or uuid.uuid4().hex
        if self._dedup.is_duplicate(f"{device.chat_id}:{message_id}"):
            logger.info("hermes-mobile: дубликат сообщения %s — пропускаю", message_id)
            return
        text = proto.require_text(frame)
        await self._set_avatar(device, "thinking")
        event = self._build_event(
            device,
            text=text,
            message_id=message_id,
            message_type=MessageType.TEXT,
        )
        await self.handle_message(event)

    async def _on_audio(self, device: MobileDevice, frame: Dict[str, Any]) -> None:
        message_id = proto.client_id(frame) or uuid.uuid4().hex
        if self._dedup.is_duplicate(f"{device.chat_id}:{message_id}"):
            logger.info("hermes-mobile: дубликат голосового %s — пропускаю", message_id)
            return
        audio, fmt = proto.require_audio(frame)
        path = self._store_audio(message_id, audio, fmt)
        transcript = str(frame.get("text") or "").strip()
        await self._set_avatar(device, "thinking")
        event = self._build_event(
            device,
            text=transcript,
            message_id=message_id,
            message_type=MessageType.VOICE,
            media_urls=[str(path)],
            media_types=["audio/wav" if fmt == "wav" else "audio/ogg"],
        )
        event.metadata["mobile"]["voice"] = True
        await self.handle_message(event)

    async def _on_deliver(self, device: MobileDevice, frame: Dict[str, Any]) -> None:
        """Служебная доставка от другого процесса (cron) — отдаём целевому устройству."""
        target = str(frame.get("target") or "").strip() or self.home_device
        text = proto.require_text(frame)
        session = await self._session_for(target)
        if session is None:
            raise proto.ProtocolError(proto.E_DEVICE_OFFLINE, f"Устройство {target or '<по умолчанию>'} не подключено")
        result = await self.send(session.chat_id, text)
        if not result.success:
            raise proto.ProtocolError(proto.E_DEVICE_OFFLINE, result.error or "не доставлено")

    def _build_event(
        self,
        device: MobileDevice,
        *,
        text: str,
        message_id: str,
        message_type: MessageType,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> MessageEvent:
        user_id = device.user_id or device.device_id
        user_name = device.display_name()
        source = self.build_source(
            chat_id=device.chat_id,
            chat_name=device.display_name(),
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
            message_id=message_id,
        )
        return MessageEvent(
            text=text,
            message_type=message_type,
            user_id=user_id,
            user_name=user_name,
            source=source,
            message_id=message_id,
            media_urls=list(media_urls or []),
            media_types=list(media_types or []),
            metadata={
                "mobile": {
                    "device_id": device.device_id,
                    "device_name": device.display_name(),
                    "platform": device.platform,
                    "app_version": device.app_version,
                }
            },
        )

    def _store_audio(self, message_id: str, audio: bytes, fmt: str) -> Path:
        """Сохраняет голосовое во временный файл: Hermes читает медиа с диска."""
        self._media_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in message_id if ch.isalnum() or ch in "-_") or uuid.uuid4().hex
        path = self._media_dir / f"{safe}.{fmt}"
        path.write_bytes(audio)
        return path

    # ── исходящие сообщения ──────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Доставляет ответ агента на устройство, нарезая длинный текст на части."""
        session = await self._session_for(chat_id)
        if session is None:
            return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        chunks = proto.chunk_text(content, self.max_message_length) or [""]
        message_id = uuid.uuid4().hex
        await self._set_avatar(session, "speaking")
        total = len(chunks)
        for index, chunk in enumerate(chunks):
            ok = await self._push(
                session,
                {
                    "t": proto.S_MESSAGE,
                    "id": message_id,
                    "text": chunk,
                    "chunk": {"index": index, "total": total},
                    "final": index == total - 1,
                },
            )
            if not ok:
                return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        self._schedule_idle(session)
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Индикатор «агент печатает» — приложение рисует его вокруг аватара."""
        session = await self._session_for(chat_id)
        if session is None:
            return
        await self._set_avatar(session, "thinking")
        await self._push(session, {"t": proto.S_TYPING})

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        session = await self._session_for(chat_id)
        return {"name": session.display_name() if session else chat_id, "type": "dm"}

    # ── вспомогательное для соединений ───────────────────────────────────────

    def _request_path(self, websocket) -> str:
        with contextlib.suppress(Exception):
            return str(getattr(getattr(websocket, "request", None), "path", "") or "")
        return ""

    def _path_matches(self, websocket) -> bool:
        """Проверяет путь эндпоинта; пустой путь (тесты/прокси) считаем совпадающим."""
        path = self._request_path(websocket).split("?", 1)[0]
        if not path:
            return True
        return path.rstrip("/").endswith(self.path.rstrip("/"))

    def _token_from(self, frame: Dict[str, Any], websocket) -> Optional[str]:
        """Токен из кадра ``hello`` либо из параметра ``?token=`` в URL."""
        token = frame.get("token")
        if token:
            return str(token).strip()
        query = self._request_path(websocket).split("?", 1)
        if len(query) == 2:
            values = parse_qs(query[1]).get("token") or []
            if values:
                return values[0].strip()
        return None

    def _register(self, frame: Dict[str, Any], websocket) -> MobileDevice:
        info = proto.parse_device(frame)
        user = proto.parse_user(frame)
        device_id = info.get("id") or user.get("id") or f"dev-{uuid.uuid4().hex[:8]}"
        chat_id = f"{PLATFORM_NAME}:{device_id}"
        now = time.time()
        device = MobileDevice(
            device_id=device_id,
            chat_id=chat_id,
            name=info.get("name", ""),
            platform=info.get("platform", ""),
            app_version=info.get("app_version", ""),
            user_id=user.get("id", ""),
            user_name=user.get("name", ""),
            connected_at=now,
            last_seen=now,
        )
        self._sessions[chat_id] = device
        self._connections[chat_id] = websocket
        return device

    async def _unregister(self, device: MobileDevice) -> None:
        self._connections.pop(device.chat_id, None)
        self._sessions.pop(device.chat_id, None)
        task = self._idle_tasks.pop(device.chat_id, None)
        if task is not None:
            await cancel_task(task)
        logger.info("hermes-mobile: устройство %s отключилось", device.device_id)

    async def _session_for(self, chat_id: str) -> Optional[MobileDevice]:
        """Ищет устройство по ``chat_id``, по «сырому» id и по домашнему устройству."""
        if chat_id in self._sessions and chat_id in self._connections:
            return self._sessions[chat_id]
        candidates = [chat_id, f"{PLATFORM_NAME}:{chat_id}", self.home_device, f"{PLATFORM_NAME}:{self.home_device}"]
        for candidate in candidates:
            if candidate and candidate in self._sessions and candidate in self._connections:
                return self._sessions[candidate]
        return None

    async def _push(self, device: MobileDevice, frame: Dict[str, Any]) -> bool:
        """Отправляет кадр устройству; False, если соединение уже закрыто."""
        websocket = self._connections.get(device.chat_id)
        if websocket is None:
            return False
        try:
            await websocket.send(proto.dumps(frame))
            device.last_seen = time.time()
            return True
        except Exception as exc:
            logger.info("hermes-mobile: не смог отправить кадр устройству %s — %s", device.device_id, exc)
            return False

    async def _reject(self, websocket, code: str, message: str) -> None:
        with contextlib.suppress(Exception):
            await websocket.send(proto.dumps({"t": proto.S_ERROR, "code": code, "message": message, "fatal": True}))
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason=code)

    async def _set_avatar(self, device: MobileDevice, state: str) -> None:
        """Состояние аватара в приложении: thinking → speaking → idle."""
        if state not in proto.AVATAR_STATES:
            state = "idle"
        await self._push(device, {"t": proto.S_AVATAR, "state": state})

    def _schedule_idle(self, device: MobileDevice) -> None:
        """Через паузу без новых сообщений возвращает аватар в покой."""
        previous = self._idle_tasks.pop(device.chat_id, None)
        if previous is not None:
            previous.cancel()

        async def _idle_later() -> None:
            try:
                await asyncio.sleep(self.typing_idle_seconds)
                await self._set_avatar(device, "idle")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("hermes-mobile: не удалось вернуть аватар в idle", exc_info=True)
            finally:
                self._idle_tasks.pop(device.chat_id, None)

        self._idle_tasks[device.chat_id] = asyncio.create_task(_idle_later())


# ── регистрация платформы ────────────────────────────────────────────────────

def check_requirements() -> bool:
    """ПАССИВНАЯ проверка зависимостей: пакет ``websockets`` доступен сейчас?

    Контракт Hermes: эта функция не должна ничего устанавливать — её свободно вызывают
    индикаторы статуса. Установкой занимается ``ensure_deps`` (``ensure_deps_fn``),
    и только когда платформа уже включена и вот-вот подключится.
    """
    return importlib.util.find_spec("websockets") is not None


def ensure_deps() -> bool:
    """АКТИВНАЯ установка зависимости (``websockets``) в текущее окружение."""
    if check_requirements():
        return True
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "websockets>=12"],
            check=True,
            timeout=180,
        )
    except Exception as exc:  # pragma: no cover — нет сети/pip
        logger.error("hermes-mobile: не удалось установить websockets — %s", exc)
        return False
    importlib.invalidate_caches()
    return check_requirements()


def validate_config(config) -> bool:
    """Хватает ли настроек, чтобы поднять платформу (для UI и ``gateway status``)."""
    extra = getattr(config, "extra", {}) or {}
    token = _env_or_extra(extra, "MOBILE_TOKEN", "token", "")
    return bool(str(token or "").strip())


def _env_enablement() -> Optional[dict]:
    """``env_enablement_fn``: заполняет ``PlatformConfig.extra`` из env до создания адаптера.

    ``None`` — платформа не настроена (нет токена), и тогда она не включается вовсе.
    """
    token = str(_get_scoped_secret("MOBILE_TOKEN", "") or "").strip()
    if not token:
        return None
    home_device = str(_get_scoped_secret("MOBILE_HOME_DEVICE", "") or "").strip()
    seed = _seed_extra_from_env(
        (
            ("MOBILE_HOST", "host", None),
            ("MOBILE_PORT", "port", int),
            ("MOBILE_PATH", "path", None),
            ("MOBILE_AVATAR_URL", "avatar_url", None),
            ("MOBILE_ALLOWED_USERS", "allowed_users", lambda v: proto.split_tokens(v)),
            ("MOBILE_EXTRA_TOKENS", "extra_tokens", lambda v: proto.split_tokens(v)),
        ),
        home_env="MOBILE_HOME_DEVICE",
        home_default=home_device or None,
    )
    return {"token": token, **seed}


async def standalone_send(
    config,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> dict:
    """``standalone_sender_fn``: доставка без шлюза в этом процессе.

    Подключается к работающему шлюзу как служебный клиент и просит его отдать сообщение
    устройству. Если шлюз не поднят (или устройство не подключено) — возвращает ошибку,
    а не «успех»: мобильная доставка требует живого подключения приложения.
    """
    extra = getattr(config, "extra", {}) or {}
    token = str(_env_or_extra(extra, "MOBILE_TOKEN", "token", "") or "").strip()
    if not token:
        return {"error": "MOBILE_TOKEN не задан"}
    host = str(_env_or_extra(extra, "MOBILE_HOST", "host", "127.0.0.1") or "127.0.0.1")
    port = coerce_port(_env_or_extra(extra, "MOBILE_PORT", "port", DEFAULT_PORT), DEFAULT_PORT)
    raw_path = "/" + str(_env_or_extra(extra, "MOBILE_PATH", "path", DEFAULT_PATH) or DEFAULT_PATH).strip().strip("/")
    if host in ("0.0.0.0", "::"):  # локальная доставка — идём на loopback
        host = "127.0.0.1"
    try:
        import websockets
    except ImportError as exc:
        return {"error": f"websockets недоступен: {exc}"}
    url = f"ws://{host}:{port}{raw_path}?token={quote(token)}"
    message_id = uuid.uuid4().hex
    try:
        async with websockets.connect(url, max_size=proto.MAX_FRAME_BYTES, open_timeout=10) as websocket:
            await websocket.send(
                proto.dumps(
                    {
                        "t": proto.C_HELLO,
                        "token": token,
                        "device": {"id": "standalone", "name": "Hermes (вне процесса шлюза)"},
                    }
                )
            )
            ready = proto.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            if proto.frame_type(ready) != proto.S_READY:
                return {"error": str(ready.get("message") or "шлюз отказал в подключении")}
            for index, chunk in enumerate(proto.chunk_text(message, proto.DEFAULT_CHUNK_LIMIT) or [""]):
                await websocket.send(
                    proto.dumps(
                        {
                            "t": proto.C_DELIVER,
                            "id": f"{message_id}:{index}",
                            "target": chat_id,
                            "text": chunk,
                        }
                    )
                )
            return {"success": True, "message_id": message_id}
    except Exception as exc:
        return {"error": f"шлюз недоступен ({exc.__class__.__name__}: {exc})"}


def interactive_setup() -> None:
    """``hermes gateway setup``: интерактивная настройка платформы."""
    from hermes_cli.setup import prompt, prompt_yes_no, save_env_value, print_header, print_info, print_success

    print_header("Mobile App (WebSocket)")
    print_info("Токен приложения защищает WebSocket-подключение вашего телефона.")
    token = prompt("Токен приложения (пусто — сгенерировать)", password=True).strip()
    if not token:
        token = os.urandom(32).hex()
        print_info(f"Сгенерирован токен: {token} — вставьте его в приложение.")
    save_env_value("MOBILE_TOKEN", token)
    host = prompt("Адрес прослушивания", default=DEFAULT_HOST).strip() or DEFAULT_HOST
    save_env_value("MOBILE_HOST", host)
    port = prompt("Порт", default=str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
    save_env_value("MOBILE_PORT", port)
    if prompt_yes_no("Ограничить список устройств?", default=False):
        save_env_value("MOBILE_ALLOWED_USERS", prompt("ID устройств через запятую", default="").strip())
    save_env_value("MOBILE_HOME_DEVICE", prompt("ID устройства по умолчанию для cron", default="").strip())
    print_success("Платформа Mobile App настроена. Перезапустите шлюз: hermes gateway restart")


def register(ctx) -> None:
    """Точка входа плагина: регистрирует платформенный адаптер в реестре Hermes."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Mobile App (WebSocket)",
        adapter_factory=lambda config: HermesMobileAdapter(config),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["MOBILE_TOKEN"],
        install_hint="pip install websockets",
        ensure_deps_fn=ensure_deps,
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        allowed_users_env="MOBILE_ALLOWED_USERS",
        max_message_length=proto.DEFAULT_CHUNK_LIMIT,
        emoji="📱",
        pii_safe=True,
        cron_deliver_env_var="MOBILE_HOME_DEVICE",
        standalone_sender_fn=standalone_send,
        platform_hint=(
            "Пользователь читает ответ в мобильном приложении на небольшом экране. "
            "Отвечайте по делу и по-русски; длинные таблицы и простыни логов не подходят — "
            "лучше короткие абзацы и списки. Ответ озвучивается аватаром, поэтому избегайте "
            "избыточных эмодзи и служебных пометок."
        ),
    )
    logger.debug("hermes-mobile: платформа зарегистрирована")
