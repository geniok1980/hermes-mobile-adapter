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
import binascii
import contextlib
import importlib.util
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # обычная загрузка как пакет плагина
    from . import protocol as proto
    from . import i18n
except ImportError:  # pragma: no cover — загрузка адаптера как отдельного модуля
    import protocol as proto  # type: ignore
    import i18n  # type: ignore

from gateway.config import Platform
from gateway.platforms._shared import (
    coerce_port,
    get_scoped_secret as _get_scoped_secret,
    seed_extra_from_env as _seed_extra_from_env,
)
from hermes_constants import get_hermes_home as _get_hermes_home
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, cancel_task

logger = logging.getLogger(__name__)

PLATFORM_NAME = "mobile"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_PATH = "/ws"
DEFAULT_HELLO_TIMEOUT = 15.0
DEFAULT_TYPING_IDLE_SECONDS = 3.0
MAX_OPEN_MEDIA_UPLOADS = 8  # незавершённых многочастных загрузок на устройство
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


def _is_loopback(host: str) -> bool:
    """Локальный peer (127.0.0.0/8, ::1): с него разрешена служебная доставка cron."""
    value = str(host or "").strip()
    return value.startswith("127.") or value in {"::1", "localhost"}


def _env_or_extra(extra: Dict[str, Any], env_key: str, extra_key: str, default: Any = None) -> Any:
    """Значение env имеет приоритет над ``config.yaml`` → ``extra``."""
    raw = _get_scoped_secret(env_key)
    if raw not in (None, ""):
        return raw
    value = extra.get(extra_key)
    return default if value in (None, "") else value


def _default_suffix(kind: str, mime: str) -> str:
    """Расширение для сохранённого входящего файла, если приложение его не назвало."""
    by_mime = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "image/heic": ".heic", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
        "audio/wav": ".wav", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
        "application/pdf": ".pdf", "text/plain": ".txt",
    }
    if mime in by_mime:
        return by_mime[mime]
    return {"image": ".jpg", "video": ".mp4", "audio": ".ogg", "document": ".bin"}.get(kind, ".bin")


def parse_target_ref(target_ref: str) -> Optional[tuple]:
    """Разбирает адрес доставки для ``hermes send --to mobile:<устройство>`` и cron.

    Принимает ``mobile:dev-1`` и просто ``dev-1`` (тогда подставляем префикс платформы).
    Возвращает ``(chat_id, thread_id)``; ``None`` — не наш адрес, пусть решает шлюз.
    """
    raw = str(target_ref or "").strip()
    if not raw:
        return None
    if raw.lower() in ("home", "mobile", PLATFORM_NAME, f"{PLATFORM_NAME}:home"):
        return None  # домашний канал/устройство резолвит сам шлюз
    if raw.startswith(f"{PLATFORM_NAME}:"):
        device = raw.split(":", 1)[1].strip()
        return (f"{PLATFORM_NAME}:{device}", None) if device else None
    return (f"{PLATFORM_NAME}:{raw}", None)


def validate_target_ref(target_ref: str) -> bool | str:
    """Проверка адреса доставки: непустое имя устройства (диагностику показывает шлюз)."""
    raw = str(target_ref or "").strip()
    if not raw:
        return i18n.t("err.empty_target")
    return True


