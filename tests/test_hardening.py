"""Тесты жёсткости и безопасности адаптера hermes-mobile (8 правил).

Проверяется ПОВЕДЕНИЕ и КОДЫ кадров (``proto.E_*``, ``CLOSE_UNAUTHORIZED``), а не
тексты сообщений: тексты параллельно переводят на английский. Существующие тесты и
сам адаптер не изменяются — этот файл только добавляется.

Опорная точка — ``tests/test_adapter_ws.py`` (класс ``BaseWS``): он поднимает
настоящий WebSocket-сервер адаптера на свободном порту и даёт помощники
``url()``/``hello()``/``recv_frame()`` и константу ``TOKEN``.
"""

import asyncio
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))

import websockets  # noqa: E402

from test_adapter_ws import HAS_HERMES, TOKEN, BaseWS  # noqa: E402

from hermes_mobile import adapter as mobile_adapter  # noqa: E402
from hermes_mobile import protocol as proto  # noqa: E402

SECOND_TOKEN = "s" * 64       # свободный дополнительный токен (без привязки)
BOUND_TOKEN = "b" * 64        # токен, привязанный к устройству BOUND_DEVICE
BOUND_DEVICE = "dev-bound"
DELIVER_TOKEN = "d" * 64      # токен из MOBILE_DELIVER_TOKENS


def _media_frame(mid, *, part=0, parts=1, payload=b"x" * 32, kind="image", filename="p.jpg"):
    """Собирает кадр ``media`` от приложения (одна часть)."""
    return proto.dumps(
        {
            "t": proto.C_MEDIA,
            "id": mid,
            "kind": kind,
            "filename": filename,
            "part": part,
            "parts": parts,
            "data": base64.b64encode(payload).decode(),
        }
    )


# ── 1. Токен принимается ТОЛЬКО из кадра hello ───────────────────────────────

class TestTokenSource(BaseWS):
    async def test_token_in_query_string_is_not_authorized(self):
        """?token= в URL не работает: без токена в hello — отказ auth_failed."""
        await self.adapter.connect()
        async with websockets.connect(self.url(TOKEN)) as ws:
            await ws.send(proto.dumps({"t": proto.C_HELLO, "device": {"id": "dev-q"}}))
            frame = proto.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        self.assertEqual(frame["t"], proto.S_ERROR)
        self.assertEqual(frame["code"], proto.E_AUTH_FAILED)

    async def test_token_in_hello_is_accepted_without_query(self):
        await self.adapter.connect()
        async with websockets.connect(self.url(None)) as ws:
            ready = await self.hello(ws)
        self.assertEqual(ready["t"], proto.S_READY)
        self.assertEqual(ready["session"], "mobile:dev-1")


# ── общая основа: адаптер с дополнительными токенами из env ──────────────────

class _ExtraTokens(BaseWS):
    extra_tokens_env = ""
    deliver_tokens_env = ""

    async def asyncSetUp(self):
        os.environ["MOBILE_EXTRA_TOKENS"] = self.extra_tokens_env
        os.environ["MOBILE_DELIVER_TOKENS"] = self.deliver_tokens_env
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        os.environ.pop("MOBILE_EXTRA_TOKENS", None)
        os.environ.pop("MOBILE_DELIVER_TOKENS", None)


# ── 2. MOBILE_EXTRA_TOKENS device_id=token: привязка токена к устройству ─────

class TestTokenBinding(_ExtraTokens):
    extra_tokens_env = f"{BOUND_DEVICE}={BOUND_TOKEN}"

    async def test_binding_is_parsed(self):
        self.assertEqual(self.adapter.token_bindings, {BOUND_TOKEN: BOUND_DEVICE})
        self.assertIn(BOUND_TOKEN, self.adapter.extra_tokens)

    async def test_bound_token_cannot_claim_another_device(self):
        await self.adapter.connect()
        async with websockets.connect(self.url(None)) as ws:
            frame = await self.hello(ws, token=BOUND_TOKEN, device_id="dev-other")
            self.assertEqual(frame["t"], proto.S_ERROR)
            self.assertEqual(frame["code"], proto.E_FORBIDDEN)
            with self.assertRaises(websockets.exceptions.ConnectionClosed) as ctx:
                await asyncio.wait_for(ws.recv(), timeout=5)
            self.assertEqual(ctx.exception.rcvd.code, mobile_adapter.CLOSE_UNAUTHORIZED)

    async def test_bound_token_can_claim_its_device(self):
        await self.adapter.connect()
        async with websockets.connect(self.url(None)) as ws:
            ready = await self.hello(ws, token=BOUND_TOKEN, device_id=BOUND_DEVICE)
        self.assertEqual(ready["t"], proto.S_READY)
        self.assertEqual(ready["session"], f"mobile:{BOUND_DEVICE}")


# ── 3. Занятый device id: другой токен — отказ, тот же — переподключение ─────

