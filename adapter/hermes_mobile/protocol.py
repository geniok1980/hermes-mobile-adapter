"""Протокол обмена между мобильным приложением и адаптером Hermes Agent.

Модуль намеренно **не зависит от Hermes**: его импортируют и адаптер, и тесты, и
(через документацию ``site-docs/protocol/index.md``) клиентская часть приложения. Вся арифметика
протокола — разбор кадров, проверка токена, нарезка длинного текста — живёт здесь и
покрыта тестами.

Кадр — одно JSON-сообщение WebSocket (UTF-8). Поля:

  * ``t`` — тип кадра (см. ``C_*`` для клиент→сервер и ``S_*`` для сервер→клиент);
  * ``v`` — версия протокола (необязательно, по умолчанию 1);
  * ``id`` — идентификатор сообщения клиента (для идемпотентности и ответов);
  * остальные поля зависят от типа кадра.

Обратная совместимость: новые *необязательные* поля можно добавлять в любой момент,
новые типы кадров — только вместе с ``PROTOCOL_VERSION``; неизвестный тип кадра клиент
обязан игнорировать (``unknown_frame``), а не разрывать соединение.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# ── Ограничения ──────────────────────────────────────────────────────────────
MAX_FRAME_BYTES = 16 * 1024 * 1024       # потолок одного WS-кадра (голос в base64)
MAX_TEXT_CHARS = 12000                   # больше — отклоняем с понятной ошибкой
MAX_AUDIO_BYTES = 8 * 1024 * 1024        # ~8 МБ PCM/WAV — хватает на минуты речи
DEFAULT_CHUNK_LIMIT = 3500               # нарезка ответа агента для мобильного экрана

# ── Типы кадров: клиент → сервер ─────────────────────────────────────────────
C_HELLO = "hello"        # {t, token?, device:{id,name,platform,app_version}, user:{id,name}}
C_TEXT = "text"          # {t, id, text}
C_AUDIO = "audio"        # {t, id, format:"wav"|"ogg", data:<base64>, text?}
C_TYPING = "typing"      # {t} — «пользователь печатает»
C_PING = "ping"          # {t, ts?}
C_ACK = "ack"            # {t, id} — приложение подтвердило показ/проигрывание
C_DELIVER = "deliver"    # {t, id, target, text} — служебная доставка (cron → устройство)
C_PROMPT = "prompt"      # {t, prompt_id, option_id, text?} — ответ на карточку-вопрос
C_MEDIA = "media"        # {t, id, kind, mime, filename?, caption?, data, part?, parts?} — медиа от приложения

# ── Типы кадров: сервер → клиент ─────────────────────────────────────────────
S_READY = "ready"        # {t, v, session, device, avatar_url, capabilities}
S_TYPING = "typing"      # {t, stop?} — «агент печатает» / остановить индикатор
S_MESSAGE = "message"    # {t, id, text, chunk:{index,total}, final, edit?}
S_AVATAR = "avatar"      # {t, state:"idle"|"thinking"|"speaking", mood?}
S_ERROR = "error"        # {t, code, message, fatal}
S_PONG = "pong"          # {t, ts}
# Зарезервировано: адаптер эти кадры пока не отправляет (клиенту можно игнорировать).
S_DELIVERED = "delivered"  # {t, id, device} — подтверждение служебной доставки (cron)
S_PROMPT = "prompt"      # {t, id, kind, text, options:[{id,label,style?}], session, timeout_seconds}
S_PROMPT_DONE = "prompt_done"  # {t, id, option_id?, expired?} — карточка закрыта
S_MEDIA = "media"        # {t, id, kind, mime, filename, caption?, data, part, parts, final, size}

AVATAR_STATES = ("idle", "thinking", "speaking")

# Типы карточек-вопросов (kind в кадре prompt).
P_EXEC_APPROVAL = "exec_approval"
P_SLASH_CONFIRM = "slash_confirm"
P_CLARIFY = "clarify"
PROMPT_KINDS = (P_EXEC_APPROVAL, P_SLASH_CONFIRM, P_CLARIFY)

# Допустимые ответы. Всё, что не в списке, адаптер заменяет на безопасное значение:
# для одобрения опасной команды — deny, для подтверждения слэш-команды — cancel.
EXEC_APPROVAL_OPTIONS = ("once", "session", "always", "deny")
SLASH_CONFIRM_OPTIONS = ("once", "always", "cancel")
CLARIFY_OTHER = "other"

# Медиа: что приложение может прислать агенту и что адаптер может прислать приложению.
INBOUND_MEDIA_KINDS = ("image", "video", "document", "audio")
OUTBOUND_MEDIA_KINDS = ("image", "voice", "video", "document", "animation")
MAX_MEDIA_BYTES = 8 * 1024 * 1024     # один файл (base64 ≈ 10.7 МБ — влезает в кадр)
MEDIA_CHUNK_BYTES = 3 * 1024 * 1024   # размер куска при передаче больших файлов
MIN_MEDIA_CHUNK_BYTES = 4 * 1024       # разумный минимум: ограничивает число частей (память)
DEFAULT_PROMPT_TTL = 300.0            # столько же, сколько ждёт текстовый фолбэк Hermes

# Коды ошибок протокола (стабильные — клиент показывает их пользователю).
# E_AUTH_REQUIRED и E_INTERNAL объявлены заранее: адаптер их пока не отправляет
# (первый кадр не hello → E_BAD_FRAME + закрытие 1008; внутренние сбои закрывают соединение).
E_AUTH_REQUIRED = "auth_required"
E_AUTH_FAILED = "auth_failed"
E_FORBIDDEN = "forbidden"   # действие запрещено этому соединению (чужой device id, deliver не от cron)
E_BAD_FRAME = "bad_frame"
E_UNSUPPORTED = "unsupported_frame"
E_TOO_BIG = "too_big"
E_DEVICE_OFFLINE = "device_offline"
E_INTERNAL = "internal_error"


class ProtocolError(Exception):
    """Кадр не соответствует протоколу. ``code`` — стабильный код для клиента."""

    def __init__(self, code: str, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fatal = fatal

    def to_frame(self) -> Dict[str, Any]:
        return {"t": S_ERROR, "code": self.code, "message": self.message, "fatal": self.fatal}


# ── Кодирование/декодирование ────────────────────────────────────────────────

def dumps(frame: Dict[str, Any]) -> str:
    """Сериализует кадр (без ASCII-экранирования — русский текст читаем в логах)."""
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def loads(raw: Any) -> Dict[str, Any]:
    """Разбирает кадр. Принимает str и bytes (bytes декодируем как UTF-8)."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(E_BAD_FRAME, "Frame is not valid UTF-8", fatal=True) from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError(E_TOO_BIG, "Frame is too large", fatal=True)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(E_BAD_FRAME, "Frame is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProtocolError(E_BAD_FRAME, "Frame must be a JSON object")
    return data


