"""Тесты протокола hermes-mobile (без Hermes: только ``protocol``)."""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))

from hermes_mobile import protocol as proto  # noqa: E402


class TestFrames(unittest.TestCase):
    def test_round_trip_keeps_cyrillic(self):
        frame = {"t": proto.S_MESSAGE, "text": "Привет, Евгений!"}
        raw = proto.dumps(frame)
        self.assertIn("Привет", raw)  # без \uXXXX-экранирования
        self.assertEqual(proto.loads(raw), frame)

    def test_loads_accepts_bytes(self):
        self.assertEqual(proto.loads("{\"t\":\"ping\"}".encode()), {"t": "ping"})

    def test_bad_json_is_protocol_error(self):
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.loads("{не json}")
        self.assertEqual(ctx.exception.code, proto.E_BAD_FRAME)
        self.assertFalse(ctx.exception.fatal)

    def test_non_object_is_protocol_error(self):
        with self.assertRaises(proto.ProtocolError):
            proto.loads("[1,2,3]")

    def test_bad_utf8_is_fatal(self):
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.loads(b"\xff\xfe\x00")
        self.assertTrue(ctx.exception.fatal)

    def test_oversized_frame_rejected(self):
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.loads("x" * (proto.MAX_FRAME_BYTES + 1))
        self.assertEqual(ctx.exception.code, proto.E_TOO_BIG)

    def test_frame_type_and_client_id(self):
        self.assertEqual(proto.frame_type({"t": " TEXT "}), "text")
        self.assertEqual(proto.frame_type({}), "")
        self.assertEqual(proto.client_id({"id": " abc "}), "abc")
        self.assertIsNone(proto.client_id({}))
        self.assertEqual(len(proto.client_id({"id": "x" * 500})), 128)


class TestPayloadValidation(unittest.TestCase):
    def test_require_text(self):
        self.assertEqual(proto.require_text({"text": "  привет  "}), "  привет  ")
        for bad in ({}, {"text": "   "}, {"text": None}):
            with self.assertRaises(proto.ProtocolError):
                proto.require_text(bad)

    def test_require_text_too_long(self):
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.require_text({"text": "я" * (proto.MAX_TEXT_CHARS + 1)})
        self.assertEqual(ctx.exception.code, proto.E_TOO_BIG)

    def test_require_audio_ok(self):
        payload = base64.b64encode(b"RIFF....WAVE").decode()
        audio, fmt = proto.require_audio({"format": "WAV", "data": payload})
        self.assertEqual(audio, b"RIFF....WAVE")
        self.assertEqual(fmt, "wav")

    def test_require_audio_rejects_bad_format_base64_and_size(self):
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.require_audio({"format": "mp3", "data": "AAAA"})
        self.assertEqual(ctx.exception.code, proto.E_UNSUPPORTED)
        with self.assertRaises(proto.ProtocolError):
            proto.require_audio({"format": "wav", "data": "не-base64!"})
        with self.assertRaises(proto.ProtocolError):
            proto.require_audio({"format": "ogg", "data": ""})
        big = base64.b64encode(b"\x00" * (proto.MAX_AUDIO_BYTES + 1)).decode()
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.require_audio({"format": "wav", "data": big})
        self.assertEqual(ctx.exception.code, proto.E_TOO_BIG)

    def test_parse_device_and_user(self):
        device = proto.parse_device({"device": {"id": " 1 ", "name": "iPhone", "мечта": "x"}})
        self.assertEqual(device, {"id": "1", "name": "iPhone"})
        self.assertEqual(proto.parse_user({"user": {"id": 5, "name": "Евгений"}}), {"id": "5", "name": "Евгений"})
        self.assertEqual(proto.parse_device({}), {})


class TestTokens(unittest.TestCase):
    def test_split_tokens(self):
        self.assertEqual(proto.split_tokens("a, b\nc"), ["a", "b", "c"])
        self.assertEqual(proto.split_tokens(""), [])
        self.assertEqual(proto.split_tokens(None), [])

    def test_is_authorized(self):
        token = "a" * 64
        self.assertTrue(proto.is_authorized(token, [token]))
        self.assertTrue(proto.is_authorized(token, ["другой", token]))
        self.assertFalse(proto.is_authorized("неверный", [token]))
        self.assertFalse(proto.is_authorized("", [token]))
        self.assertFalse(proto.is_authorized(None, [token]))
        # Платформа без токена не поднимается: пустой список ожидаемых = отказ.
        self.assertFalse(proto.is_authorized(token, []))
        self.assertFalse(proto.is_authorized(token, ["", None]))  # type: ignore[list-item]

    def test_device_allowed(self):
        self.assertTrue(proto.device_allowed("dev1", []))
        self.assertTrue(proto.device_allowed("dev1", ["dev1", "dev2"]))
        self.assertFalse(proto.device_allowed("dev3", ["dev1"]))
        self.assertFalse(proto.device_allowed(None, ["dev1"]))

    def test_redact_token_never_leaks(self):
        secret = "0123456789abcdef" * 2
        masked = proto.redact_token(secret)
        self.assertNotIn(secret, masked)
        self.assertTrue(masked.startswith("0123"))
        self.assertEqual(proto.redact_token(""), "<пусто>")
        self.assertEqual(proto.redact_token("short"), "***")


class TestChunking(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(proto.chunk_text("привет", 100), ["привет"])

    def test_empty_text_no_chunks(self):
        self.assertEqual(proto.chunk_text("", 100), [])
        self.assertEqual(proto.chunk_text(None, 100), [])  # type: ignore[arg-type]

    def test_long_text_chunks_respect_limit_and_losslessness(self):
        text = "Первый абзац про серверы и тарифы.\n\n" * 60
        chunks = proto.chunk_text(text, 200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.strip())
            self.assertLessEqual(len(chunk), 200)
        # Нарезка убирает пробелы на стыках кусков (каждый кусок — отдельное сообщение),
        # но не теряет ни одного знака.
        self.assertEqual("".join("".join(chunks).split()), "".join(text.split()))

    def test_splits_on_sentence_boundary(self):
        text = ("Первое предложение. " * 40).strip()
        chunks = proto.chunk_text(text, 100)
        self.assertTrue(chunks[0].endswith("."))

    def test_hard_split_without_spaces(self):
        text = "я" * 500
        chunks = proto.chunk_text(text, 120)
        self.assertEqual(len(chunks), 5)
        self.assertEqual("".join(chunks), text)

    def test_zero_limit_is_error(self):
        with self.assertRaises(ValueError):
            proto.chunk_text("текст", 0)


class TestLogging(unittest.TestCase):
    def test_trim_for_log(self):
        self.assertEqual(proto.trim_for_log("много   пробелов\nи перенос"), "много пробелов и перенос")
        self.assertTrue(proto.trim_for_log("я" * 500).endswith("…"))


if __name__ == "__main__":
    unittest.main()
