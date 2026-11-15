"""Ограничение доступа на входе: чужой пользователь не доходит до агента.

Ядро на свежих версиях не сверяет allowlist для платформы, которая авторизует
соединение сама, поэтому MOBILE_ALLOWED_USERS проверяет адаптер — до создания
события. Проверяем по-настоящему: сервер адаптера + клиент, и отказ виден кадром
ошибки, а не молчанием.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websockets  # noqa: E402

from test_adapter_ws import BaseWS  # noqa: E402

from hermes_mobile import protocol as proto  # noqa: E402


class TestIntakeRestriction(BaseWS):
    async def _connect(self):
        await self.adapter.connect()
        websocket = await websockets.connect(self.url())
        await self.hello(websocket)
        return websocket

    async def test_stranger_gets_error_frame_and_no_event(self):
        self.adapter.allowed_users = ["someone-else"]
        websocket = await self._connect()
        try:
            await websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "s1", "text": "привет"}))
            frame = proto.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
            self.assertEqual(frame["t"], proto.S_ERROR)
            self.assertEqual(frame["code"], proto.E_FORBIDDEN)
            await asyncio.sleep(0.3)
            self.assertEqual(self.adapter.events, [])
        finally:
            await websocket.close()

    async def test_allowed_user_passes(self):
        self.adapter.allowed_users = ["u-1"]
        websocket = await self._connect()
        try:
            await websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "s2", "text": "привет"}))
            event = await self.adapter.next_event()
            self.assertEqual(event.text, "привет")
        finally:
            await websocket.close()

    async def test_without_allowlist_any_token_holder_passes(self):
        websocket = await self._connect()
        try:
            await websocket.send(proto.dumps({"t": proto.C_TEXT, "id": "s3", "text": "привет"}))
            event = await self.adapter.next_event()
            self.assertEqual(event.text, "привет")
        finally:
            await websocket.close()


if __name__ == "__main__":
    unittest.main()
