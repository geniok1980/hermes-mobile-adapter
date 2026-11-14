"""Поиск агента в локальной сети: кадр ``discover`` до авторизации.

Проверки идут по-настоящему: поднимается сервер адаптера, к нему подключается
клиент ``websockets``, кадры идут по сети. Публичный ответ не должен содержать
ни токена, ни списков устройств — кадр отдаётся любому в подсети.
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websockets  # noqa: E402

from test_adapter_ws import BaseWS, TOKEN  # noqa: E402

from hermes_mobile import protocol as proto  # noqa: E402


class TestDiscoverFrame(unittest.TestCase):
    """Чистая сборка кадра — без сервера (работает и без Hermes)."""

    def test_frame_carries_only_public_fields(self):
        frame = proto.discover_frame(
            agent="Агент на кухне",
            profile="kitchen",
            server="kitchen-host",
            port=8787,
            path="/ws",
            adapter_version="1.2.0",
            avatar=True,
        )
        self.assertEqual(frame["t"], proto.S_DISCOVER)
        self.assertEqual(frame["v"], proto.PROTOCOL_VERSION)
        self.assertEqual(frame["platform"], "hermes-mobile")
        self.assertEqual(frame["agent"], {"name": "Агент на кухне", "profile": "kitchen"})
        self.assertEqual(frame["port"], 8787)
        self.assertEqual(frame["path"], "/ws")
        self.assertTrue(frame["requires_token"])
        self.assertTrue(frame["capabilities"]["voice"])

    def test_no_secret_fields(self):
        frame = proto.discover_frame(agent="a", server="h")
        flat = json.dumps(frame, ensure_ascii=False).lower()
        self.assertNotIn("\"token\":", flat)          # сам токен не отдаётся
        self.assertNotIn("allowed", flat)            # белые списки устройств/пользователей
        self.assertNotIn("\"user", flat)
        self.assertNotIn("\"home", flat)             # домашний каталог агента
        self.assertEqual(set(frame), {
            "t", "v", "platform", "adapter", "agent", "server", "port", "path",
            "requires_token", "capabilities",
        })


class TestDiscoveryOverNetwork(BaseWS):
    async def _discover(self, websocket, path: str = "/ws"):
        await websocket.send(proto.dumps({"t": proto.C_DISCOVER}))
        return proto.loads(await asyncio.wait_for(websocket.recv(), timeout=5))

    async def test_discover_before_auth_finds_agent(self):
        await self.adapter.connect()
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as websocket:
            frame = await self._discover(websocket)
        self.assertEqual(frame["t"], proto.S_DISCOVER)
        self.assertEqual(frame["port"], self.port)
        self.assertEqual(frame["path"], "/ws")
        self.assertTrue(frame["requires_token"])
        # Имя есть всегда: имя агента, профиль, имя хоста или «Hermes Agent».
        self.assertTrue(frame["agent"]["name"])
        self.assertTrue(frame["adapter"])

    async def test_discover_answers_on_any_path(self):
        """Приложение перебирает адреса и не знает путь — ответ приходит и не на /ws."""
        await self.adapter.connect()
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/probe") as websocket:
            frame = await self._discover(websocket)
        self.assertEqual(frame["t"], proto.S_DISCOVER)
        self.assertEqual(frame["path"], "/ws")  # настоящий путь приходит в ответе

    async def test_discover_never_returns_token_and_closes(self):
        await self.adapter.connect()
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as websocket:
            await websocket.send(proto.dumps({"t": proto.C_DISCOVER}))
            raw = await asyncio.wait_for(websocket.recv(), timeout=5)
            self.assertNotIn(TOKEN, raw)
            self.assertEqual(proto.loads(raw)["t"], proto.S_DISCOVER)
            # Соединение закрыто после ответа: сканирование не держит сотни сокетов.
            with self.assertRaises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(websocket.recv(), timeout=5)

    async def test_discover_disabled_by_setting(self):
        self.adapter.discovery_enabled = False
        await self.adapter.connect()
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as websocket:
            frame = await self._discover(websocket)
        self.assertEqual(frame["t"], proto.S_ERROR)
        self.assertEqual(frame["code"], proto.E_UNSUPPORTED)

    async def test_discover_rate_limited_per_address(self):
        self.adapter.discovery_burst = 1
        await self.adapter.connect()
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as websocket:
            first = await self._discover(websocket)
        self.assertEqual(first["t"], proto.S_DISCOVER)
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as websocket:
            second = await self._discover(websocket)
        self.assertEqual(second["t"], proto.S_ERROR)
        self.assertEqual(second["code"], proto.E_FORBIDDEN)

    async def test_discover_allows_burst_for_honest_search(self):
        """Честный поиск обращается к агенту несколько раз подряд — это не скан."""
        await self.adapter.connect()
        for _ in range(5):
            async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as websocket:
                frame = await self._discover(websocket)
            self.assertEqual(frame["t"], proto.S_DISCOVER)

    async def test_discover_on_authenticated_connection(self):
        await self.adapter.connect()
        async with websockets.connect(self.url()) as websocket:
            await self.hello(websocket)
            await websocket.send(proto.dumps({"t": proto.C_DISCOVER}))
            frame = await self.recv_frame(websocket, proto.S_DISCOVER)
        self.assertEqual(frame["t"], proto.S_DISCOVER)
        self.assertTrue(frame["requires_token"])

    async def test_hello_after_discover_still_works_on_new_connection(self):
        """Сканирование не мешает обычному подключению с токеном."""
        await self.adapter.connect()
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as websocket:
            await self._discover(websocket)
        async with websockets.connect(self.url()) as websocket:
            ready = await self.hello(websocket)
        self.assertEqual(ready["t"], proto.S_READY)


if __name__ == "__main__":
    unittest.main()
