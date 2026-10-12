"""Тесты полного набора возможностей адаптера: медиа, карточки-вопросы, стриминг.

Проверяем ровно то, что делают штатные адаптеры Hermes: медиа наружу (агент → приложение)
и внутрь (приложение → агент), кнопочное одобрение опасных команд, подтверждение
слэш-команд, уточняющие вопросы, правку сообщения для потоковой выдачи, остановку
индикатора «печатает» и адресацию устройств (`mobile:<id>`).
"""

import asyncio
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))

from test_adapter_ws import TOKEN, BaseWS  # noqa: E402

from hermes_mobile import protocol as proto  # noqa: E402


class TestOutboundMedia(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        import websockets

        self.websocket = await websockets.connect(self.url())
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def _collect_media(self, message_id: str) -> bytes:
        """Собирает все части файла, который адаптер отправил приложению."""
        payload = b""
        for _ in range(50):
            frame = await self.recv_frame(self.websocket, proto.S_MEDIA)
            if frame["id"] != message_id:
                continue
            if frame.get("data"):
                payload += base64.b64decode(frame["data"])
            if frame["final"]:
                return payload
        raise AssertionError("кадры media не пришли целиком")

    async def test_send_image_file(self):
        image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
        path = os.path.join(self._hermes_home, "chart.png")
        with open(path, "wb") as handle:
            handle.write(image)
        result = await self.adapter.send_image_file("mobile:dev-1", path, caption="График")
        self.assertTrue(result.success)
        frame = await self.recv_frame(self.websocket, proto.S_MEDIA)
        self.assertEqual(frame["kind"], "image")
        self.assertEqual(frame["mime"], "image/png")
        self.assertEqual(frame["caption"], "График")
        self.assertEqual(frame["parts"], 1)
        self.assertTrue(frame["final"])
        self.assertEqual(base64.b64decode(frame["data"]), image)

    async def test_send_document_with_filename(self):
        path = os.path.join(self._hermes_home, "report.bin")
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4 test")
        result = await self.adapter.send_document("mobile:dev-1", path, caption="Отчёт", file_name="отчёт.pdf")
        self.assertTrue(result.success)
        frame = await self.recv_frame(self.websocket, proto.S_MEDIA)
        self.assertEqual(frame["kind"], "document")
        self.assertEqual(frame["filename"], "отчёт.pdf")
        self.assertEqual(frame["mime"], "application/pdf")

    async def test_send_voice_and_video_kinds(self):
        voice = os.path.join(self._hermes_home, "answer.ogg")
        with open(voice, "wb") as handle:
            handle.write(b"OggS" + b"\x00" * 32)
        self.assertTrue((await self.adapter.send_voice("mobile:dev-1", voice)).success)
        frame = await self.recv_frame(self.websocket, proto.S_MEDIA)
        self.assertEqual((frame["kind"], frame["mime"]), ("voice", "audio/ogg"))

        video = os.path.join(self._hermes_home, "clip.mp4")
        with open(video, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42")
        self.assertTrue((await self.adapter.send_video("mobile:dev-1", video)).success)
        self.assertEqual((await self.recv_frame(self.websocket, proto.S_MEDIA))["kind"], "video")

    async def test_large_file_is_chunked(self):
        proto_original = proto.MEDIA_CHUNK_BYTES
        proto.MEDIA_CHUNK_BYTES = 1024  # ускоряем тест: куски по 1 КБ
        try:
            path = os.path.join(self._hermes_home, "big.bin")
            payload = bytes(range(256)) * 20  # 5120 байт → 5 частей
            with open(path, "wb") as handle:
                handle.write(payload)
            result = await self.adapter.send_document("mobile:dev-1", path)
            self.assertTrue(result.success)
            self.assertEqual(await self._collect_media(result.message_id), payload)
        finally:
            proto.MEDIA_CHUNK_BYTES = proto_original

    async def test_send_image_by_url_sends_link(self):
        result = await self.adapter.send_image("mobile:dev-1", "https://example.com/pic.png", caption="из сети")
        self.assertTrue(result.success)
        frame = await self.recv_frame(self.websocket, proto.S_MEDIA)
        self.assertEqual(frame["url"], "https://example.com/pic.png")
        self.assertNotIn("data", frame)

    async def test_missing_file_is_a_real_failure(self):
        result = await self.adapter.send_image_file("mobile:dev-1", "/нет/такого.png")
        self.assertFalse(result.success)
        self.assertIn("unavailable", result.error or "")

    async def test_oversized_file_is_refused(self):
        path = os.path.join(self._hermes_home, "huge.bin")
        with open(path, "wb") as handle:
            handle.write(b"\x00" * (proto.MAX_MEDIA_BYTES + 1))
        result = await self.adapter.send_document("mobile:dev-1", path)
        self.assertFalse(result.success)
        self.assertIn("above the", result.error or "")

    async def test_multiple_images_aggregate_success(self):
        good = os.path.join(self._hermes_home, "one.png")
        with open(good, "wb") as handle:
            handle.write(b"\x89PNG" + b"\x00" * 8)
        result = await self.adapter.send_multiple_images("mobile:dev-1", [(good, "первая"), ("/нет/второй.png", None)])
        self.assertTrue(result.success)  # успех, если доставлена хотя бы одна

    async def test_media_to_offline_device_fails(self):
        result = await self.adapter.send_image_file("mobile:нет-такого", "/tmp/x.png")
        self.assertFalse(result.success)
        self.assertEqual(result.error, proto.E_DEVICE_OFFLINE)


class TestInboundMedia(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        import websockets

        self.websocket = await websockets.connect(self.url())
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def test_photo_becomes_photo_event(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"data"
        await self.websocket.send(
            proto.dumps(
                {
                    "t": proto.C_MEDIA,
                    "id": "img-1",
                    "kind": "image",
                    "mime": "image/png",
                    "filename": "скрин.png",
                    "caption": "вот ошибка",
                    "data": base64.b64encode(payload).decode(),
                }
            )
        )
        event = await self.adapter.next_event()
        self.assertEqual(str(event.message_type), "MessageType.PHOTO")
        self.assertEqual(event.text, "вот ошибка")
        self.assertEqual(event.media_types, ["image/png"])
        self.assertEqual(event.media_text_inlined, [False])
        self.assertTrue(event.media_urls[0].endswith(".png"))
        with open(event.media_urls[0], "rb") as handle:
            self.assertEqual(handle.read(), payload)

    async def test_document_and_video_types(self):
        await self.websocket.send(
            proto.dumps(
                {"t": proto.C_MEDIA, "id": "doc-1", "kind": "document", "mime": "application/pdf",
                 "filename": "договор.pdf", "data": base64.b64encode(b"%PDF").decode()}
            )
        )
        event = await self.adapter.next_event()
        self.assertEqual(str(event.message_type), "MessageType.DOCUMENT")
        self.assertEqual(event.media_types, ["application/pdf"])

        await self.websocket.send(
            proto.dumps(
                {"t": proto.C_MEDIA, "id": "vid-1", "kind": "video", "mime": "video/mp4",
                 "data": base64.b64encode(b"\x00\x00\x00\x18ftyp").decode()}
            )
        )
        for _ in range(50):
            if len(self.adapter.events) >= 2:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(str(self.adapter.events[-1].message_type), "MessageType.VIDEO")

    async def test_chunked_upload_is_assembled(self):
        payload = b"0123456789" * 3
        parts = [payload[:15], payload[15:]]
        for index, chunk in enumerate(parts):
            await self.websocket.send(
                proto.dumps(
                    {"t": proto.C_MEDIA, "id": "big-1", "kind": "document", "mime": "application/zip",
                     "filename": "архив.zip", "part": index, "parts": len(parts),
                     "data": base64.b64encode(chunk).decode()}
                )
            )
        event = await self.adapter.next_event()
        with open(event.media_urls[0], "rb") as handle:
            self.assertEqual(handle.read(), payload)  # файл собран целиком, не обрывок

    async def test_bad_media_kind_is_rejected(self):
        await self.websocket.send(
            proto.dumps({"t": proto.C_MEDIA, "id": "bad", "kind": "exe", "data": base64.b64encode(b"x").decode()})
        )
        frame = await self.recv_frame(self.websocket)
        self.assertEqual(frame["code"], proto.E_UNSUPPORTED)
        self.assertEqual(self.adapter.events, [])


class TestPrompts(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        import websockets

        self.websocket = await websockets.connect(self.url())
        await self.hello(self.websocket)
        self.resolved = []
        self.marked = []

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    def _patch_resolvers(self):
        """Подменяет резолверы Hermes, чтобы проверить, что мы зовём именно их."""
        import tools.approval as approval
        import tools.clarify_gateway as clarify
        import tools.slash_confirm as slash

        self._orig = (approval.resolve_gateway_approval, clarify.resolve_gateway_clarify,
                      clarify.mark_awaiting_text, slash.resolve)
        approval.resolve_gateway_approval = lambda session_key, choice, **kw: (
            self.resolved.append(("approval", session_key, choice)) or 1
        )
        clarify.resolve_gateway_clarify = lambda clarify_id, response: (
            self.resolved.append(("clarify", clarify_id, response)) or True
        )
        clarify.mark_awaiting_text = lambda clarify_id: (self.marked.append(clarify_id) or True)
        async def _slash_resolve(session_key, confirm_id, choice):
            self.resolved.append(("slash", session_key, confirm_id, choice))
            return "Команда выполнена"
        slash.resolve = _slash_resolve

    def _restore_resolvers(self):
        import tools.approval as approval
        import tools.clarify_gateway as clarify
        import tools.slash_confirm as slash

        approval.resolve_gateway_approval, clarify.resolve_gateway_clarify, \
            clarify.mark_awaiting_text, slash.resolve = self._orig

    async def test_exec_approval_round_trip(self):
        self._patch_resolvers()
        try:
            result = await self.adapter.send_exec_approval(
                "mobile:dev-1", "rm -rf /tmp/x", "mobile:dev-1", description="удаление файлов"
            )
            self.assertTrue(result.success)
            card = await self.recv_frame(self.websocket, proto.S_PROMPT)
            self.assertEqual(card["kind"], proto.P_EXEC_APPROVAL)
            self.assertIn("rm -rf /tmp/x", card["text"])
            option_ids = [option["id"] for option in card["options"]]
            self.assertIn("once", option_ids)
            self.assertIn("deny", option_ids)
            self.assertEqual(card["session"], "mobile:dev-1")

            await self.websocket.send(
                proto.dumps({"t": proto.C_PROMPT, "prompt_id": card["id"], "option_id": "once"})
            )
            done = await self.recv_frame(self.websocket, proto.S_PROMPT_DONE)
            self.assertEqual(done["option_id"], "once")
            self.assertEqual(self.resolved, [("approval", "mobile:dev-1", "once")])
            # Ответ на карточку не должен уйти в чат как обычное сообщение.
            await asyncio.sleep(0.2)
            self.assertEqual(self.adapter.events, [])
        finally:
            self._restore_resolvers()

    async def test_exec_approval_unknown_option_becomes_deny(self):
        self._patch_resolvers()
        try:
            result = await self.adapter.send_exec_approval("mobile:dev-1", "dd if=/dev/zero", "mobile:dev-1")
            card = await self.recv_frame(self.websocket, proto.S_PROMPT)
            await self.websocket.send(
                proto.dumps({"t": proto.C_PROMPT, "prompt_id": card["id"], "option_id": "разрешить_всё"})
            )
            done = await self.recv_frame(self.websocket, proto.S_PROMPT_DONE)
            self.assertEqual(done["option_id"], "deny")   # незнакомое значение = запрет
            self.assertEqual(self.resolved[-1], ("approval", "mobile:dev-1", "deny"))
        finally:
            self._restore_resolvers()

    async def test_slash_confirm_resolves_and_delivers_result(self):
        self._patch_resolvers()
        try:
            result = await self.adapter.send_slash_confirm(
                "mobile:dev-1", "Перезагрузить MCP?", "Будут перезапущены серверы.", "mobile:dev-1", "confirm-1"
            )
            self.assertTrue(result.success)
            card = await self.recv_frame(self.websocket, proto.S_PROMPT)
            self.assertEqual(card["kind"], proto.P_SLASH_CONFIRM)
            self.assertEqual([option["id"] for option in card["options"]], ["once", "always", "cancel"])
            await self.websocket.send(
                proto.dumps({"t": proto.C_PROMPT, "prompt_id": card["id"], "option_id": "always"})
            )
            # Текст резолвера и закрытие карточки приходят подряд — читаем всё, ничего не теряя.
            message_text, done_option = None, None
            for _ in range(8):
                frame = proto.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5))
                if frame.get("t") == proto.S_MESSAGE:
                    message_text = frame.get("text")
                elif frame.get("t") == proto.S_PROMPT_DONE:
                    done_option = frame.get("option_id")
                if message_text is not None and done_option is not None:
                    break
            self.assertEqual(done_option, "always")
            self.assertEqual(message_text, "Команда выполнена")
            self.assertEqual(self.resolved[-1], ("slash", "mobile:dev-1", "confirm-1", "always"))
        finally:
            self._restore_resolvers()

    async def test_clarify_choice_and_free_text(self):
        self._patch_resolvers()
        try:
            result = await self.adapter.send_clarify(
                "mobile:dev-1", "Какой тариф?", ["Базовый", "Про"], "clarify-1", "mobile:dev-1"
            )
            self.assertTrue(result.success)
            card = await self.recv_frame(self.websocket, proto.S_PROMPT)
            self.assertEqual(card["kind"], proto.P_CLARIFY)
            self.assertEqual([option["id"] for option in card["options"]], ["c0", "c1", proto.CLARIFY_OTHER])
            await self.websocket.send(
                proto.dumps({"t": proto.C_PROMPT, "prompt_id": card["id"], "option_id": "c1"})
            )
            await self.recv_frame(self.websocket, proto.S_PROMPT_DONE)
            self.assertEqual(self.resolved[-1], ("clarify", "clarify-1", "Про"))

            # «Свой ответ» текстом: уходит в резолвер, а не в чат.
            result = await self.adapter.send_clarify("mobile:dev-1", "Уточните", ["А", "Б"], "clarify-2", "mobile:dev-1")
            card = await self.recv_frame(self.websocket, proto.S_PROMPT)
            await self.websocket.send(
                proto.dumps({"t": proto.C_PROMPT, "prompt_id": card["id"], "option_id": "other", "text": "Свой вариант"})
            )
            await self.recv_frame(self.websocket, proto.S_PROMPT_DONE)
            self.assertEqual(self.resolved[-1], ("clarify", "clarify-2", "Свой вариант"))
            self.assertEqual(self.adapter.events, [])
        finally:
            self._restore_resolvers()

    async def test_unknown_prompt_is_ignored(self):
        await self.websocket.send(
            proto.dumps({"t": proto.C_PROMPT, "prompt_id": "нет-такого", "option_id": "once"})
        )
        frame = await self.recv_frame(self.websocket, proto.S_PROMPT_DONE)
        self.assertTrue(frame["expired"])
        self.assertEqual(self.adapter.events, [])

    async def test_prompt_expires_and_notifies(self):
        self.adapter.prompt_ttl = 0.3
        result = await self.adapter.send_slash_confirm("mobile:dev-1", "Т", "М", "mobile:dev-1", "c-x")
        card = await self.recv_frame(self.websocket, proto.S_PROMPT)
        done = await self.recv_frame(self.websocket, proto.S_PROMPT_DONE, timeout=5)
        self.assertEqual(done["id"], card["id"])
        self.assertTrue(done["expired"])
        self.assertNotIn(card["id"], self.adapter._pending_prompts)
        self.assertTrue(result.success)


class TestStreamingAndTyping(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        import websockets

        self.websocket = await websockets.connect(self.url())
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def test_edit_message_marks_stream(self):
        result = await self.adapter.edit_message("mobile:dev-1", "msg-1", "Первая половина")
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "msg-1")
        frame = await self.recv_frame(self.websocket, proto.S_MESSAGE)
        self.assertTrue(frame["edit"])
        self.assertFalse(frame["final"])
        self.assertEqual(frame["text"], "Первая половина")

        await self.adapter.edit_message("mobile:dev-1", "msg-1", "Целиком готовый ответ", finalize=True)
        frame = await self.recv_frame(self.websocket, proto.S_MESSAGE)
        self.assertTrue(frame["final"] and frame["edit"])
        self.assertEqual(frame["text"], "Целиком готовый ответ")

    async def test_edit_on_offline_device_is_false(self):
        result = await self.adapter.edit_message("mobile:нет", "m", "текст")
        self.assertFalse(result.success)

    async def test_stop_typing_sends_flag_and_idle(self):
        await self.adapter.send_typing("mobile:dev-1")
        first = await self.recv_frame(self.websocket)
        self.assertIn(first["t"], (proto.S_AVATAR, proto.S_TYPING))
        await self.adapter.stop_typing("mobile:dev-1")
        seen = []
        for _ in range(3):
            frame = await self.recv_frame(self.websocket)
            seen.append((frame["t"], frame.get("stop"), frame.get("state")))
            if (proto.S_TYPING, True, None) in seen and (proto.S_AVATAR, None, "idle") in seen:
                break
        self.assertIn((proto.S_TYPING, True, None), seen)
        self.assertIn((proto.S_AVATAR, None, "idle"), seen)

    async def test_capabilities_advertise_everything(self):
        ready = self.ready_frame
        capabilities = ready["capabilities"]
        self.assertTrue(capabilities["edit"])
        self.assertIn("document", capabilities["media_in"])
        self.assertIn("voice", capabilities["media_out"])
        self.assertEqual(sorted(capabilities["prompts"]), sorted(proto.PROMPT_KINDS))
        self.assertEqual(capabilities["max_media_bytes"], proto.MAX_MEDIA_BYTES)


class TestTargetRefs(BaseWS):
    async def test_parse_target_ref(self):
        from hermes_mobile import adapter as mobile_adapter

        self.assertEqual(mobile_adapter.parse_target_ref("mobile:tel-1"), ("mobile:tel-1", None))
        self.assertEqual(mobile_adapter.parse_target_ref("tel-1"), ("mobile:tel-1", None))
        self.assertIsNone(mobile_adapter.parse_target_ref("mobile"))
        self.assertIsNone(mobile_adapter.parse_target_ref("home"))
        self.assertIsNone(mobile_adapter.parse_target_ref(""))

    async def test_validate_target_ref(self):
        from hermes_mobile import adapter as mobile_adapter

        self.assertTrue(mobile_adapter.validate_target_ref("tel-1"))
        self.assertIsInstance(mobile_adapter.validate_target_ref(""), str)

    async def test_registered_hooks_are_present(self):
        self.assertTrue(callable(self.captured["parse_target_ref_fn"]))
        self.assertTrue(callable(self.captured["validate_target_ref_fn"]))
        self.assertEqual(self.captured["allow_all_env"], "MOBILE_ALLOW_ALL_USERS")
        self.assertTrue(self.captured["is_connected"](self.config))


class TestDeliverWithAttachment(BaseWS):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.adapter.connect()
        import websockets

        self.websocket = await websockets.connect(self.url())
        await self.hello(self.websocket)

    async def asyncTearDown(self):
        await self.websocket.close()
        await super().asyncTearDown()

    async def test_deliver_with_media_reaches_device(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"cron"
        await self.websocket.send(
            proto.dumps(
                {
                    "t": proto.C_DELIVER,
                    "id": "d-media",
                    "target": "dev-1",
                    "text": "отчёт за день",
                    "media": {
                        "kind": "image",
                        "mime": "image/png",
                        "filename": "отчёт.png",
                        "data": base64.b64encode(payload).decode(),
                    },
                }
            )
        )
        frame = await self.recv_frame(self.websocket, proto.S_MEDIA)
        self.assertEqual(frame["kind"], "image")
        self.assertEqual(frame["filename"], "отчёт.png")
        self.assertEqual(frame["caption"], "отчёт за день")
        self.assertEqual(base64.b64decode(frame["data"]), payload)

    async def test_deliver_without_text_or_media_is_rejected(self):
        await self.websocket.send(proto.dumps({"t": proto.C_DELIVER, "id": "empty", "target": "dev-1"}))
        frame = await self.recv_frame(self.websocket)
        self.assertEqual(frame["code"], proto.E_BAD_FRAME)


class TestProtocolMediaHelpers(unittest.TestCase):
    def test_guess_mime(self):
        self.assertEqual(proto.guess_mime("a.PNG"), "image/png")
        self.assertEqual(proto.guess_mime("отчёт.pdf"), "application/pdf")
        self.assertEqual(proto.guess_mime("file.unknown", "text/plain"), "text/plain")

    def test_split_bytes(self):
        chunks = proto.split_bytes(b"abcdef", 4)
        self.assertEqual(chunks, [b"abcd", b"ef"])
        self.assertEqual(b"".join(proto.split_bytes(b"", 4)), b"")

    def test_option_allowed_allowlists(self):
        self.assertTrue(proto.option_allowed(proto.P_EXEC_APPROVAL, "deny"))
        self.assertFalse(proto.option_allowed(proto.P_EXEC_APPROVAL, "yes"))
        self.assertTrue(proto.option_allowed(proto.P_SLASH_CONFIRM, "always"))
        self.assertTrue(proto.option_allowed(proto.P_SLASH_CONFIRM, "once"))
        self.assertFalse(proto.option_allowed(proto.P_SLASH_CONFIRM, "cancel_all"))
        self.assertTrue(proto.option_allowed(proto.P_CLARIFY, "c3"))
        self.assertTrue(proto.option_allowed(proto.P_CLARIFY, "other"))
        self.assertFalse(proto.option_allowed(proto.P_CLARIFY, "c"))

    def test_parse_media_upload_validation(self):
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.parse_media_upload({"kind": "image", "data": "!!!"})
        self.assertEqual(ctx.exception.code, proto.E_BAD_FRAME)
        info = proto.parse_media_upload(
            {"kind": "image", "data": base64.b64encode(b"x").decode(), "part": 0, "parts": 2}
        )
        self.assertEqual((info["part"], info["parts"], info["kind"]), (0, 2, "image"))
        with self.assertRaises(proto.ProtocolError):
            proto.parse_media_upload({"kind": "image", "part": 5, "parts": 2, "data": base64.b64encode(b"x").decode()})


if __name__ == "__main__":
    unittest.main()