class HermesMobileAdapter(BasePlatformAdapter):
    """WebSocket-адаптер мобильного приложения (платформа hermes-mobile)."""

    # Сколько символов шлём одним кадром message. База читает этот атрибут для нарезки
    # и для стриминга (gateway/platforms/base.py: MAX_MESSAGE_LENGTH, по умолчанию 4096).
    MAX_MESSAGE_LENGTH = proto.DEFAULT_CHUNK_LIMIT

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = getattr(config, "extra", {}) or {}

        self.token = str(_env_or_extra(extra, "MOBILE_TOKEN", "token", "") or "").strip()
        raw_extra_tokens = _env_or_extra(extra, "MOBILE_EXTRA_TOKENS", "extra_tokens", "")
        if isinstance(raw_extra_tokens, (list, tuple, set)):
            raw_extra_tokens = ",".join(str(item).strip() for item in raw_extra_tokens if str(item).strip())
        # Элемент ``device_id=token`` привязывает токен к устройству: с этим токеном нельзя
        # занять чужой device id (иначе перехват чужой сессии, ответов и карточек одобрения).
        self.token_bindings = proto.split_token_bindings(raw_extra_tokens)
        self.extra_tokens = list(self.token_bindings)
        # ``deliver`` (служебная доставка от cron) разрешён только loopback-пиру или
        # соединению с токеном из ``MOBILE_DELIVER_TOKENS``.
        self.deliver_tokens = set(
            proto.split_tokens(_env_or_extra(extra, "MOBILE_DELIVER_TOKENS", "deliver_tokens", ""))
        )
        self.host = str(_env_or_extra(extra, "MOBILE_HOST", "host", DEFAULT_HOST) or DEFAULT_HOST)
        self.port = coerce_port(_env_or_extra(extra, "MOBILE_PORT", "port", DEFAULT_PORT), DEFAULT_PORT)
        raw_path = str(_env_or_extra(extra, "MOBILE_PATH", "path", DEFAULT_PATH) or DEFAULT_PATH)
        self.path = "/" + raw_path.strip().strip("/")
        self.avatar_url = str(_env_or_extra(extra, "MOBILE_AVATAR_URL", "avatar_url", "") or "").strip()

        # Белый список устройств — отдельный от пользовательского: MOBILE_ALLOWED_USERS
        # проверяет сам Hermes (это список пользователей, там pairing/политики), а устройства
        # ограничиваем отдельной переменной, чтобы одно имя не значило две разные вещи.
        allowed_devices = extra.get("allowed_devices") or proto.split_tokens(
            _get_scoped_secret("MOBILE_ALLOWED_DEVICES", "")
        )
        if isinstance(allowed_devices, str):
            allowed_devices = proto.split_tokens(allowed_devices)
        self.allowed_devices: List[str] = [str(a).strip() for a in allowed_devices if str(a).strip()]
        self.home_device = str(extra.get("home_device") or _get_scoped_secret("MOBILE_HOME_DEVICE", "") or "").strip()

        max_len = extra.get("max_message_length")
        if max_len is None:
            with contextlib.suppress(Exception):
                from gateway.platform_registry import platform_registry
                max_len = platform_registry.get(PLATFORM_NAME).max_message_length
        self.max_message_length = int(max_len or proto.DEFAULT_CHUNK_LIMIT)
        # Каталог команд: по умолчанию отдаём только по запросу приложения ({t:"commands"}),
        # чтобы поток кадров не менялся для уже выпущенных приложений. Автоматическую
        # отправку сразу после ready можно включить: MOBILE_CATALOG_AFTER_READY=1.
        raw_catalog = _env_or_extra(extra, "MOBILE_CATALOG_AFTER_READY", "catalog_after_ready", False)
        self.catalog_after_ready = str(raw_catalog).strip().lower() in ("1", "true", "yes", "on")

        self.hello_timeout = float(extra.get("hello_timeout", DEFAULT_HELLO_TIMEOUT) or DEFAULT_HELLO_TIMEOUT)
        self.typing_idle_seconds = float(extra.get("typing_idle_seconds", DEFAULT_TYPING_IDLE_SECONDS))

        self._server: Any = None
        self.listener_base: Optional[str] = None  # ws://host:port/path — публикуется в статусе шлюза
        self._sessions: Dict[str, MobileDevice] = {}          # chat_id -> устройство
        self._connections: Dict[str, Any] = {}                # chat_id -> websocket
        self._connection_tokens: Dict[str, str] = {}          # chat_id -> токен соединения
        self._deliver_allowed: Dict[str, bool] = {}           # chat_id -> можно ли слать deliver
        self._lock = asyncio.Lock()
        self._dedup = MessageDeduplicator(max_size=4000, ttl_seconds=600)
        self._idle_tasks: Dict[str, asyncio.Task] = {}
        self._media_dir = Path(_get_hermes_home()).expanduser() / "mobile-media"
        # Карточки-вопросы (одобрение команды, clarify, подтверждение слэш-команды):
        # prompt_id → {kind, session_key, chat_id, token, choices, created_at}.
        self._pending_prompts: Dict[str, Dict[str, Any]] = {}
        self._prompt_tasks: Dict[str, asyncio.Task] = {}
        self.prompt_ttl = float(extra.get("prompt_ttl", proto.DEFAULT_PROMPT_TTL) or proto.DEFAULT_PROMPT_TTL)
        # Сборка больших входящих файлов: "<chat_id>:<message_id>" → части.
        self._media_buffers: Dict[str, Dict[str, Any]] = {}

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
                "MOBILE_TOKEN is not set: the mobile app will not be able to authenticate "
                "(generate a token: openssl rand -hex 32)",
                retryable=False,
            )
        if not self._acquire_platform_lock(
            PLATFORM_NAME, f"{self.host}:{self.port}", f"hermes-mobile adapter on {self.host}:{self.port}"
        ):
            return False
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover — ставится ensure_deps_fn
            return self._fail("deps_missing", f"websockets package is not installed: {exc}", retryable=False)
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
            return self._fail("bind_failed", f"failed to bind {self.host}:{self.port} — {exc}", retryable=True)
        self.listener_base = f"ws://{self.host}:{self.port}{self.path}"
        self._mark_connected(listener_base=self.listener_base)
        logger.info(
            "hermes-mobile: listening on ws://%s:%s%s (allow-listed devices: %d)",
            self.host, self.port, self.path, len(self.allowed_devices),
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
        for task in list(self._prompt_tasks.values()):
            await cancel_task(task)
        self._prompt_tasks.clear()
        self._pending_prompts.clear()
        self._media_buffers.clear()
        self._sessions.clear()
        self._mark_disconnected()

    # ── соединение приложения ────────────────────────────────────────────────

    async def _handle_connection(self, websocket) -> None:
        """Обслуживает одно подключение приложения до разрыва."""
        device: Optional[MobileDevice] = None
        try:
            if not self._path_matches(websocket):
                await self._reject(websocket, proto.E_UNSUPPORTED, i18n.t("err.unknown_path", path=self._request_path(websocket)))
                return
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.hello_timeout)
            frame = proto.loads(raw)
            if proto.frame_type(frame) != proto.C_HELLO:
                await self._reject(websocket, proto.E_BAD_FRAME, i18n.t("err.first_frame_hello"))
                return
            token = self._token_from(frame, websocket)
            if not proto.is_authorized(token, self._all_tokens()):
                logger.warning("hermes-mobile: connection rejected, token %s", proto.redact_token(token))
                await self._reject(websocket, proto.E_AUTH_FAILED, i18n.t("err.bad_token"))
                return

            info = proto.parse_device(frame)
            candidate = info.get("id") or proto.parse_user(frame).get("id") or ""
            if not proto.device_allowed(candidate, self.allowed_devices):
                logger.warning("hermes-mobile: device %s is not in the allow list", candidate or "<no id>")
                await self._reject(websocket, proto.E_AUTH_FAILED, i18n.t("err.device_not_allowed"))
                return

            device = self._register(frame, websocket, token)
            await self._push(
                device,
                {
                    "t": proto.S_READY,
                    "v": proto.PROTOCOL_VERSION,
                    "session": device.chat_id,
                    "device": {"id": device.device_id, "name": device.display_name()},
                    "avatar_url": self.avatar_url,
                    "capabilities": {
                        "text": True,
                        "voice": True,
                        "typing": True,
                        "avatar_states": list(proto.AVATAR_STATES),
                        "media_in": list(proto.INBOUND_MEDIA_KINDS),
                        "media_out": list(proto.OUTBOUND_MEDIA_KINDS),
                        "prompts": list(proto.PROMPT_KINDS),
                        "commands": True,          # умеем отдавать каталог команд агента
                        "models": True,            # умеем отдавать список моделей для выбора
                        "edit": True,               # приложение умеет править сообщение по id (стриминг)
                        "max_media_bytes": proto.MAX_MEDIA_BYTES,
                        "max_text_chars": proto.MAX_TEXT_CHARS,
                    },
                },
            )
            # Каталог команд сразу после ready — только если так настроено (issue #23):
            # по умолчанию приложение само присылает {t:"commands"}.
            if self.catalog_after_ready:
                await self._send_command_catalog(device)
            logger.info("hermes-mobile: device %s (%s) connected", device.device_id, device.display_name())

            async for raw in websocket:
                await self._dispatch(device, websocket, raw)
        except proto.ProtocolError as exc:
            logger.warning("hermes-mobile: protocol error — %s", exc.message)
            with contextlib.suppress(Exception):
                await websocket.send(proto.dumps(exc.to_frame()))
            if exc.fatal:
                with contextlib.suppress(Exception):
                    await websocket.close(code=CLOSE_UNAUTHORIZED, reason=exc.code)
        except asyncio.TimeoutError:
            logger.warning("hermes-mobile: no hello received within %.0f s", self.hello_timeout)
            with contextlib.suppress(Exception):
                await websocket.close(code=CLOSE_UNAUTHORIZED, reason="hello timeout")
        except asyncio.CancelledError:  # shuts down cleanly
            raise
        except Exception:
            logger.exception("hermes-mobile: connection failed")
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
            elif kind == proto.C_MEDIA:
                await self._on_media(device, frame)
            elif kind == proto.C_PROMPT:
                await self._on_prompt_response(device, frame)
            elif kind == proto.C_ACK:
                return
            elif kind == proto.C_COMMANDS:
                await self._send_command_catalog(device)
            elif kind == proto.C_MODELS:
                await self._send_model_options(device)
            elif kind == proto.C_HELLO:
                raise proto.ProtocolError(proto.E_BAD_FRAME, i18n.t("err.repeated_hello"))
            else:
                logger.debug("hermes-mobile: unknown frame %r — ignoring", proto.frame_type(frame))
        except proto.ProtocolError as exc:
            logger.warning("hermes-mobile: %s (%s)", exc.message, exc.code)
            await websocket.send(proto.dumps(exc.to_frame()))

    # ── входящие сообщения ───────────────────────────────────────────────────

    async def _on_text(self, device: MobileDevice, frame: Dict[str, Any]) -> None:
        message_id = proto.client_id(frame) or uuid.uuid4().hex
        if self._dedup.is_duplicate(f"{device.chat_id}:{message_id}"):
            logger.info("hermes-mobile: duplicate message %s — skipping", message_id)
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
            logger.info("hermes-mobile: duplicate voice message %s — skipping", message_id)
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

    async def _on_media(self, device: MobileDevice, frame: Dict[str, Any]) -> None:
        """Медиа от приложения: фото, видео, файл, аудио.

        Большие файлы приходят несколькими кадрами (``part``/``parts``) — собираем их
        до последнего куска и только потом отдаём агенту, иначе он получит обрывок.
        """
        info = proto.parse_media_upload(frame)
        message_id = info["message_id"]
        key = f"{device.chat_id}:{message_id}"
        if info["parts"] > 1:
            # Многочастный файл: собираем куски и дедуп проверяем только на целом файле —
            # иначе вторая часть была бы отброшена как «повтор» первой.
            buffer = self._media_buffers.get(key)
            if buffer is None:
                # Ограничиваем незавершённые загрузки: иначе клиент держит память шлюза
                # открытыми буферами (и по числу загрузок, и по суммарному объёму).
                if len(self._device_buffers(device.chat_id)) >= MAX_OPEN_MEDIA_UPLOADS:
                    raise proto.ProtocolError(
                        proto.E_TOO_BIG,
                        i18n.t("err.too_many_uploads", limit=MAX_OPEN_MEDIA_UPLOADS),
                    )
                buffer = {
                    "parts": info["parts"], "chunks": {}, "bytes": 0, "kind": info["kind"],
                    "mime": info["mime"], "filename": info["filename"], "caption": info["caption"],
                }
                self._media_buffers[key] = buffer
            buffer["chunks"][info["part"]] = info["data"]
            buffer["bytes"] = int(buffer.get("bytes") or 0) + len(info["data"])
            if (
                buffer["bytes"] > proto.MAX_MEDIA_BYTES
                or self._device_buffer_bytes(device.chat_id) > proto.MAX_MEDIA_BYTES
            ):
                self._media_buffers.pop(key, None)
                raise proto.ProtocolError(
                    proto.E_TOO_BIG,
                    i18n.t("err.total_upload_too_big", limit=proto.MAX_MEDIA_BYTES // (1024 * 1024)),
                )
            if len(buffer["chunks"]) < info["parts"]:
                logger.debug(
                    "hermes-mobile: media %s — part %d/%d", message_id, info["part"] + 1, info["parts"]
                )
                return
            part_map = buffer["chunks"]
            info = {**info, **{field: buffer[field] for field in ("kind", "mime", "filename", "caption")}}
        else:
            part_map = {0: info["data"]}
        if self._dedup.is_duplicate(key):
            self._media_buffers.pop(key, None)
            logger.info("hermes-mobile: duplicate media %s — skipping", message_id)
            return
        payload = b"".join(part_map[index] for index in sorted(part_map))
        self._media_buffers.pop(key, None)
        await self._finish_media(device, info, payload, message_id)

    async def _finish_media(
        self, device: MobileDevice, info: Dict[str, Any], payload: bytes, message_id: str
    ) -> None:
        """Сохраняет файл на диск и передаёт агенту как медиа-событие."""
        if len(payload) > proto.MAX_MEDIA_BYTES:
            raise proto.ProtocolError(
                proto.E_TOO_BIG,
                i18n.t("err.file_too_big", limit=proto.MAX_MEDIA_BYTES // (1024 * 1024)),
            )
        suffix = Path(info["filename"] or "").suffix or _default_suffix(info["kind"], info["mime"])
        path = self._store_bytes(message_id, payload, suffix)
        kind = info["kind"]
        message_type = {
            "image": MessageType.PHOTO,
            "video": MessageType.VIDEO,
            "document": MessageType.DOCUMENT,
            "audio": MessageType.VOICE,
        }.get(kind, MessageType.DOCUMENT)
        await self._set_avatar(device, "thinking")
        event = self._build_event(
            device,
            text=info["caption"] or "",
            message_id=message_id,
            message_type=message_type,
            media_urls=[str(path)],
            media_types=[info["mime"]],
            media_text_inlined=[False],   # текст в файле не подставляем в сообщение
        )
        event.metadata["mobile"].update({"media_kind": kind, "filename": info["filename"]})
        await self.handle_message(event)

    async def _on_deliver(self, device: MobileDevice, frame: Dict[str, Any]) -> None:
        """Служебная доставка от другого процесса (cron) — отдаём целевому устройству."""
        if not self._deliver_allowed.get(device.chat_id, False):
            raise proto.ProtocolError(
                proto.E_FORBIDDEN,
                i18n.t("err.deliver_not_allowed"),
            )
        target = str(frame.get("target") or "").strip() or self.home_device
        text = str(frame.get("text") or "")
        media = frame.get("media")
        if not text.strip() and not (isinstance(media, dict) and media.get("data")):
            raise proto.ProtocolError(proto.E_BAD_FRAME, i18n.t("err.deliver_empty"))
        session = await self._session_for(target)
        if session is None:
            raise proto.ProtocolError(proto.E_DEVICE_OFFLINE, i18n.t("err.device_offline", device=target or i18n.t("err.device_default")))
        if isinstance(media, dict) and media.get("data"):
            await self._deliver_media(session, media, text)
            return
        result = await self.send(session.chat_id, text)
        if not result.success:
            raise proto.ProtocolError(proto.E_DEVICE_OFFLINE, result.error or "not delivered")

    async def _deliver_media(self, session: MobileDevice, media: Dict[str, Any], caption: str) -> None:
        """Вложение из служебной доставки: сохраняем во временный файл и отдаём как медиа."""
        try:
            payload = base64.b64decode(str(media.get("data") or ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise proto.ProtocolError(proto.E_BAD_FRAME, i18n.t("err.media_bad_base64")) from exc
        if not payload:
            raise proto.ProtocolError(proto.E_BAD_FRAME, i18n.t("err.empty_attachment"))
        if len(payload) > proto.MAX_MEDIA_BYTES:
            raise proto.ProtocolError(
                proto.E_TOO_BIG, i18n.t("err.attachment_too_big", limit=proto.MAX_MEDIA_BYTES // (1024 * 1024))
            )
        kind = str(media.get("kind") or "document")
        filename = str(media.get("filename") or "") or None
        suffix = Path(filename or "").suffix or _default_suffix(kind, str(media.get("mime") or ""))
        path = self._store_bytes(uuid.uuid4().hex, payload, suffix)
        out_kind = kind if kind in ("image", "voice", "video") else "document"
        result = await self._send_media_file(
            session.chat_id, str(path), out_kind, caption or None, filename=filename
        )
        if not result.success:
            raise proto.ProtocolError(proto.E_DEVICE_OFFLINE, result.error or "attachment not delivered")

    def _build_event(
        self,
        device: MobileDevice,
        *,
        text: str,
        message_id: str,
        message_type: MessageType,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
        media_text_inlined: Optional[List[Optional[bool]]] = None,
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
            media_text_inlined=list(media_text_inlined or []),
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
        return self._store_bytes(message_id, audio, f".{fmt}")

    def _store_bytes(self, message_id: str, payload: bytes, suffix: str) -> Path:
        """Сохраняет входящий файл в каталог медиа профиля и возвращает путь."""
        self._media_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in message_id if ch.isalnum() or ch in "-_") or uuid.uuid4().hex
        clean_suffix = suffix if suffix.startswith(".") and len(suffix) <= 12 else ""
        path = self._media_dir / f"{safe}{clean_suffix}"
        path.write_bytes(payload)
        return path

    # ── медиа наружу (агент → приложение) ────────────────────────────────────

    async def _send_media_file(
        self,
        chat_id: str,
        path: str,
        kind: str,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        reply_to: Optional[str] = None,
        url: Optional[str] = None,
    ) -> SendResult:
        """Отправляет файл приложению кадром ``media`` (большие — несколькими частями).

        Локальный файл читается с диска; если файла нет, но есть ``url`` — шлём ссылку,
        приложение скачает само. Никаких «тихих» успехов: если не отправили — success=False,
        и шлюз сам сообщит агенту, что доставка не удалась.
        """
        session = await self._session_for(chat_id)
        if session is None:
            return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        message_id = uuid.uuid4().hex
        name = filename or (Path(path).name if path else None)
        mime = proto.guess_mime(name, "application/octet-stream")
        if kind == "voice" and mime == "application/octet-stream":
            mime = "audio/ogg"

        payload = b""
        if path and Path(path).is_file():
            try:
                payload = Path(path).read_bytes()
            except OSError as exc:
                logger.warning("hermes-mobile: could not read file %s — %s", path, exc)
        if not payload and not url:
            return SendResult(success=False, error=f"File unavailable: {path}", retryable=False)
        if payload and len(payload) > proto.MAX_MEDIA_BYTES:
            return SendResult(
                success=False,
                error=(
                    f"File is {len(payload) / 1048576:.1f} MB, above the "
                    f"{proto.MAX_MEDIA_BYTES // 1048576} MB limit of the mobile channel"
                ),
                retryable=False,
            )

        chunks = proto.split_bytes(payload) if payload else [b""]
        total = len(chunks)
        await self._set_avatar(session, "speaking")
        for index, chunk in enumerate(chunks):
            frame: Dict[str, Any] = {
                "t": proto.S_MEDIA,
                "id": message_id,
                "kind": kind,
                "mime": mime,
                "filename": name,
                "caption": caption or "",
                "part": index,
                "parts": total,
                "final": index == total - 1,
                "size": len(payload),
            }
            if chunk:
                frame["data"] = base64.b64encode(chunk).decode()
            if url and index == 0:
                frame["url"] = url
            if reply_to:
                frame["reply_to"] = reply_to
            if not await self._push(session, frame):
                return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        self._schedule_idle(session)
        return SendResult(success=True, message_id=message_id)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Картинка из локального файла (агент сгенерировал график/скриншот)."""
        return await self._send_media_file(chat_id, image_path, "image", caption, reply_to=reply_to)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Картинка по ссылке либо из локального файла (``file://`` или путь)."""
        target = str(image_url or "")
        local = target[7:] if target.startswith("file://") else target
        if local and not target.startswith(("http://", "https://")) and Path(local).is_file():
            return await self._send_media_file(chat_id, local, "image", caption, reply_to=reply_to)
        return await self._send_media_file(chat_id, "", "image", caption, reply_to=reply_to, url=target)

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """GIF/анимация — приложение покажет её как картинку или видео."""
        return await self.send_image(chat_id, animation_url, caption, reply_to, metadata)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Голосовое сообщение от агента (озвучка ответа аватаром)."""
        return await self._send_media_file(chat_id, audio_path, "voice", caption, reply_to=reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Видео — приложение проигрывает его инлайн."""
        return await self._send_media_file(chat_id, video_path, "video", caption, reply_to=reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Файл (PDF, xlsx, zip…) — приложение сохраняет или открывает его."""
        return await self._send_media_file(
            chat_id, file_path, "document", caption, filename=file_name, reply_to=reply_to
        )

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Any],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> SendResult:
        """Несколько картинок подряд. Успех — если доставлена хотя бы одна (как в контракте базы)."""
        delivered, last_id, first_error = 0, None, None
        for item in images:
            url = item[0] if isinstance(item, (tuple, list)) and item else str(item)
            caption = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None
            result = await self.send_image(chat_id, url, caption)
            if result.success:
                delivered += 1
                last_id = result.message_id
            elif first_error is None:
                first_error = result.error
            if human_delay:
                await asyncio.sleep(human_delay)
        if delivered:
            return SendResult(success=True, message_id=last_id)
        return SendResult(success=False, error=first_error or "no image was delivered")

    # ── карточки-вопросы (одобрение команды, clarify, подтверждение) ─────────

    async def _send_prompt(
        self,
        *,
        chat_id: str,
        kind: str,
        text: str,
        options: List[Dict[str, Any]],
        session_key: str = "",
        token: str = "",
        choices: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Шлёт приложению карточку с вариантами и запоминает её до ответа или истечения.

        Если карточку не удалось отправить, состояние не оставляем: иначе следующий,
        никак не связанный ответ пользователя был бы принят за ответ на невидимую карточку.
        """
        session = await self._session_for(chat_id)
        if session is None:
            return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        prompt_id = uuid.uuid4().hex
        self._pending_prompts[prompt_id] = {
            "kind": kind,
            "session_key": session_key,
            "token": token,
            "chat_id": session.chat_id,
            "choices": list(choices or []),
            "created_at": time.time(),
        }
        payload: Dict[str, Any] = {
            "t": proto.S_PROMPT,
            "id": prompt_id,
            "kind": kind,
            "text": text,
            "options": options,
            "session": session.chat_id,
            "timeout_seconds": int(self.prompt_ttl),
        }
        if extra_fields:
            payload.update({key: value for key, value in extra_fields.items() if value not in (None, "")})
        ok = await self._push(session, payload)
        if not ok:
            self._pending_prompts.pop(prompt_id, None)
            return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        self._schedule_prompt_expiry(prompt_id)
        logger.info("hermes-mobile: prompt %s (%s) sent to device %s", prompt_id, kind, session.device_id)
        return SendResult(success=True, message_id=prompt_id)

    async def _send_exec_approval_prompt(self, prompt) -> SendResult:
        """Кнопки одобрения опасной команды: once / session / always / deny.

        База собирает ``ExecApprovalPrompt`` с готовыми вариантами (учитывая
        ``allow_permanent``/``allow_session``/``smart_denied``) — берём их как есть.
        """
        options = [
            {"id": choice, "label": label, "style": style or ""}
            for label, choice, style in (getattr(prompt, "actions", None) or [])
        ]
        return await self._send_prompt(
            chat_id=prompt.chat_id,
            kind=proto.P_EXEC_APPROVAL,
            text=prompt.text,
            options=options,
            session_key=prompt.session_key,
            metadata=getattr(prompt, "metadata", None),
            # Команду и описание отдаём отдельными полями: приложение показывает их
            # рядом с кнопками, чтобы человек видел, что именно одобряет.
            extra_fields={
                "command": str(getattr(prompt, "command", "") or ""),
                "description": str(getattr(prompt, "description", "") or ""),
            },
        )

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Подтверждение слэш-команды: Approve Once / Always Approve / Cancel."""
        text = f"{title}\n\n{message}" if title and message else (message or title)
        return await self._send_prompt(
            chat_id=chat_id,
            kind=proto.P_SLASH_CONFIRM,
            text=text,
            options=[
                {"id": "once", "label": i18n.t("label.approve_once"), "style": "primary"},
                {"id": "always", "label": i18n.t("label.approve_always"), "style": ""},
                {"id": "cancel", "label": i18n.t("label.cancel"), "style": "danger"},
            ],
            session_key=session_key,
            token=confirm_id,
            metadata=metadata,
        )

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Уточняющий вопрос: кнопки по вариантам + «свой ответ» (текстом)."""
        options = [
            {"id": f"c{index}", "label": str(choice), "style": ""}
            for index, choice in enumerate(choices or [])
        ]
        if options:
            options.append({"id": proto.CLARIFY_OTHER, "label": i18n.t("label.clarify_other"), "style": ""})
        return await self._send_prompt(
            chat_id=chat_id,
            kind=proto.P_CLARIFY,
            text=question,
            options=options,
            session_key=session_key,
            token=clarify_id,
            choices=[str(choice) for choice in (choices or [])],
            metadata=metadata,
        )

    async def _on_prompt_response(self, device: MobileDevice, frame: Dict[str, Any]) -> None:
        """Ответ на карточку: резолвим через штатные резолверы Hermes, не как сообщение в чат."""
        info = proto.parse_prompt_response(frame)
        pending = self._pending_prompts.get(info["prompt_id"])
        if pending is None:
            logger.info("hermes-mobile: response to unknown/expired prompt %s — ignoring", info["prompt_id"])
            await self._push(device, {"t": proto.S_PROMPT_DONE, "id": info["prompt_id"], "expired": True})
            return
        # Карточку может закрыть только то устройство, которому она показана: иначе
        # подключившийся с общим токеном клиент одобрял бы чужие опасные команды.
        if pending.get("chat_id") and pending["chat_id"] != device.chat_id:
            logger.warning(
                "hermes-mobile: device %s answered prompt %s of another device — denied",
                device.device_id,
                info["prompt_id"],
            )
            raise proto.ProtocolError(proto.E_FORBIDDEN, i18n.t("err.prompt_other_device"))
        self._pending_prompts.pop(info["prompt_id"], None)
        task = self._prompt_tasks.pop(info["prompt_id"], None)
        if task is not None:
            task.cancel()
        kind = pending["kind"]
        option_id, text = info["option_id"], info["text"].strip()
        resolved_option = await self._resolve_prompt(kind, pending, option_id, text)
        await self._push(
            device,
            {"t": proto.S_PROMPT_DONE, "id": info["prompt_id"], "option_id": resolved_option},
        )

    async def _resolve_prompt(
        self, kind: str, pending: Dict[str, Any], option_id: str, text: str
    ) -> Optional[str]:
        """Вызывает резолвер Hermes и возвращает фактически применённый вариант."""
        # Ключевое правило: ответ всегда валидируем сами — незнакомое значение
        # превращаем в безопасное (deny / cancel), как это делают штатные адаптеры.
        if kind == proto.P_EXEC_APPROVAL:
            choice = option_id if proto.option_allowed(kind, option_id) else "deny"
            from tools.approval import resolve_gateway_approval
            from asyncio import to_thread as _to_thread  # резолвер синхронный
            count = await _to_thread(resolve_gateway_approval, pending["session_key"], choice)
            logger.info(
                "hermes-mobile: command approval → %s (pending: %s)", choice, count
            )
            return choice
        if kind == proto.P_SLASH_CONFIRM:
            choice = option_id if proto.option_allowed(kind, option_id) else "cancel"
            from tools import slash_confirm as slash_confirm_mod
            result_text = await slash_confirm_mod.resolve(pending["session_key"], pending["token"], choice)
            if result_text:
                session = await self._session_for(pending["chat_id"])
                if session is not None:
                    await self.send(session.chat_id, str(result_text))
            return choice
        if kind == proto.P_CLARIFY:
            from tools.clarify_gateway import mark_awaiting_text, resolve_gateway_clarify
            choices = pending.get("choices") or []
            if text and (not option_id or option_id == proto.CLARIFY_OTHER):
                resolve_gateway_clarify(pending["token"], text)
                return proto.CLARIFY_OTHER
            if option_id == proto.CLARIFY_OTHER or not proto.option_allowed(kind, option_id):
                mark_awaiting_text(pending["token"])
                return proto.CLARIFY_OTHER
            index = int(option_id[1:])
            if index >= len(choices):
                mark_awaiting_text(pending["token"])
                return proto.CLARIFY_OTHER
            resolve_gateway_clarify(pending["token"], choices[index])
            return option_id
        logger.warning("hermes-mobile: unknown prompt kind %r", kind)
        return None

    def _schedule_prompt_expiry(self, prompt_id: str) -> None:
        """Через ``prompt_ttl`` закрывает карточку: приложение получает prompt_done(expired)."""
        previous = self._prompt_tasks.pop(prompt_id, None)
        if previous is not None:
            previous.cancel()

        async def _expire() -> None:
            try:
                await asyncio.sleep(self.prompt_ttl)
                pending = self._pending_prompts.pop(prompt_id, None)
                if pending is None:
                    return
                session = await self._session_for(pending["chat_id"])
                if session is not None:
                    await self._push(session, {"t": proto.S_PROMPT_DONE, "id": prompt_id, "expired": True})
                logger.info("hermes-mobile: prompt %s expired", prompt_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("hermes-mobile: could not close prompt %s", prompt_id, exc_info=True)
            finally:
                self._prompt_tasks.pop(prompt_id, None)

        self._prompt_tasks[prompt_id] = asyncio.create_task(_expire())

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

    async def stop_typing(self, chat_id: str) -> None:
        """Гасит индикатор: шлюз запускает цикл «печатает» каждые ~2 секунды и зовёт это в конце."""
        session = await self._session_for(chat_id)
        if session is None:
            return
        await self._push(session, {"t": proto.S_TYPING, "stop": True})
        await self._set_avatar(session, "idle")

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """Правка уже отправленного сообщения — это включает потоковую выдачу ответа.

        Шлюз при включённом ``gateway.streaming.enabled`` сначала шлёт сообщение целиком,
        затем правит его по мере генерации и в конце вызывает с ``finalize=True``.
        Приложение перерисовывает сообщение с тем же ``id``. Если вернуть success=False,
        шлюз не потеряет текст — дошлёт хвост обычным сообщением.
        """
        session = await self._session_for(chat_id)
        if session is None:
            return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        ok = await self._push(
            session,
            {
                "t": proto.S_MESSAGE,
                "id": message_id,
                "text": content,
                "edit": True,
                "chunk": {"index": 0, "total": 1},
                "final": bool(finalize),
            },
        )
        if not ok:
            return SendResult(success=False, error=proto.E_DEVICE_OFFLINE, retryable=False)
        return SendResult(success=True, message_id=message_id)

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
        """Токен только из кадра ``hello``: query-параметры попадают в логи прокси."""
        token = frame.get("token")
        return str(token).strip() if token else None

    def _register(self, frame: Dict[str, Any], websocket, token: str = "") -> MobileDevice:
        info = proto.parse_device(frame)
        user = proto.parse_user(frame)
        device_id = info.get("id") or user.get("id") or f"dev-{uuid.uuid4().hex[:8]}"
        chat_id = f"{PLATFORM_NAME}:{device_id}"
        # Токен, привязанный к устройству (``device_id=token``), не может занять другой id.
        bound = self.token_bindings.get(token, "")
        if bound and bound != device_id:
            raise proto.ProtocolError(
                proto.E_FORBIDDEN,
                i18n.t("err.token_bound", device=bound),
                fatal=True,
            )
        existing = self._connections.get(chat_id)
        if existing is not None and existing is not websocket:
            if self._connection_tokens.get(chat_id, "") != token:
                raise proto.ProtocolError(
                    proto.E_FORBIDDEN,
                    i18n.t("err.device_connected", device=device_id),
                    fatal=True,
                )
            # То же устройство с тем же токеном — это переподключение: закрываем старое.
            with contextlib.suppress(Exception):
                asyncio.create_task(existing.close(code=1000, reason="replaced by a new connection"))
        peer = getattr(websocket, "remote_address", None)
        peer_host = peer[0] if isinstance(peer, (tuple, list)) and peer else ""
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
        self._connection_tokens[chat_id] = token
        # Служебные кадры доставки — только от cron: loopback-пир или отдельный токен.
        self._deliver_allowed[chat_id] = bool(token) and (
            token in self.deliver_tokens or _is_loopback(peer_host)
        )
        return device

    def _device_buffers(self, chat_id: str) -> Dict[str, Dict[str, Any]]:
        """Незавершённые многочастные загрузки конкретного устройства."""
        prefix = f"{chat_id}:"
        return {key: value for key, value in self._media_buffers.items() if key.startswith(prefix)}

    def _device_buffer_bytes(self, chat_id: str) -> int:
        """Сколько байт устройство уже держит в незавершённых загрузках."""
        return sum(int(buffer.get("bytes") or 0) for buffer in self._device_buffers(chat_id).values())

    async def _unregister(self, device: MobileDevice) -> None:
        self._connections.pop(device.chat_id, None)
        self._connection_tokens.pop(device.chat_id, None)
        self._deliver_allowed.pop(device.chat_id, None)
        # Незавершённые загрузки отключившегося устройства больше никто не дополнит.
        for key in list(self._device_buffers(device.chat_id)):
            self._media_buffers.pop(key, None)
        self._sessions.pop(device.chat_id, None)
        task = self._idle_tasks.pop(device.chat_id, None)
        if task is not None:
            await cancel_task(task)
        logger.info("hermes-mobile: device %s disconnected", device.device_id)

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
            logger.info("hermes-mobile: could not send a frame to device %s — %s", device.device_id, exc)
            return False

    async def _send_command_catalog(self, device: MobileDevice) -> None:
        """Отдаёт каталог команд из реестра ядра (issue #23). Пусто — кадр не шлём."""
        catalog = await asyncio.to_thread(collect_command_catalog)
        if not catalog:
            logger.info(
                "hermes-mobile: каталог команд недоступен — приложение останется на снимке сборки",
            )
            return
        await self._push(device, proto.commands_frame(catalog))

    async def _send_model_options(self, device: MobileDevice) -> None:
        """Отдаёт список моделей для выбора из инвентаря ядра (issue #23)."""
        options = await asyncio.to_thread(collect_model_options)
        if options is None:
            logger.info("hermes-mobile: список моделей недоступен — приложение предложит ввод вручную")
            return
        provider, current, models = options
        await self._push(device, proto.models_frame(provider, current, models))

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
                logger.debug("hermes-mobile: could not return avatar to idle", exc_info=True)
            finally:
                self._idle_tasks.pop(device.chat_id, None)

        self._idle_tasks[device.chat_id] = asyncio.create_task(_idle_later())


# ── регистрация платформы ────────────────────────────────────────────────────

def check_requirements() -> bool:
    """ПАССИВНАЯ проверка зависимостей: пакет ``websockets`` доступен сейчас?

    Контракт Hermes: проверка пассивная — её свободно вызывают индикаторы статуса.
    ``websockets`` входит в окружение Hermes, поэтому установка не требуется.
    """
    return importlib.util.find_spec("websockets") is not None


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
            ("MOBILE_ALLOWED_DEVICES", "allowed_devices", lambda v: proto.split_tokens(v)),
            ("MOBILE_EXTRA_TOKENS", "extra_tokens", lambda v: proto.split_tokens(v)),
            ("MOBILE_DELIVER_TOKENS", "deliver_tokens", lambda v: proto.split_tokens(v)),
        ),
        home_env="MOBILE_HOME_DEVICE",
        home_default=home_device or None,
    )
    return {"token": token, **seed}


def _model_name(item: Any) -> str:
    """Название модели из строки инвентаря: строка, либо словарь с name/id/model."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("name", "id", "model"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def collect_command_catalog() -> List[Dict[str, Any]]:
    """Каталог команд из реестра ядра: имена, аргументы, описания, категории.

    Ничего не выдумываем: если реестр недоступен (другая сборка ядра), возвращаем пусто —
    приложение тогда работает на вшитом снимке (issue #19).
    """
    try:
        from hermes_cli.commands import COMMAND_REGISTRY
    except Exception as exc:  # noqa: BLE001 — каталог не критичен для разговора
        logger.debug("hermes-mobile: реестр команд недоступен — %s", exc)
        return []

    catalog: List[Dict[str, Any]] = []
    for command in COMMAND_REGISTRY:
        # Команды только для терминала в мессенджере не работают — не показываем.
        if getattr(command, "cli_only", False):
            continue
        try:
            description = command.describe()
        except Exception:  # noqa: BLE001 — описания бывают локализованными
            description = str(getattr(command, "description", "") or "")
        catalog.append(
            {
                "name": str(getattr(command, "name", "") or ""),
                "args": str(getattr(command, "args_hint", "") or ""),
                "description": description,
                "category": str(getattr(command, "category", "") or ""),
                "aliases": [str(alias) for alias in (getattr(command, "aliases", ()) or ())],
            }
        )
    return [item for item in catalog if item["name"]]


def collect_model_options() -> Optional[Tuple[str, str, List[str]]]:
    """Модели для выбора из инвентаря ядра: (провайдер, действующая модель, названия).

    Тот же источник, что у десктопа (``/api/model/options``): каталог провайдера с диска,
    без обращения к сети — тяжёлую работу инвентарь делает в фоне.
    """
    try:
        from hermes_cli.inventory import build_model_options_payload, load_picker_context
    except Exception as exc:  # noqa: BLE001
        logger.debug("hermes-mobile: инвентарь моделей недоступен — %s", exc)
        return None

    payload = build_model_options_payload(load_picker_context(), include_unconfigured=True)
    provider = str(payload.get("provider") or "")
    current = str(payload.get("model") or "")

    names: List[str] = []
    seen = set()
    for row in payload.get("providers") or []:
        if not isinstance(row, dict):
            continue
        # Пока показываем модели действующего провайдера: переключение провайдеров —
        # отдельный шаг, в кадре едет один провайдер.
        if provider and str(row.get("slug") or "") != provider:
            continue
        for item in row.get("models") or []:
            name = _model_name(item)
            if name and name not in seen:
                seen.add(name)
                names.append(name)

    if not names:
        return None
    return provider, current, names


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
        return {"error": "MOBILE_TOKEN is not set"}
    host = str(_env_or_extra(extra, "MOBILE_HOST", "host", "127.0.0.1") or "127.0.0.1")
    port = coerce_port(_env_or_extra(extra, "MOBILE_PORT", "port", DEFAULT_PORT), DEFAULT_PORT)
    raw_path = "/" + str(_env_or_extra(extra, "MOBILE_PATH", "path", DEFAULT_PATH) or DEFAULT_PATH).strip().strip("/")
    if host in ("0.0.0.0", "::"):  # локальная доставка — идём на loopback
        host = "127.0.0.1"
    try:
        import websockets
    except ImportError as exc:
        return {"error": f"websockets is unavailable: {exc}"}
    url = f"ws://{host}:{port}{raw_path}"
    message_id = uuid.uuid4().hex
    try:
        async with websockets.connect(url, max_size=proto.MAX_FRAME_BYTES, open_timeout=10) as websocket:
            await websocket.send(
                proto.dumps(
                    {
                        "t": proto.C_HELLO,
                        "token": token,
                        "device": {"id": "standalone", "name": "Hermes (outside the gateway process)"},
                    }
                )
            )
            ready = proto.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            if proto.frame_type(ready) != proto.S_READY:
                return {"error": str(ready.get("message") or "the gateway refused the connection")}
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
            missing: List[str] = []
            for descriptor in list(media_files or []):
                # Ядро передаёт вложение парой (путь, признак голоса), а не строкой. Раньше
                # путь не распаковывался: Path(str(('путь', False))) файла не находил, и
                # вложение молча не уходило.
                if isinstance(descriptor, (list, tuple)):
                    raw_path = descriptor[0] if descriptor else ""
                    is_voice = bool(descriptor[1]) if len(descriptor) > 1 else False
                else:
                    raw_path = descriptor
                    is_voice = False
                file_path = Path(str(raw_path))
                if not file_path.is_file():
                    # Вложение нельзя прочитать из процесса отправки. Раньше оно молча
                    # пропускалось, а отправитель докладывал успех: агент считал файл
                    # отправленным, человек его не получал (и причины не видел никто).
                    # Теперь честная ошибка — агент может повторить с доступным путём.
                    logger.warning(
                        "hermes-mobile: attachment %s is unavailable to the sending process",
                        raw_path,
                    )
                    missing.append(str(raw_path))
                    continue
                payload = file_path.read_bytes()
                if len(payload) > proto.MAX_MEDIA_BYTES:
                    return {"error": f"attachment {file_path.name} exceeds {proto.MAX_MEDIA_BYTES // (1024 * 1024)} MB"}
                mime = proto.guess_mime(file_path.name)
                if mime.startswith("image/"):
                    kind = "image"
                elif is_voice and mime.startswith("audio/"):
                    kind = "voice"
                else:
                    kind = "document"
                await websocket.send(
                    proto.dumps(
                        {
                            "t": proto.C_DELIVER,
                            "id": f"{message_id}:media:{file_path.name}",
                            "target": chat_id,
                            "text": "" if force_document or kind == "document" else message,
                            "media": {
                                "kind": "document" if force_document else kind,
                                "mime": proto.guess_mime(file_path.name),
                                "filename": file_path.name,
                                "data": base64.b64encode(payload).decode(),
                            },
                        }
                    )
                )
            if missing:
                return {
                    "error": (
                        "attachment was not delivered (the message text was): the file is not "
                        f"available to the sending process — {', '.join(missing)}. If the file "
                        "lives outside the agent home (HERMES_HOME), put a copy there and resend."
                    )
                }
            return {"success": True, "message_id": message_id}
    except Exception as exc:
        return {"error": f"gateway unreachable ({exc.__class__.__name__}: {exc})"}


def interactive_setup() -> None:
    """``hermes gateway setup``: интерактивная настройка платформы."""
    from hermes_cli.setup import prompt, prompt_yes_no, save_env_value, print_header, print_info, print_success

    print_header(i18n.t("setup.header"))
    print_info(i18n.t("setup.intro"))
    token = prompt(i18n.t("setup.token_prompt"), password=True).strip()
    if not token:
        token = os.urandom(32).hex()
        print_info(i18n.t("setup.token_generated", token=token))
    save_env_value("MOBILE_TOKEN", token)
    host = prompt(i18n.t("setup.host_prompt"), default=DEFAULT_HOST).strip() or DEFAULT_HOST
    save_env_value("MOBILE_HOST", host)
    port = prompt(i18n.t("setup.port_prompt"), default=str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
    save_env_value("MOBILE_PORT", port)
    if prompt_yes_no(i18n.t("setup.restrict_devices"), default=False):
        save_env_value(
            "MOBILE_ALLOWED_DEVICES",
            prompt(i18n.t("setup.devices_prompt"), default="").strip(),
        )
    if prompt_yes_no(i18n.t("setup.restrict_users"), default=False):
        save_env_value(
            "MOBILE_ALLOWED_USERS",
            prompt(i18n.t("setup.users_prompt"), default="").strip(),
        )
    save_env_value("MOBILE_HOME_DEVICE", prompt(i18n.t("setup.home_device_prompt"), default="").strip())
    print_success(i18n.t("setup.success"))


def register(ctx) -> None:
    """Точка входа плагина: регистрирует платформенный адаптер в реестре Hermes."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Mobile App (WebSocket)",
        adapter_factory=lambda config: HermesMobileAdapter(config),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["MOBILE_TOKEN"],
        install_hint="websockets ships with Hermes — update Hermes if it is missing",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        allowed_users_env="MOBILE_ALLOWED_USERS",
        allow_all_env="MOBILE_ALLOW_ALL_USERS",
        is_connected=validate_config,
        parse_target_ref_fn=parse_target_ref,
        validate_target_ref_fn=validate_target_ref,
        max_message_length=proto.DEFAULT_CHUNK_LIMIT,
        emoji="📱",
        pii_safe=True,
        cron_deliver_env_var="MOBILE_HOME_DEVICE",
        standalone_sender_fn=standalone_send,
        platform_hint=(
            "The user reads the reply in a mobile app on a small screen. Reply in the user's "
            "language and stay to the point; long tables and wall-of-log dumps do not fit — "
            "prefer short paragraphs and lists. The reply is voiced by an avatar, so avoid "
            "excessive emoji and service notes."
        ),
    )
    logger.debug("hermes-mobile: platform registered")
