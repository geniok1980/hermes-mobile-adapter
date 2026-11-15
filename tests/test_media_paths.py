"""Вложения от приложения: где лежит файл и что видит агент.

Проверяем то, что ломается молча: файл, сохранённый вне каталогов медиа ядра
(``HERMES_HOME/cache``), агент получить не может — приходит только текст. И
потолок размера: обычное фото с телефона бывает больше 8 МБ.
"""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websockets  # noqa: E402

from test_adapter_ws import BaseWS  # noqa: E402

from hermes_mobile import protocol as proto  # noqa: E402

# Крошечный PNG 1x1 (валидная сигнатура и IHDR — содержимое картинки не разбираем).
PNG_1X1 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
    )
).decode()


class TestMediaPaths(BaseWS):
    async def _connect(self):
        await self.adapter.connect()
        websocket = await websockets.connect(self.url(), max_size=proto.MAX_FRAME_BYTES)
        await self.hello(websocket)
        return websocket

    async def test_file_lands_in_kernel_media_cache(self):
        websocket = await self._connect()
        try:
            await websocket.send(proto.dumps({
                "t": proto.C_MEDIA, "id": "m-1", "kind": "image",
                "mime": "image/png", "filename": "photo.png",
                "caption": "Что на фото?", "data": PNG_1X1,
            }))
            event = await self.adapter.next_event()
            path = event.media_urls[0]
            cache_root = os.path.join(self._hermes_home, "cache")
            self.assertTrue(path.startswith(cache_root), f"файл вне каталога медиа ядра: {path}")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(event.media_types, ["image/png"])
            self.assertEqual(event.text, "Что на фото?")
            # Имя файла — новое поле ядра; на старых его нет, тогда проверять нечего.
            if "media_filenames" in getattr(type(event), "__dataclass_fields__", {}):
                self.assertEqual(event.media_filenames, ["photo.png"])
        finally:
            await websocket.close()

    async def test_large_photo_is_accepted(self):
        """9 МБ — обычный снимок с телефона; раньше такой файл отвергался целиком."""
        payload = b"\x00" * (9 * 1024 * 1024)
        chunk = proto.MEDIA_CHUNK_BYTES
        parts = -(-len(payload) // chunk)
        websocket = await self._connect()
        try:
            for index in range(parts):
                piece = payload[index * chunk:(index + 1) * chunk]
                await websocket.send(proto.dumps({
                    "t": proto.C_MEDIA, "id": "m-2", "kind": "document",
                    "mime": "application/octet-stream", "filename": "scan.pdf",
                    "part": index, "parts": parts,
                    "data": base64.b64encode(piece).decode(),
                }))
            event = await self.adapter.next_event()
            self.assertTrue(event.media_urls)
            self.assertEqual(os.path.getsize(event.media_urls[0]), len(payload))
        finally:
            await websocket.close()


if __name__ == "__main__":
    unittest.main()
