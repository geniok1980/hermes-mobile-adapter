"""Тесты Python-SDK на новые возможности адаптера: карточки-вопросы, медиа, стриминг.

Гоняем **настоящий Python-SDK** против **настоящего сервера адаптера** (тот же harness,
что в ``test_adapter_ws.py``: класс ``BaseWS``, реальный WebSocket на loopback).
Подменяется только резолвер Hermes (``tools.approval.resolve_gateway_approval``) — чтобы
проверить, что ответ на карточку действительно доходит до Hermes, а не уходит в чат.
"""

import asyncio
import base64
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "adapter"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_adapter_ws import HAS_HERMES, TOKEN, BaseWS  # noqa: E402

from sdk.python.hermes_client import Device, HermesMobileClient  # noqa: E402


@unittest.skipUnless(HAS_HERMES, "нужен Hermes Agent (платформенный API)")
class TestSdkPrompts(BaseWS):
    """Карточка exec_approval → ответ SDK → резолвер Hermes вызван (и только он)."""

    def _patch_resolvers(self):
        import tools.approval as approval

        self._orig = approval.resolve_gateway_approval
        approval.resolve_gateway_approval = lambda session_key, choice, **kw: (
            self.resolved.append(("approval", session_key, choice)) or 1
        )

    def _restore_resolvers(self):
        import tools.approval as approval

        approval.resolve_gateway_approval = self._orig

    async def test_exec_approval_card_round_trip(self):
        self.resolved = []
        self._patch_resolvers()
        try:
            await self.adapter.connect()
            prompts, done = [], []
            async with HermesMobileClient(
                url=f"ws://127.0.0.1:{self.port}/ws",
                token=TOKEN,
                device=Device(id="sdk-prompt", name="SDK-карточки"),
                on_prompt=prompts.append,
                on_prompt_done=done.append,
            ) as client:
                result = await self.adapter.send_exec_approval(
                    "mobile:sdk-prompt", "rm -rf /tmp/x", "mobile:sdk-prompt", description="удаление файлов"
                )
                self.assertTrue(result.success)

                prompt = await client.wait_prompt(timeout=10)
                self.assertEqual(prompt.kind, "exec_approval")
                self.assertIn("rm -rf /tmp/x", prompt.text)
                self.assertIn("once", prompt.option_ids())
                self.assertIn("deny", prompt.option_ids())
                self.assertEqual(prompt.session, "mobile:sdk-prompt")

                await client.answer_prompt(prompt.id, option_id="once")

                for _ in range(50):
                    if self.resolved:
                        break
                    await asyncio.sleep(0.1)
                self.assertEqual(self.resolved, [("approval", "mobile:sdk-prompt", "once")])

                for _ in range(50):
                    if done:
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(done and done[-1].option_id == "once")

                # Ответ на карточку не должен уйти в чат как обычное сообщение.
                await asyncio.sleep(0.2)
                self.assertEqual(self.adapter.events, [])
        finally:
            self._restore_resolvers()

    async def test_answer_unknown_card_is_marked_expired(self):
        self.resolved = []
        await self.adapter.connect()
        seen_done = []
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws",
            token=TOKEN,
            device=Device(id="sdk-prompt2"),
            on_prompt_done=seen_done.append,
        ) as client:
            await client.answer_prompt("нет-такой-карточки", option_id="once")
            for _ in range(50):
                if seen_done:
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(seen_done and seen_done[-1].expired)


@unittest.skipUnless(HAS_HERMES, "нужен Hermes Agent (платформенный API)")
class TestSdkInboundMedia(BaseWS):
    """SDK шлёт фото/файл агенту — адаптер собирает файл и отдаёт событие."""

    async def test_sdk_sends_photo_agent_receives(self):
        await self.adapter.connect()
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws", token=TOKEN, device=Device(id="sdk-media-in")
        ) as client:
            payload = b"\x89PNG\r\n\x1a\n" + b"data" * 8
            await client.send_image(payload, filename="скрин.png", caption="вот ошибка")

            event = await self.adapter.next_event()
            self.assertEqual(str(event.message_type), "MessageType.PHOTO")
            self.assertEqual(event.text, "вот ошибка")
            self.assertEqual(event.media_types, ["image/png"])
            self.assertTrue(event.media_urls[0].endswith(".png"))
            with open(event.media_urls[0], "rb") as handle:
                self.assertEqual(handle.read(), payload)

    async def test_sdk_sends_document_with_chunking(self):
        """Файл больше одного куска: SDK режет по part/parts, адаптер склеивает целиком."""
        await self.adapter.connect()
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws", token=TOKEN, device=Device(id="sdk-chunk")
        ) as client:
            payload = bytes(range(256)) * 20  # 5120 байт → несколько частей
            await client.send_document(
                payload, filename="архив.zip", chunk_bytes=1024, mime="application/zip"
            )
            event = await self.adapter.next_event()
            self.assertEqual(str(event.message_type), "MessageType.DOCUMENT")
            with open(event.media_urls[0], "rb") as handle:
                self.assertEqual(handle.read(), payload)  # не обрывок

    async def test_empty_media_is_refused_by_sdk(self):
        await self.adapter.connect()
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws", token=TOKEN, device=Device(id="sdk-empty")
        ) as client:
            with self.assertRaises(ValueError):
                await client.send_image(b"")


