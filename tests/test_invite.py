"""Установка без ручной настройки: плагин сам заводит токен и присылает карточку в Telegram.

Проверки идут по-настоящему: поднимается сервер адаптера, а Telegram подменяется
локальным HTTP-сервером, который принимает `sendMessage`. Секрет в журнал не попадает,
повторно на тот же токен карточка не уходит.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gateway.config import PlatformConfig  # noqa: E402

from hermes_mobile import adapter as mobile_adapter  # noqa: E402

from test_adapter_ws import _free_port, _register_platform, RecorderAdapter  # noqa: E402

BOT = "123456:test-bot-token"
CHAT = "8288589227"


class FakeTelegram:
    """Локальный «Telegram»: запоминает тела sendMessage и отвечает ok."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                try:
                    outer.messages.append(json.loads(body))
                except json.JSONDecodeError:
                    outer.messages.append({"raw": body})
                payload = b'{"ok":true,"result":{"message_id":1}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # тишина в выводе тестов
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeTelegram":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def _make_adapter(extra: dict) -> RecorderAdapter:
    return RecorderAdapter(PlatformConfig(enabled=True, extra=dict(extra)))


class InviteBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Свой дом профиля на каждый тест: иначе генератор токена пишет в настоящий .env.
        self._home_backup = os.environ.get("HERMES_HOME")
        self.home = tempfile.mkdtemp(prefix="mobile-invite-home-")
        os.environ["HERMES_HOME"] = self.home
        self._env_backup = {key: os.environ.get(key) for key in ("MOBILE_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL")}
        for key in self._env_backup:
            os.environ.pop(key, None)
        _register_platform()
        self.telegram = FakeTelegram().__enter__()
        self.port = _free_port()
        self.extra = {
            "host": "127.0.0.1",
            "port": self.port,
            "path": "/ws",
            "agent_name": "Агент дома",
            "telegram_api": f"http://127.0.0.1:{self.telegram.port}",
            "invite_chat": CHAT,
        }
        os.environ["TELEGRAM_BOT_TOKEN"] = BOT
        self.adapters: list = []

    async def asyncTearDown(self) -> None:
        for adapter in self.adapters:
            await adapter.disconnect()
        self.telegram.__exit__()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._home_backup is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._home_backup
        shutil.rmtree(self.home, ignore_errors=True)

    async def start(self, **extra) -> RecorderAdapter:
        adapter = _make_adapter({**self.extra, **extra})
        self.adapters.append(adapter)
        self.assertTrue(await adapter.connect())
        await self.wait_invite(adapter)
        return adapter

    async def wait_invite(self, adapter) -> None:
        task = getattr(adapter, "_invite_task", None)
        if task is not None:
            await asyncio.wait_for(task, timeout=10)

    def env_path(self):
        from hermes_mobile.adapter import _hermes_env_path
        return _hermes_env_path()


class TestAutoToken(InviteBase):
    async def test_token_is_generated_and_saved_to_env(self):
        adapter = await self.start()
        self.assertEqual(len(adapter.token), 64)
        self.assertTrue(adapter.token_generated)
        content = self.env_path().read_text(encoding="utf-8")
        self.assertIn(f"MOBILE_TOKEN={adapter.token}", content)
        self.assertEqual(oct(self.env_path().stat().st_mode)[-3:], "600")

    async def test_auto_token_can_be_disabled(self):
        adapter = _make_adapter({**self.extra, "auto_token": False})
        self.adapters.append(adapter)
        self.assertFalse(await adapter.connect())
        self.assertIn("MOBILE_TOKEN", adapter._fatal_error_message or "")
        self.assertFalse(self.env_path().exists() and "MOBILE_TOKEN=" in self.env_path().read_text(encoding="utf-8"))

    async def test_token_is_taken_from_env_file_without_regenerating(self):
        """Перезапуск без загруженного .env не должен выпускать новый токен."""
        saved = "e" * 64
        from hermes_mobile.adapter import _hermes_env_path
        _hermes_env_path().write_text(f"OTHER=1\nMOBILE_TOKEN={saved}\n", encoding="utf-8")
        adapter = await self.start()
        self.assertEqual(adapter.token, saved)
        self.assertFalse(adapter.token_generated)
        self.assertEqual(len(self.telegram.messages), 0)

    async def test_token_is_not_written_to_logs(self):
        records: list[str] = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("hermes_mobile.adapter")
        handler = Collector()
        logger.addHandler(handler)
        try:
            adapter = await self.start()
        finally:
            logger.removeHandler(handler)
        self.assertTrue(adapter.token)
        self.assertFalse(any(adapter.token in message for message in records))