def frame_type(frame: Dict[str, Any]) -> str:
    """Тип кадра в нижнем регистре; пусто, если поля нет."""
    return str(frame.get("t") or "").strip().lower()


def client_id(frame: Dict[str, Any]) -> Optional[str]:
    """Идентификатор сообщения клиента (``id``), если он задан и разумной длины."""
    value = frame.get("id")
    if value is None:
        return None
    text = str(value).strip()
    return text[:128] or None


# ── Валидация полезной нагрузки ──────────────────────────────────────────────

def require_text(frame: Dict[str, Any]) -> str:
    """Текст сообщения из кадра ``text``; пустой текст — ошибка протокола."""
    raw = frame.get("text")
    if raw is None:
        raise ProtocolError(E_BAD_FRAME, "Frame has no text field")
    text = str(raw)
    if not text.strip():
        raise ProtocolError(E_BAD_FRAME, "Empty message")
    if len(text) > MAX_TEXT_CHARS:
        raise ProtocolError(
            E_TOO_BIG, f"Message is longer than {MAX_TEXT_CHARS} characters — split it into parts"
        )
    return text


def require_audio(frame: Dict[str, Any]) -> Tuple[bytes, str]:
    """Декодирует аудио из кадра ``audio`` → ``(байты, формат)``.

    Формат — только ``wav`` или ``ogg``: это то, что умеет провайдер распознавания речи
    в Hermes; остальное отклоняем с понятной ошибкой, а не молча.
    """
    fmt = str(frame.get("format") or "").strip().lower()
    if fmt not in ("wav", "ogg"):
        raise ProtocolError(E_UNSUPPORTED, "Only wav and ogg formats are supported")
    data = frame.get("data")
    if not isinstance(data, str) or not data:
        raise ProtocolError(E_BAD_FRAME, "audio frame has no data field (base64)")
    try:
        audio = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError(E_BAD_FRAME, "data field is not valid base64") from exc
    if not audio:
        raise ProtocolError(E_BAD_FRAME, "Empty audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ProtocolError(
            E_TOO_BIG, f"Audio is longer than {MAX_AUDIO_BYTES // (1024 * 1024)} MB — shorten the recording"
        )
    return audio, fmt


