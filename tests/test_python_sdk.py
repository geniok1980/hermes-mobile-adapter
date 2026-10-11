"""Тесты Python-SDK против настоящего адаптера (тот же сервер, что в test_adapter_ws)."""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "adapter"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_adapter_ws import HAS_HERMES, TOKEN, BaseWS  # noqa: E402

from sdk.python.hermes_client import Device, HermesMobileClient, HermesProtocolError  # noqa: E402


@unittest.skipUnless(HAS_HERMES, "нужен Hermes Agent (платформенный API)")
class TestPythonSdk(BaseWS):
    async def test_sdk_connects_and_sends_text(self):
        self.adapter.home_device = "sdk-device"   # чтобы send() с устройства по умолчанию находил SDK
        await self.adapter.connect()
        seen_avatars = []

        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws",
            token=TOKEN,
            device=Device(id="sdk-device", name="SDK-клиент"),
            on_avatar=seen_avatars.append,
        ) as client:
            self.assertEqual(client.ready.session, "mobile:sdk-device")
            self.assertEqual(client.ready.v, 1)
            self.assertTrue(client.ready.capabilities["voice"])

            await client.send_text("привет из SDK", message_id="sdk-1")
            event = await self.adapter.next_event()
            self.assertEqual(event.text, "привет из SDK")
            self.assertEqual(event.source.chat_id, "mobile:sdk-device")
            self.assertEqual(event.message_id, "sdk-1")

            # Ответ агента приходит в SDK и собирается в один текст.
            await self.adapter.send(event.source.chat_id, "ответ агенту")
            parts = await client.wait_message(timeout=10)
            self.assertEqual("".join(parts), "ответ агенту")

            # Дедупликация: повтор с тем же id не создаёт второе событие.
            await client.send_text("привет из SDK", message_id="sdk-1")
            await asyncio.sleep(0.3)
            self.assertEqual(len(self.adapter.events), 1)

        # Состояния аватара дошли до SDK.
        self.assertIn("thinking", seen_avatars)

    async def test_sdk_wrong_token_raises(self):
        await self.adapter.connect()
        client = HermesMobileClient(url=f"ws://127.0.0.1:{self.port}/ws", token="неверный-токен", device=Device(id="x"))
        with self.assertRaises(HermesProtocolError) as ctx:
            await client.connect()
        self.assertEqual(ctx.exception.code, "auth_failed")
        self.assertTrue(ctx.exception.fatal)
        await client.aclose()

    async def test_sdk_voice_and_control_frames(self):
        await self.adapter.connect()
        async with HermesMobileClient(
            url=f"ws://127.0.0.1:{self.port}/ws", token=TOKEN, device=Device(id="sdk-voice", name="SDK-голос")
        ) as client:
            wav = b"RIFF" + b"\x00" * 32 + b"WAVEfmt "
            await client.send_audio(wav, fmt="wav", transcript="распознанный текст", message_id="voice-1")
            event = await self.adapter.next_event()
            self.assertEqual(event.text, "распознанный текст")
            self.assertTrue(event.media_urls and event.media_urls[0].endswith("voice-1.wav"))
            self.assertEqual(str(event.message_type), "MessageType.VOICE")

            await client.ping()
            await client.send_typing()
            await self.adapter.send_typing("mobile:sdk-voice")
            self.assertEqual(await client.wait_avatar(timeout=10), "thinking")


if __name__ == "__main__":
    unittest.main()