class TestConnectionCard(InviteBase):
    async def test_card_is_sent_once_with_address_and_token(self):
        adapter = await self.start()
        self.assertEqual(len(self.telegram.messages), 1)
        message = self.telegram.messages[0]
        self.assertEqual(str(message["chat_id"]), CHAT)
        self.assertIn(adapter.token, message["text"])
        self.assertIn("ws://", message["text"])
        self.assertIn("Агент дома", message["text"])

        marker = json.loads((mobile_adapter._get_hermes_home() and (self.env_path().parent / "mobile-invite.json")).read_text(encoding="utf-8"))
        self.assertTrue(marker["sent"])

        # Повторный старт с тем же токеном — новой карточки нет.
        await adapter.disconnect()
        again = await self.start(token=adapter.token)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertFalse(again.token_generated)

    async def test_rotated_token_sends_new_card(self):
        first = await self.start()
        self.assertEqual(len(self.telegram.messages), 1)
        await first.disconnect()
        rotated = "f" * 64
        await self.start(token=rotated)
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertIn(rotated, self.telegram.messages[1]["text"])
        self.assertNotEqual(first.token, rotated)

    async def test_existing_token_on_first_run_is_silent(self):
        # Обновление плагина на работающем агенте не должно вываливать токен в чат.
        token = "a" * 64
        adapter = await self.start(token=token)
        self.assertEqual(len(self.telegram.messages), 0)
        marker = json.loads((self.env_path().parent / "mobile-invite.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["token_fingerprint"], mobile_adapter._fingerprint(token))
        self.assertFalse(marker["sent"])
        self.assertEqual(adapter.token, token)

    async def test_invite_can_be_disabled(self):
        await self.start(invite=False)
        self.assertEqual(len(self.telegram.messages), 0)

    async def test_missing_telegram_does_not_break_start(self):
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        adapter = await self.start(invite_chat="")
        self.assertTrue(adapter.token)
        self.assertEqual(len(self.telegram.messages), 0)


class TestCardText(unittest.TestCase):
    def test_card_mentions_rotation(self):
        text = mobile_adapter.invite_text("Агент", "b" * 64, "192.168.1.7", 8787, "/ws")
        self.assertIn("ws://192.168.1.7:8787/ws", text)
        self.assertIn("b" * 64, text)
        self.assertIn("MOBILE_TOKEN", text)
        self.assertTrue("rotate" in text.lower() or "перевыпустить" in text.lower())

    def test_russian_locale_card(self):
        os.environ["MOBILE_LOCALE"] = "ru"
        try:
            text = mobile_adapter.invite_text("Агент", "d" * 64, "192.168.1.7", 8787, "/ws")
        finally:
            os.environ.pop("MOBILE_LOCALE", None)
        self.assertIn("готов к подключению", text)
        self.assertIn("перевыпустить", text.lower())

    def test_card_without_address_still_has_hint(self):
        text = mobile_adapter.invite_text("Агент", "c" * 64, "", 8787, "/ws")
        self.assertIn("8787/ws", text)


class TestSetupWizard(unittest.TestCase):
    """Мастер настройки: генерирует токен, печатает карточку и предлагает отправить её."""

    def test_wizard_generates_token_and_offers_card(self):
        from unittest import mock

        saved: dict[str, str] = {}
        printed: list[str] = []
        sent: list[str] = []

        def fake_prompt(_text, default="", **_kwargs):
            return default  # токен — пусто, значит генерировать; остальное по умолчанию

        def fake_yes_no(_text, default=False, **_kwargs):
            return True

        def fake_save(key, value):
            saved[key] = value

        with mock.patch("hermes_cli.setup.prompt", fake_prompt), \
             mock.patch("hermes_cli.setup.prompt_yes_no", fake_yes_no), \
             mock.patch("hermes_cli.setup.save_env_value", fake_save), \
             mock.patch("hermes_cli.setup.print_header", lambda text: printed.append(str(text))), \
             mock.patch("hermes_cli.setup.print_info", lambda text: printed.append(str(text))), \
             mock.patch("hermes_cli.setup.print_success", lambda text: printed.append(str(text))), \
             mock.patch.object(mobile_adapter, "telegram_target", lambda *_a, **_k: ("bot", "chat")), \
             mock.patch.object(mobile_adapter, "send_telegram_card", lambda api, bot, chat, text, **kw: (sent.append(text) or (True, ""))):
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                mobile_adapter.interactive_setup()

        token = saved.get("MOBILE_TOKEN", "")
        self.assertEqual(len(token), 64)
        self.assertEqual(len(sent), 1)
        self.assertIn(token, sent[0])
        self.assertIn("ws://", sent[0])
        self.assertTrue(printed)


if __name__ == "__main__":
    unittest.main()
