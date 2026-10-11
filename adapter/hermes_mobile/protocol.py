"""Протокол обмена между мобильным приложением и адаптером Hermes Agent.

Модуль намеренно **не зависит от Hermes**: его импортируют и адаптер, и тесты, и
(через документацию ``docs/protocol.md``) клиентская часть приложения. Вся арифметика
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

# ── Типы кадров: сервер → клиент ─────────────────────────────────────────────
S_READY = "ready"        # {t, v, session, device, avatar_url, capabilities}
S_TYPING = "typing"      # {t}
S_MESSAGE = "message"    # {t, id, text, chunk:{index,total}, final}
S_AVATAR = "avatar"      # {t, state:"idle"|"thinking"|"speaking", mood?}
S_ERROR = "error"        # {t, code, message, fatal}
S_PONG = "pong"          # {t, ts}
S_DELIVERED = "delivered"  # {t, id, device} — ответ на служебную доставку (standalone)

AVATAR_STATES = ("idle", "thinking", "speaking")

# Коды ошибок протокола (стабильные — клиент показывает их пользователю).
E_AUTH_REQUIRED = "auth_required"
E_AUTH_FAILED = "auth_failed"
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
            raise ProtocolError(E_BAD_FRAME, "Кадр не является корректным UTF-8", fatal=True) from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError(E_TOO_BIG, "Кадр слишком большой", fatal=True)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(E_BAD_FRAME, "Кадр не является корректным JSON") from exc
    if not isinstance(data, dict):
        raise ProtocolError(E_BAD_FRAME, "Кадр должен быть JSON-объектом")
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
        raise ProtocolError(E_BAD_FRAME, "В кадре нет поля text")
    text = str(raw)
    if not text.strip():
        raise ProtocolError(E_BAD_FRAME, "Пустое сообщение")
    if len(text) > MAX_TEXT_CHARS:
        raise ProtocolError(
            E_TOO_BIG, f"Сообщение длиннее {MAX_TEXT_CHARS} символов — разбейте его на части"
        )
    return text


def require_audio(frame: Dict[str, Any]) -> Tuple[bytes, str]:
    """Декодирует аудио из кадра ``audio`` → ``(байты, формат)``.

    Формат — только ``wav`` или ``ogg``: это то, что умеет провайдер распознавания речи
    в Hermes; остальное отклоняем с понятной ошибкой, а не молча.
    """
    fmt = str(frame.get("format") or "").strip().lower()
    if fmt not in ("wav", "ogg"):
        raise ProtocolError(E_UNSUPPORTED, "Поддерживаются только форматы wav и ogg")
    data = frame.get("data")
    if not isinstance(data, str) or not data:
        raise ProtocolError(E_BAD_FRAME, "В кадре audio нет поля data (base64)")
    try:
        audio = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError(E_BAD_FRAME, "Поле data не является корректным base64") from exc
    if not audio:
        raise ProtocolError(E_BAD_FRAME, "Пустая аудиозапись")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ProtocolError(
            E_TOO_BIG, f"Аудио длиннее {MAX_AUDIO_BYTES // (1024 * 1024)} МБ — сократите запись"
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
        return "<пусто>"
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
        raise ValueError("limit должен быть положительным")
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