@unittest.skipUnless(HAS_HERMES, "нужен Hermes Agent (платформенный API)")
class TestSdkOutboundMedia(BaseWS):
    """Агент шлёт медиа — SDK собирает части и сохраняет файл на диск."""

    async def test_sdk_receives_and_saves_media(self):
        await self.adapter.connect()
        received = []
        client = HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws",
            token=TOKEN,
            device=Device(id="sdk-media-out"),
            on_media=received.append,
        )
        async with client:
            data = b"\x89PNG\r\n\x1a\n" + b"chart-bytes" * 4
            source = os.path.join(self._hermes_home, "chart.png")
            with open(source, "wb") as handle:
                handle.write(data)

            result = await self.adapter.send_image_file("mobile:sdk-media-out", source, caption="График")
            self.assertTrue(result.success)

            media = await client.wait_media(timeout=10)
            self.assertEqual(media.kind, "image")
            self.assertEqual(media.mime, "image/png")
            self.assertEqual(media.caption, "График")
            self.assertEqual(media.data, data)          # все части собраны вместе
            self.assertEqual(media.size, len(data))

            out_dir = os.path.join(self._hermes_home, "sdk-downloads")
            saved = client.save_media(media, out_dir)
            self.assertTrue(saved.exists())
            self.assertEqual(saved.read_bytes(), data)
            self.assertTrue(saved.name.endswith(".png"))
            self.assertTrue(received)                   # колбэк on_media тоже сработал

    async def test_sdk_receives_chunked_media(self):
        """Большой файл приходит несколькими кадрами — SDK отдаёт его только целиком."""
        await self.adapter.connect()
        import hermes_mobile.protocol as proto

        original = proto.split_bytes
        # Адаптер режет файл на части (в бою — по 3 МБ; здесь уменьшаем кусок для скорости).
        proto.split_bytes = lambda payload, chunk_size=1024: original(payload, chunk_size)
        try:
            async with HermesMobileClient(
                url=f"ws://127.0.0.1:{self.port}/ws", token=TOKEN, device=Device(id="sdk-media-big")
            ) as client:
                data = bytes(range(256)) * 20  # 5120 байт → 5 частей
                source = os.path.join(self._hermes_home, "big.bin")
                with open(source, "wb") as handle:
                    handle.write(data)

                result = await self.adapter.send_document("mobile:sdk-media-big", source)
                self.assertTrue(result.success)
                media = await client.wait_media(timeout=10)
                self.assertGreater(media.parts, 1)
                self.assertEqual(media.data, data)
        finally:
            proto.split_bytes = original


@unittest.skipUnless(HAS_HERMES, "нужен Hermes Agent (платформенный API)")
class TestSdkStreamingAndTyping(BaseWS):
    """Правка сообщения (edit) и остановка индикатора «печатает» (typing.stop)."""

    async def test_sdk_applies_message_edit(self):
        await self.adapter.connect()
        edits = []
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws",
            token=TOKEN,
            device=Device(id="sdk-stream"),
            on_message_edit=edits.append,
        ) as client:
            first = await self.adapter.edit_message("mobile:sdk-stream", "msg-1", "Первая половина")
            self.assertTrue(first.success)
            for _ in range(50):
                if edits:
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(edits)
            self.assertEqual(edits[-1].id, "msg-1")
            self.assertEqual(edits[-1].text, "Первая половина")
            self.assertTrue(edits[-1].edit)
            self.assertFalse(edits[-1].final)

            await self.adapter.edit_message("mobile:sdk-stream", "msg-1", "Целиком готовый ответ", finalize=True)
            for _ in range(50):
                if edits and edits[-1].final:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(edits[-1].text, "Целиком готовый ответ")
            self.assertTrue(edits[-1].final and edits[-1].edit)

    async def test_sdk_sees_typing_stop(self):
        await self.adapter.connect()
        stops = []
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws",
            token=TOKEN,
            device=Device(id="sdk-typing"),
            on_typing_stop=lambda: stops.append(1),
        ) as client:
            await self.adapter.stop_typing("mobile:sdk-typing")
            for _ in range(50):
                if stops:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(stops, [1])


@unittest.skipUnless(HAS_HERMES, "нужен Hermes Agent (платформенный API)")
class TestSdkCapabilities(BaseWS):
    async def test_ready_advertises_new_capabilities(self):
        await self.adapter.connect()
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws", token=TOKEN, device=Device(id="sdk-caps")
        ) as client:
            capabilities = client.ready.capabilities
            self.assertTrue(capabilities["edit"])
            self.assertIn("document", capabilities["media_in"])
            self.assertIn("voice", capabilities["media_out"])
            self.assertIn("exec_approval", capabilities["prompts"])
            self.assertEqual(capabilities["max_media_bytes"], 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
