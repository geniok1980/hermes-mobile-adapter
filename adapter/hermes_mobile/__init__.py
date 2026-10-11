"""Пакет плагина hermes-mobile (платформа ``mobile``): WebSocket-адаптер для мобильных приложений.

Точка входа — ``register(ctx)`` из ``adapter``. ``__init__`` держим лёгким:
тяжёлые импорты (``websockets``) подтягиваются внутри ``connect()``/``register()``.
"""

from .adapter import register

__all__ = ["register"]