def parse_device(frame: Dict[str, Any]) -> Dict[str, str]:
    """Нормализует блок ``device`` из кадра ``hello`` (все поля — строки, обрезаны)."""
    raw = frame.get("device")
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, str] = {}
    for key in ("id", "name", "platform", "app_version"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            out[key] = str(value).strip()[:120]
    return out


def parse_user(frame: Dict[str, Any]) -> Dict[str, str]:
    """Нормализует блок ``user`` из кадра ``hello``."""
    raw = frame.get("user")
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, str] = {}
    for key in ("id", "name"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            out[key] = str(value).strip()[:120]
    return out


# ── Токены ───────────────────────────────────────────────────────────────────

def split_tokens(raw: Optional[str]) -> List[str]:
    """Разбирает список токенов/устройств из строки (запятая или перевод строки)."""
    if not raw:
        return []
    parts = re.split(r"[,\s]+", str(raw))
    return [p.strip() for p in parts if p.strip()]


def is_authorized(token: Optional[str], expected: Iterable[str]) -> bool:
    """Сравнение токена с ожидаемыми значениями за постоянное время.

    Сравниваем байты, а не строки: ``hmac.compare_digest`` работает только с ASCII, а
    токен из приложения может содержать любые символы (иначе проверка падала бы
    исключением вместо честного отказа).

    Пустой список ожидаемых токенов → отказ (платформа без токена не поднимается:
    это приложение в интернете, а не локальный сокет).
    """
    if not token:
        return False
    candidates = [t for t in expected if t]
    if not candidates:
        return False
    provided = str(token).strip().encode("utf-8")
    return any(hmac.compare_digest(provided, str(candidate).encode("utf-8")) for candidate in candidates)


def redact_token(token: Optional[str]) -> str:
    """Маска токена для логов — сам токен в логи не попадает никогда."""
    if not token:
        return "<empty>"
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…{token[-2:]}"


def device_allowed(device_id: Optional[str], allowed: Sequence[str]) -> bool:
    """Устройство в белом списке? Пустой список = разрешены все устройства с валидным токеном."""
    if not allowed:
        return True
    return bool(device_id) and str(device_id) in set(allowed)


# ── Нарезка длинного текста ──────────────────────────────────────────────────

def chunk_text(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> List[str]:
    """Режет ответ агента на части по ``limit`` символов, не разрывая слова.

    Порядок предпочтений: пустая строка (абзацы) → конец предложения → пробел →
    жёсткий разрез. Никогда не возвращает пустых кусков; короткий текст — один кусок.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    payload = str(text or "")
    if not payload:
        return []
    if len(payload) <= limit:
        return [payload]

    chunks: List[str] = []
    rest = payload
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " "):
            pos = window.rfind(sep)
            if pos > 0:
                cut = pos + (len(sep) if sep != "\n\n" else 1)
                break
        if cut <= 0:
            cut = limit
        piece = rest[:cut].rstrip()
        if not piece:
            piece, cut = rest[:limit], limit
        chunks.append(piece)
        rest = rest[cut:].lstrip() if cut < len(rest) else ""
    if rest:
        chunks.append(rest)
    return chunks


def trim_for_log(text: Optional[str], limit: int = 120) -> str:
    """Короткая однострочная выжимка текста для логов."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ── Медиа ────────────────────────────────────────────────────────────────────

def guess_mime(filename: Optional[str], fallback: str = "application/octet-stream") -> str:
    """MIME по расширению файла (для кадра media); неизвестное — fallback."""
    name = str(filename or "").lower()
    table = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".heic": "image/heic",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".oga": "audio/ogg",
        ".wav": "audio/wav", ".opus": "audio/opus", ".aac": "audio/aac",
        ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
        ".csv": "text/csv", ".json": "application/json", ".zip": "application/zip",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    for ext, mime in table.items():
        if name.endswith(ext):
            return mime
    return fallback


def split_bytes(payload: bytes, chunk_size: int = MEDIA_CHUNK_BYTES) -> List[bytes]:
    """Режет файл на куски для передачи кадрами (большие файлы не влезают в один кадр)."""
    size = max(1, int(chunk_size))
    return [payload[i: i + size] for i in range(0, len(payload), size)] or [b""]


def split_token_bindings(raw: Any) -> Dict[str, str]:
    """Разбирает ``MOBILE_EXTRA_TOKENS``: элемент ``token`` или ``device_id=token``.

    Возвращает ``{token: device_id | ""}``. Привязка не даёт владельцу общего токена
    занять чужой device id и перехватить чужую сессию, ответы и карточки одобрения.
    """
    bindings: Dict[str, str] = {}
    for item in split_tokens(raw):
        device_id, separator, token = item.partition("=")
        if separator and token.strip():
            bindings[token.strip()] = device_id.strip()
        else:
            bindings[item] = ""
    return bindings


def parse_media_upload(frame: Dict[str, Any]) -> Dict[str, Any]:
    """Разбирает кадр ``media`` от приложения.

    Возвращает ``{kind, mime, filename, caption, data, part, parts, message_id}``;
    ``data`` — уже декодированные байты куска. Ошибки — ``ProtocolError``.
    """
    kind = str(frame.get("kind") or "").strip().lower()
    if kind not in INBOUND_MEDIA_KINDS:
        raise ProtocolError(
            E_UNSUPPORTED,
            f"Unsupported media kind {kind!r} (allowed: {', '.join(INBOUND_MEDIA_KINDS)})",
        )
    raw = frame.get("data")
    if not isinstance(raw, str) or not raw:
        raise ProtocolError(E_BAD_FRAME, "media frame has no data field (base64)")
    try:
        payload = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError(E_BAD_FRAME, "data field is not valid base64") from exc
    if not payload:
        raise ProtocolError(E_BAD_FRAME, "Empty file")

    parts = _coerce_int(frame.get("parts"), 1)
    part = _coerce_int(frame.get("part"), 0)
    if parts < 1:
        raise ProtocolError(E_BAD_FRAME, "parts must be ≥ 1")
    # Потолок числа частей: без него клиент заявляет parts=10^9 и держит буферы.
    # Считаем от минимального куска (а не от 3 МБ): клиент вправе резать мельче
    # максимального куска, иначе его отвергли бы на законной загрузке.
    max_parts = -(-MAX_MEDIA_BYTES // MIN_MEDIA_CHUNK_BYTES)
    if parts > max_parts:
        raise ProtocolError(
            E_BAD_FRAME,
            f"parts={parts} exceeds the allowed maximum ({max_parts}) for a "
            f"{MAX_MEDIA_BYTES // (1024 * 1024)} MB file",
        )
    if not 0 <= part < parts:
        raise ProtocolError(E_BAD_FRAME, f"part={part} is out of range for parts={parts}")
    if len(payload) > min(MEDIA_CHUNK_BYTES, MAX_MEDIA_BYTES) + 1024:
        raise ProtocolError(E_TOO_BIG, "Media chunk is too large — reduce the frame size")
    if parts == 1 and len(payload) > MAX_MEDIA_BYTES:
        raise ProtocolError(
            E_TOO_BIG, f"File exceeds {MAX_MEDIA_BYTES // (1024 * 1024)} MB — send it in parts (part/parts)"
        )

    mime = str(frame.get("mime") or "").strip() or guess_mime(frame.get("filename"))
    return {
        "kind": kind,
        "mime": mime[:120],
        "filename": str(frame.get("filename") or "").strip()[:200] or None,
        "caption": str(frame.get("caption") or "")[:MAX_TEXT_CHARS],
        "data": payload,
        "part": part,
        "parts": parts,
        "message_id": client_id(frame) or uuid_hex(),
    }


def parse_prompt_response(frame: Dict[str, Any]) -> Dict[str, Any]:
    """Разбирает кадр-ответ на карточку: ``{prompt_id, option_id, text}``."""
    prompt_id = str(frame.get("prompt_id") or "").strip()
    if not prompt_id:
        raise ProtocolError(E_BAD_FRAME, "prompt frame has no prompt_id")
    option_id = str(frame.get("option_id") or "").strip().lower()
    text = str(frame.get("text") or "")
    if not option_id and not text.strip():
        raise ProtocolError(E_BAD_FRAME, "prompt frame has neither option_id nor text")
    return {"prompt_id": prompt_id[:128], "option_id": option_id[:64], "text": text}


def option_allowed(kind: str, option_id: str) -> bool:
    """Проверяет ответ по типу карточки. Неизвестное значение должно стать безопасным.

    Для одобрения опасной команды безопасное значение — ``deny``, для подтверждения
    слэш-команды — ``cancel``; адаптер делает эту замену сам, полагаться на клиента нельзя.
    """
    option = str(option_id or "").strip().lower()
    if kind == P_EXEC_APPROVAL:
        return option in EXEC_APPROVAL_OPTIONS
    if kind == P_SLASH_CONFIRM:
        return option in SLASH_CONFIRM_OPTIONS
    if kind == P_CLARIFY:
        return option == CLARIFY_OTHER or (len(option) > 1 and option[0] == "c" and option[1:].isdigit())
    return False


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def uuid_hex() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex
