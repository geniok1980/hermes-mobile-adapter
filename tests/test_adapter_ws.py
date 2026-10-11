"""Живые тесты адаптера hermes-mobile: реальный WebSocket-сервер, реальный клиент.

Ничего не подменяется «наполовину»: поднимается настоящий сервер адаптера
(``websockets.serve``), к нему подключается настоящий клиент, кадры идут по сети
loopback. У агента подменяется только обработчик входящих (``handle_message``) —
в тесте он записывает события, потому что полный прогон агента это отдельный тест
уровня шлюза.

Платформа регистрируется в настоящем реестре через ``register(fake_ctx)``: так
проверяется и то, что наши аргументы регистрации принимаются ``PlatformEntry``.
"""

import asyncio
import base64
import json
import os
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))

import websockets  # noqa: E402

try:  # тесты адаптера требуют платформенный API Hermes; протокольные тесты — нет
    from gateway.config import PlatformConfig  # noqa: E402
    from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

    HAS_HERMES = True
except ImportError:  # pragma: no cover — CI без Hermes
    HAS_HERMES = False
    PlatformConfig = object  # type: ignore[assignment,misc]
    PlatformEntry = object  # type: ignore[assignment,misc]
    platform_registry = None  # type: ignore[assignment]

from hermes_mobile import adapter as mobile_adapter  # noqa: E402
from hermes_mobile import protocol as proto  # noqa: E402

TOKEN = "t" * 64


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _register_platform() -> dict:
    """Регистрирует платформу так же, как это делает менеджер плагинов Hermes."""
    captured: dict = {}

    class FakeCtx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)
            fields = {k: v for k, v in kwargs.items() if k in PlatformEntry.__dataclass_fields__}
            entry = PlatformEntry(**fields)
            platform_registry.register(entry)
            return entry

    mobile_adapter.register(FakeCtx())
    return captured


class RecorderAdapter(mobile_adapter.HermesMobileAdapter):
    """Адаптер, который вместо запуска агента записывает входящие события."""

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.events = []
        self._event_signal = asyncio.Event()

    async def handle_message(self, event):
        self.events.append(event)
        self._event_signal.set()

    async def next_event(self, timeout: float = 5.0):
        for _ in range(50):
            if self.events:
                return self.events[-1]
            self._event_signal.clear()
            try:
                await asyncio.wait_for(self._event_signal.wait(), timeout=timeout / 10)
            except asyncio.TimeoutError:
                continue
        raise AssertionError("адаптер не передал событие агенту")


@unittest.skipUnless(HAS_HERMES, "нужен Hermes Agent (платформенный API)")
class BaseWS(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._hermes_home = tempfile.mkdtemp(prefix="mobile-test-home-")
        os.environ["HERMES_HOME"] = self._hermes_home
        for key in ("MOBILE_TOKEN", "MOBILE_HOST", "MOBILE_PORT", "MOBILE_PATH", "MOBILE_ALLOWED_USERS"):
            os.environ.pop(key, None)
        self.captured = _register_platform()
        self.port = _free_port()
        self.extra = {
            "token": TOKEN,
            "host": "127.0.0.1",
            "port": self.port,
            "path": "/ws",
            "avatar_url": "https://hermes-go.com/avatar/index.html?v=2",
        }
        self.config = PlatformConfig(enabled=True, extra=dict(self.extra))
        self.adapter = RecorderAdapter(self.config)

    async def asyncTearDown(self):
        await self.adapter.disconnect()
        os.environ.pop("HERMES_HOME", None)

    def url(self, token: str | None = TOKEN) -> str:
        base = f"ws://127.0.0.1:{self.port}/ws"
        return f"{base}?token={token}" if token else base

    async def hello(self, websocket, *, token=TOKEN, device_id="dev-1"):
        await websocket.send(
            proto.dumps(
                {
                    "t": proto.C_HELLO,
                    "token": token,
                    "device": {"id": device_id, "name": "Телефон Евгения", "platform": "android", "app_version": "1.0.0"},
                    "user": {"id": "u-1", "name": "Евгений"},
                }
            )
        )
        return proto.loads(await asyncio.wait_for(websocket.recv(), timeout=5))

    async def recv_frame(self, websocket, kind=None, timeout=5.0):
        """Читает кадры до нужного типа (аватар/typing приходят вперемешку с сообщениями)."""
        for _ in range(50):
            frame = proto.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))
            if kind is None or frame.get("t") == kind:
                return frame
        raise AssertionError(f"кадр {kind} не пришёл")


class TestRegistration(BaseWS):
    async def test_register_passes_real_contract(self):
        """register() отдаёт реестру корректные аргументы (иначе PlatformEntry упал бы)."""
        self.assertEqual(self.captured["name"], "mobile")
        self.assertEqual(self.captured["label"], "Mobile App (WebSocket)")
        self.assertEqual(self.captured["required_env"], ["MOBILE_TOKEN"])
        self.assertTrue(self.captured["check_fn"]())  # websockets установлен
        self.assertTrue(callable(self.captured["standalone_sender_fn"]))
        self.assertTrue(callable(self.captured["env_enablement_fn"]))
        self.assertIsInstance(self.captured["adapter_factory"](self.config), mobile_adapter.HermesMobileAdapter)
        self.assertIn("мобильном", self.captured["platform_hint"])

    async def test_platform_enum_resolves(self):
        self.assertEqual(str(mobile_adapter.Platform("mobile").value), "mobile")


class TestConnectAndAuth(BaseWS):
    async def test_connect_starts_server(self):
        self.assertTrue(await self.adapter.connect())
        self.assertIn(f"ws://127.0.0.1:{self.port}/ws", self.adapter.listener_base or "")

    async def test_connect_without_token_fails_descriptively(self):
        self.adapter.token = ""
        self.assertFalse(await self.adapter.connect())
        self.assertIn("MOBILE_TOKEN", self.adapter._fatal_error_message or "")

    async def test_ready_frame_on_valid_token(self):
        await self.adapter.connect()
        async with websockets.connect(self.url()) as websocket:
            ready = await self.hello(websocket)
        self.assertEqual(ready["t"], proto.S_READY)
        self.assertEqual(ready["v"], proto.PROTOCOL_VERSION)
        self.assertEqual(ready["session"], "mobile:dev-1")
        self.assertEqual(ready["avatar_url"], self.extra["avatar_url"])
        self.assertTrue(ready["capabilities"]["voice"])

    async def test_wrong_token_is_rejected_and_closed(self):
        await self.adapter.connect()
        async with websockets.connect(self.url()) as websocket:
            frame = await self.hello(websocket, token="неверный-токен")
            self.assertEqual(frame["t"], proto.S_ERROR)
            self.assertEqual(frame["code"], proto.E_AUTH_FAILED)
            self.assertTrue(frame["fatal"])
            with self.assertRaises(websockets.exceptions.ConnectionClosed) as ctx:
                await asyncio.wait_for(websocket.recv(), timeout=5)
            self.assertEqual(ctx.exception.rcvd.code, mobile_adapter.CLOSE_UNAUTHORIZED)

    async def test_token_from_query_string_is_accepted(self):
        await self.adapter.connect()
        async with websockets.connect(self.url(TOKEN)) as websocket:
            await websocket.send(proto.dumps({"t": proto.C_HELLO, "device": {"id": "dev-q"}}))
            ready = proto.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
        self.assertEqual(ready["t"], proto.S_READY)
        self.assertEqual(ready["session"], "mobile:dev-q")

    async def test_first_frame_must_be_hello(self):
        await self.adapter.connect()
        async with websockets.connect(self.url()) as websocket:
            await websocket.send(proto.dumps({"t": proto.C_TEXT, "text": "привет"}))
            frame = proto.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
        self.assertEqual(frame["code"], proto.E_BAD_FRAME)


class TestInbound(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        self.websocket = await websockets.connect(self.url())
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def test_text_reaches_agent_with_normalized_source(self):
        await self.websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "m1", "text": "Сколько стоит сервер?"}))
        event = await self.adapter.next_event()
        self.assertEqual(event.text, "Сколько стоит сервер?")
        self.assertEqual(event.message_id, "m1")
        self.assertEqual(event.source.chat_id, "mobile:dev-1")
        self.assertEqual(event.source.chat_type, "dm")
        self.assertEqual(event.source.user_id, "u-1")
        self.assertEqual(event.metadata["mobile"]["device_id"], "dev-1")
        self.assertEqual(event.metadata["mobile"]["app_version"], "1.0.0")

    async def test_avatar_goes_thinking_then_idle(self):
        await self.websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "m2", "text": "привет"}))
        frame = proto.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5))
        self.assertEqual((frame["t"], frame["state"]), (proto.S_AVATAR, "thinking"))
        await self.adapter.send("mobile:dev-1", "ответ")
        self.assertEqual(proto.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5))["state"], "speaking")

    async def test_duplicate_message_id_is_ignored(self):
        for _ in range(3):
            await self.websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "dup", "text": "один раз"}))
        await self.adapter.next_event()
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.adapter.events), 1)

    async def test_empty_text_is_rejected_with_error_frame(self):
        await self.websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "m3", "text": "   "}))
        frame = proto.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5))
        self.assertEqual(frame["t"], proto.S_ERROR)
        self.assertEqual(frame["code"], proto.E_BAD_FRAME)
        self.assertEqual(self.adapter.events, [])

    async def test_unknown_frame_is_ignored(self):
        await self.websocket.send(proto.dumps({"t": "какой-то-новый-кадр", "поле": 1}))
        await self.websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "m4", "text": "работает"}))
        event = await self.adapter.next_event()
        self.assertEqual(event.text, "работает")

    async def test_ping_pong(self):
        await self.websocket.send(proto.dumps({"t": proto.C_PING, "ts": 42}))
        frame = proto.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5))
        self.assertEqual(frame, {"t": proto.S_PONG, "ts": 42})

    async def test_voice_message_saved_and_forwarded(self):
        wav = b"RIFF" + b"\x00" * 64 + b"WAVEfmt "
        await self.websocket.send(
            proto.dumps(
                {
                    "t": proto.C_AUDIO,
                    "id": "v1",
                    "format": "wav",
                    "data": base64.b64encode(wav).decode(),
                    "text": "распознанный текст",
                }
            )
        )
        event = await self.adapter.next_event()
        self.assertEqual(str(event.message_type), "MessageType.VOICE")
        self.assertEqual(event.text, "распознанный текст")
        self.assertTrue(event.metadata["mobile"]["voice"])
        self.assertEqual(len(event.media_urls), 1)
        saved = os.path.join(self._hermes_home, "mobile-media", "v1.wav")
        self.assertTrue(os.path.exists(saved))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), wav)

    async def test_two_devices_are_separate_sessions(self):
        second = await websockets.connect(self.url())
        await self.hello(second, device_id="dev-2")
        await second.send(proto.dumps({"t": proto.C_TEXT, "id": "s1", "text": "со второго телефона"}))
        for _ in range(50):
            if self.adapter.events:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(self.adapter.events[-1].source.chat_id, "mobile:dev-2")
        # Ответ уходит именно на второе устройство.
        await self.adapter.send("mobile:dev-2", "ответ второму")
        frame = await self.recv_frame(second, proto.S_MESSAGE)
        self.assertEqual(frame["text"], "ответ второму")
        await second.close()


class TestOutbound(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        self.websocket = await websockets.connect(self.url())
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def test_send_chunks_long_answer(self):
        long_text = "Строка про тарифы и слоты. " * 300
        result = await self.adapter.send("mobile:dev-1", long_text)
        self.assertTrue(result.success)
        self.assertTrue(result.message_id)
        frames = []
        while True:
            frame = await self.recv_frame(self.websocket, proto.S_MESSAGE)
            frames.append(frame)
            if frame["final"]:
                break
        self.assertGreater(len(frames), 1)
        self.assertEqual([f["chunk"]["index"] for f in frames], list(range(len(frames))))
        self.assertEqual(frames[0]["chunk"]["total"], len(frames))
        self.assertEqual(
            "".join("".join(f["text"] for f in frames).split()), "".join(long_text.split())
        )
        self.assertEqual(len({f["id"] for f in frames}), 1)

    async def test_send_to_offline_device_reports_error(self):
        result = await self.adapter.send("mobile:не-подключено", "привет")
        self.assertFalse(result.success)
        self.assertEqual(result.error, proto.E_DEVICE_OFFLINE)

    async def test_typing_indicator(self):
        await self.adapter.send_typing("mobile:dev-1")
        states = []
        for _ in range(2):
            frame = proto.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5))
            states.append((frame["t"], frame.get("state")))
        self.assertIn((proto.S_AVATAR, "thinking"), states)
        self.assertIn((proto.S_TYPING, None), states)

    async def test_chat_info_and_session_lookup_by_raw_id(self):
        info = await self.adapter.get_chat_info("dev-1")  # без префикса mobile:
        self.assertEqual(info, {"name": "Телефон Евгения", "type": "dm"})

    async def test_home_device_fallback(self):
        self.adapter.home_device = "dev-1"
        result = await self.adapter.send("", "в домашнее устройство")
        self.assertTrue(result.success)

    async def test_deliver_frame_routes_to_target(self):
        await self.websocket.send(
            proto.dumps({"t": proto.C_DELIVER, "id": "d1", "target": "dev-1", "text": "напоминание из cron"})
        )
        frame = await self.recv_frame(self.websocket, proto.S_MESSAGE)
        self.assertEqual(frame["text"], "напоминание из cron")

    async def test_deliver_to_unknown_target_returns_error(self):
        await self.websocket.send(
            proto.dumps({"t": proto.C_DELIVER, "id": "d2", "target": "нет-такого", "text": "текст"})
        )
        frame = proto.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5))
        self.assertEqual(frame["code"], proto.E_DEVICE_OFFLINE)

    async def test_standalone_send_end_to_end(self):
        """standalone_sender_fn (cron вне шлюза) реально доставляет сообщение устройству."""
        config = PlatformConfig(enabled=True, extra={"token": TOKEN, "host": "127.0.0.1", "port": self.port, "path": "/ws"})
        result = await mobile_adapter.standalone_send(config, "dev-1", "сообщение из cron")
        self.assertTrue(result["success"], result)
        frame = await self.recv_frame(self.websocket, proto.S_MESSAGE)
        self.assertEqual(frame["text"], "сообщение из cron")

    async def test_standalone_send_without_gateway_reports_error(self):
        config = PlatformConfig(enabled=True, extra={"token": TOKEN, "host": "127.0.0.1", "port": _free_port(), "path": "/ws"})
        result = await mobile_adapter.standalone_send(config, "dev-1", "некому доставить")
        self.assertIn("error", result)
        self.assertFalse(result.get("success"))


class TestAllowedUsers(BaseWS):
    async def test_device_not_in_whitelist_is_rejected(self):
        self.adapter.allowed_users = ["dev-allowed"]
        await self.adapter.connect()
        async with websockets.connect(self.url()) as websocket:
            frame = await self.hello(websocket, device_id="dev-чужой")
            self.assertEqual(frame["code"], proto.E_AUTH_FAILED)


class TestEnvEnablement(BaseWS):
    async def test_env_enablement_seeds_extra(self):
        os.environ["MOBILE_TOKEN"] = "e" * 64
        os.environ["MOBILE_PORT"] = "9999"
        os.environ["MOBILE_ALLOWED_USERS"] = "dev-1, dev-2"
        try:
            seed = mobile_adapter._env_enablement()
        finally:
            for key in ("MOBILE_TOKEN", "MOBILE_PORT", "MOBILE_ALLOWED_USERS"):
                os.environ.pop(key, None)
        self.assertEqual(seed["token"], "e" * 64)
        self.assertEqual(seed["port"], 9999)
        self.assertEqual(seed["allowed_users"], ["dev-1", "dev-2"])

    async def test_env_enablement_returns_none_without_token(self):
        self.assertIsNone(mobile_adapter._env_enablement())

    async def test_validate_config(self):
        self.assertTrue(mobile_adapter.validate_config(self.config))
        self.assertFalse(mobile_adapter.validate_config(PlatformConfig(enabled=True, extra={})))


if __name__ == "__main__":
    unittest.main()
