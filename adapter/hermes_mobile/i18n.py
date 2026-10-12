"""Localization for the few strings the adapter itself forms.

Scope is deliberately narrow: the interactive setup wizard, the button labels
of prompt cards, and the errors the adapter sends back to a client. Everything
else is English-only on purpose — the protocol messages, all ``logger.*``
output and the static ``plugin.yaml`` never pass through here.

English is the default; Russian is an opt-in locale. The locale comes from the
``MOBILE_LOCALE`` environment variable; any unsupported value falls back to
English. There is one catalog per language and a single ``t(key)`` lookup, so
adding a string means adding it to both dictionaries.
"""

from __future__ import annotations

import os

DEFAULT_LOCALE = "en"
LOCALE_ENV = "MOBILE_LOCALE"

_CATALOG = {
    "en": {
        # Interactive setup wizard (``hermes gateway setup``).
        "setup.header": "Mobile App (WebSocket)",
        "setup.intro": "The app token protects the WebSocket connection from your phone.",
        "setup.token_prompt": "App token (empty — generate one)",
        "setup.token_generated": "Generated token: {token} — paste it into the app.",
        "setup.host_prompt": "Listen address",
        "setup.port_prompt": "Port",
        "setup.restrict_devices": "Restrict the device list?",
        "setup.devices_prompt": "Device ids, comma-separated",
        "setup.restrict_users": "Restrict the user list (Hermes authorization)?",
        "setup.users_prompt": "User ids, comma-separated",
        "setup.home_device_prompt": "Default device id for cron",
        "setup.success": "Mobile App platform configured. Restart the gateway: hermes gateway restart",
        # Prompt card buttons.
        "label.approve_once": "✅ Approve once",
        "label.approve_always": "🔒 Always approve",
        "label.cancel": "❌ Cancel",
        "label.clarify_other": "✏️ Other answer",
        # Errors sent to a client.
        "err.unknown_path": "Unknown path {path}",
        "err.first_frame_hello": "The first frame must be hello",
        "err.bad_token": "Invalid app token",
        "err.device_not_allowed": "Device is not in the allow list",
        "err.repeated_hello": "Repeated hello in a single connection",
        "err.token_bound": "Token is bound to another device ({device})",
        "err.device_connected": "Device {device} is already connected",
        "err.prompt_other_device": "This prompt belongs to another device",
        "err.empty_target": "Empty target: specify a device, e.g. mobile:tel-1",
        "err.too_many_uploads": "Already {limit} unfinished uploads — wait for them to finish",
        "err.total_upload_too_big": "Total upload size exceeds {limit} MB",
        "err.file_too_big": "File exceeds {limit} MB — shrink or compress it",
        "err.attachment_too_big": "Attachment exceeds {limit} MB",
        "err.empty_attachment": "Empty attachment",
        "err.media_bad_base64": "media.data is not valid base64",
        "err.deliver_not_allowed": (
            "The deliver frame is accepted only from a cron sender (loopback connection) "
            "or with a token from MOBILE_DELIVER_TOKENS"
        ),
        "err.deliver_empty": "The deliver frame has neither text nor media",
        "err.device_offline": "Device {device} is not connected",
        "err.device_default": "<default>",
    },
    "ru": {
        "setup.header": "Mobile App (WebSocket)",
        "setup.intro": "Токен приложения защищает WebSocket-подключение вашего телефона.",
        "setup.token_prompt": "Токен приложения (пусто — сгенерировать)",
        "setup.token_generated": "Сгенерирован токен: {token} — вставьте его в приложение.",
        "setup.host_prompt": "Адрес прослушивания",
        "setup.port_prompt": "Порт",
        "setup.restrict_devices": "Ограничить список устройств?",
        "setup.devices_prompt": "ID устройств через запятую",
        "setup.restrict_users": "Ограничить список пользователей (авторизация Hermes)?",
        "setup.users_prompt": "ID пользователей через запятую",
        "setup.home_device_prompt": "ID устройства по умолчанию для cron",
        "setup.success": "Платформа Mobile App настроена. Перезапустите шлюз: hermes gateway restart",
        "label.approve_once": "✅ Одобрить один раз",
        "label.approve_always": "🔒 Всегда одобрять",
        "label.cancel": "❌ Отмена",
        "label.clarify_other": "✏️ Свой ответ",
        "err.unknown_path": "Неизвестный путь {path}",
        "err.first_frame_hello": "Первым кадром должен быть hello",
        "err.bad_token": "Неверный токен приложения",
        "err.device_not_allowed": "Устройство не в списке разрешённых",
        "err.repeated_hello": "Повторное приветствие в одном соединении",
        "err.token_bound": "Токен привязан к другому устройству ({device})",
        "err.device_connected": "Устройство {device} уже подключено",
        "err.prompt_other_device": "Эта карточка принадлежит другому устройству",
        "err.empty_target": "Пустой адрес: укажите устройство, например mobile:tel-1",
        "err.too_many_uploads": "Незавершённых загрузок уже {limit} — дождитесь завершения",
        "err.total_upload_too_big": "Суммарный размер загрузки больше {limit} МБ",
        "err.file_too_big": "Файл больше {limit} МБ — сократите или сожмите его",
        "err.attachment_too_big": "Вложение больше {limit} МБ",
        "err.empty_attachment": "Пустое вложение",
        "err.media_bad_base64": "Поле media.data не является корректным base64",
        "err.deliver_not_allowed": (
            "Кадр deliver принимается только от cron-отправителя (loopback-соединение) "
            "или с токеном из MOBILE_DELIVER_TOKENS"
        ),
        "err.deliver_empty": "В кадре deliver нет ни text, ни media",
        "err.device_offline": "Устройство {device} не подключено",
        "err.device_default": "<по умолчанию>",
    },
}


def current_locale() -> str:
    """Locale from ``MOBILE_LOCALE`` (default ``en``); anything unknown means English."""
    raw = str(os.environ.get(LOCALE_ENV, "") or "").strip().lower().replace("-", "_")
    base = raw.split("_", 1)[0]
    return base if base in _CATALOG else DEFAULT_LOCALE


def t(key: str, **kwargs) -> str:
    """Return ``key`` in the current locale, falling back to English, then to ``key``."""
    catalog = _CATALOG.get(current_locale(), _CATALOG[DEFAULT_LOCALE])
    template = catalog.get(key) or _CATALOG[DEFAULT_LOCALE].get(key, key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template