class TestDeviceOccupancy(_ExtraTokens):
    extra_tokens_env = SECOND_TOKEN

    async def test_other_token_cannot_take_busy_device(self):
        await self.adapter.connect()
        first = await websockets.connect(self.url(None))
        await self.hello(first, token=TOKEN, device_id="dev-1")
        second = await websockets.connect(self.url(None))
        frame = await self.hello(second, token=SECOND_TOKEN, device_id="dev-1")
        self.assertEqual(frame["code"], proto.E_FORBIDDEN)
        with self.assertRaises(websockets.exceptions.ConnectionClosed) as ctx:
            await asyncio.wait_for(second.recv(), timeout=5)
        self.assertEqual(ctx.exception.rcvd.code, mobile_adapter.CLOSE_UNAUTHORIZED)
        # Первое соединение остаётся живым и работающим.
        await first.send(proto.dumps({"t": proto.C_PING, "ts": 7}))
        pong = proto.loads(await asyncio.wait_for(first.recv(), timeout=5))
        self.assertEqual(pong, {"t": proto.S_PONG, "ts": 7})
        await first.close()

    async def test_same_token_reconnects_and_drops_old_socket(self):
        await self.adapter.connect()
        first = await websockets.connect(self.url(None))
        await self.hello(first, token=TOKEN, device_id="dev-1")
        second = await websockets.connect(self.url(None))
        ready = await self.hello(second, token=TOKEN, device_id="dev-1")
        self.assertEqual(ready["t"], proto.S_READY)
        with self.assertRaises(websockets.exceptions.ConnectionClosed) as ctx:
            await asyncio.wait_for(first.recv(), timeout=5)
        self.assertEqual(ctx.exception.rcvd.code, 1000)
        await second.close()


# ── 4. deliver разрешён только loopback-пиру или токену MOBILE_DELIVER_TOKENS ─

class TestDeliverGate(_ExtraTokens):
    extra_tokens_env = DELIVER_TOKEN
    deliver_tokens_env = DELIVER_TOKEN

    async def test_loopback_peer_may_deliver(self):
        await self.adapter.connect()
        async with websockets.connect(self.url(None)) as ws:
            await self.hello(ws)
            self.assertTrue(self.adapter._deliver_allowed["mobile:dev-1"])

    async def test_not_allowed_sender_gets_forbidden(self):
        await self.adapter.connect()
        original = mobile_adapter._is_loopback
        mobile_adapter._is_loopback = lambda host: False
        try:
            async with websockets.connect(self.url(None)) as ws:
                await self.hello(ws)
                self.assertFalse(self.adapter._deliver_allowed["mobile:dev-1"])
                await ws.send(
                    proto.dumps({"t": proto.C_DELIVER, "id": "d1", "target": "dev-1", "text": "x"})
                )
                frame = await self.recv_frame(ws, proto.S_ERROR)
        finally:
            mobile_adapter._is_loopback = original
        self.assertEqual(frame["code"], proto.E_FORBIDDEN)

    async def test_deliver_token_grants_permission_without_loopback(self):
        await self.adapter.connect()
        original = mobile_adapter._is_loopback
        mobile_adapter._is_loopback = lambda host: False
        try:
            async with websockets.connect(self.url(None)) as ws:
                await self.hello(ws, token=DELIVER_TOKEN, device_id="dev-cron")
                self.assertTrue(self.adapter._deliver_allowed["mobile:dev-cron"])
        finally:
            mobile_adapter._is_loopback = original


# ── 5. Ответ на карточку принимает только её устройство ──────────────────────

class TestPromptOwnership(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        self.owner = await websockets.connect(self.url(None))
        await self.hello(self.owner, device_id="dev-1")
        self.intruder = await websockets.connect(self.url(None))
        await self.hello(self.intruder, device_id="dev-2")

    async def asyncTearDown(self):
        await self.owner.close()
        await self.intruder.close()
        await super().asyncTearDown()

    async def test_other_device_cannot_answer_prompt(self):
        result = await self.adapter.send_exec_approval(
            "mobile:dev-1", "rm -rf /tmp/x", "mobile:dev-1", description="удаление файлов"
        )
        self.assertTrue(result.success)
        card = await self.recv_frame(self.owner, proto.S_PROMPT)
        prompt_id = card["id"]
        self.assertIn(prompt_id, self.adapter._pending_prompts)

        await self.intruder.send(
            proto.dumps({"t": proto.C_PROMPT, "prompt_id": prompt_id, "option_id": "deny"})
        )
        frame = await self.recv_frame(self.intruder, proto.S_ERROR)
        self.assertEqual(frame["code"], proto.E_FORBIDDEN)
        # Карточка остаётся висеть: чужой ответ её не закрыл.
        self.assertIn(prompt_id, self.adapter._pending_prompts)


# ── 6. Карточка exec_approval несёт отдельные command и description ──────────

class TestExecApprovalCard(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        self.websocket = await websockets.connect(self.url(None))
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def test_card_has_separate_command_and_description(self):
        result = await self.adapter.send_exec_approval(
            "mobile:dev-1", "rm -rf /tmp/x", "mobile:dev-1", description="Удаляет временные файлы"
        )
        self.assertTrue(result.success)
        card = await self.recv_frame(self.websocket, proto.S_PROMPT)
        self.assertEqual(card["kind"], proto.P_EXEC_APPROVAL)
        self.assertEqual(card["command"], "rm -rf /tmp/x")
        self.assertEqual(card["description"], "Удаляет временные файлы")
        self.assertTrue(card["command"])
        self.assertTrue(card["description"])
        self.assertNotEqual(card["command"], card["description"])


# ── 7. Лимиты медиа на устройство ────────────────────────────────────────────

class TestMediaHardening(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        self.websocket = await websockets.connect(self.url(None))
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def _wait_open_uploads(self, expected):
        for _ in range(100):
            if len(self.adapter._device_buffers("mobile:dev-1")) >= expected:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("незавершённые загрузки не появились")

    async def test_open_uploads_are_capped(self):
        limit = mobile_adapter.MAX_OPEN_MEDIA_UPLOADS
        for index in range(limit):
            await self.websocket.send(_media_frame(f"up{index}", part=0, parts=2))
        await self._wait_open_uploads(limit)

        await self.websocket.send(_media_frame("up-overflow", part=0, parts=2))
        frame = await self.recv_frame(self.websocket, proto.S_ERROR)
        self.assertEqual(frame["code"], proto.E_TOO_BIG)
        self.assertEqual(len(self.adapter._device_buffers("mobile:dev-1")), limit)

    async def test_single_upload_bytes_capped_and_buffer_dropped(self):
        saved = proto.MAX_MEDIA_BYTES
        proto.MAX_MEDIA_BYTES = 8000
        try:
            chunk = os.urandom(5000)
            await self.websocket.send(_media_frame("big", part=0, parts=2, payload=chunk))
            await self.websocket.send(_media_frame("big", part=1, parts=2, payload=chunk))
            frame = await self.recv_frame(self.websocket, proto.S_ERROR)
            self.assertEqual(frame["code"], proto.E_TOO_BIG)
            self.assertNotIn("mobile:dev-1:big", self.adapter._media_buffers)
        finally:
            proto.MAX_MEDIA_BYTES = saved

    async def test_device_total_bytes_capped(self):
        saved = proto.MAX_MEDIA_BYTES
        proto.MAX_MEDIA_BYTES = 8000
        try:
            chunk = os.urandom(5000)
            await self.websocket.send(_media_frame("a", part=0, parts=2, payload=chunk))
            await self.websocket.send(_media_frame("b", part=0, parts=2, payload=chunk))
            frame = await self.recv_frame(self.websocket, proto.S_ERROR)
            self.assertEqual(frame["code"], proto.E_TOO_BIG)
            self.assertNotIn("mobile:dev-1:b", self.adapter._media_buffers)
            self.assertIn("mobile:dev-1:a", self.adapter._media_buffers)
        finally:
            proto.MAX_MEDIA_BYTES = saved

    async def test_buffers_cleared_when_device_disconnects(self):
        await self.websocket.send(_media_frame("keep", part=0, parts=2))
        for _ in range(100):
            if "mobile:dev-1:keep" in self.adapter._media_buffers:
                break
            await asyncio.sleep(0.05)
        self.assertIn("mobile:dev-1:keep", self.adapter._media_buffers)

        await self.websocket.close()
        for _ in range(100):
            if not self.adapter._device_buffers("mobile:dev-1"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.adapter._device_buffers("mobile:dev-1"), {})


# ── 8. Протокол: потолок числа частей медиа ──────────────────────────────────

class TestProtocolMediaCeiling(unittest.TestCase):
    def test_parts_ceiling_is_2048(self):
        expected = -(-proto.MAX_MEDIA_BYTES // proto.MIN_MEDIA_CHUNK_BYTES)
        self.assertEqual(expected, 2048)

    def test_parts_above_ceiling_rejected(self):
        max_parts = -(-proto.MAX_MEDIA_BYTES // proto.MIN_MEDIA_CHUNK_BYTES)
        frame = {
            "t": proto.C_MEDIA,
            "kind": "image",
            "data": base64.b64encode(b"x").decode(),
            "part": 0,
            "parts": max_parts + 1,
        }
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.parse_media_upload(frame)
        self.assertEqual(ctx.exception.code, proto.E_BAD_FRAME)

    def test_small_multipart_upload_passes(self):
        frame = {
            "t": proto.C_MEDIA,
            "kind": "image",
            "data": base64.b64encode(b"x").decode(),
            "part": 0,
            "parts": 5,
        }
        info = proto.parse_media_upload(frame)
        self.assertEqual((info["part"], info["parts"]), (0, 5))


if __name__ == "__main__":
    unittest.main()
