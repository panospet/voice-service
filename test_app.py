import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import app


class FakeCommunicateMidStreamFail:
    """Fails on attempt 1 after yielding partial audio, succeeds on attempt 2."""

    calls = 0

    def __init__(self, text, **kwargs):
        self.text = text

    async def stream(self):
        FakeCommunicateMidStreamFail.calls += 1
        if FakeCommunicateMidStreamFail.calls == 1:
            yield {"type": "audio", "data": b"PARTIAL_GARBAGE"}
            raise RuntimeError("connection reset mid-stream")
        yield {"type": "audio", "data": b"CLEAN_AUDIO"}


class FakeCommunicateEmpty:
    """Yields no audio bytes without raising."""

    def __init__(self, text, **kwargs):
        self.text = text

    async def stream(self):
        if False:
            yield {}


class VoiceServiceTest(unittest.TestCase):
    def test_split_chunks_prefers_sentence_boundaries(self):
        text = "Πρώτη πρόταση εδώ. Δεύτερη πρόταση εδώ! Τρίτη πρόταση."
        chunks = app.split_chunks(text, 25)
        self.assertTrue(len(chunks) >= 2)
        self.assertEqual(chunks[0], "Πρώτη πρόταση εδώ.")

    def test_mid_stream_retry_does_not_duplicate_partial_bytes(self):
        FakeCommunicateMidStreamFail.calls = 0
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(app, "CACHE_DIR", tmpdir), patch.object(
                app.edge_tts, "Communicate", FakeCommunicateMidStreamFail
            ), patch("asyncio.sleep", return_value=None):
                audio = asyncio.run(
                    app.synth("Δοκιμή κειμένου.", "el-GR-AthinaNeural", "+0%", "+0Hz", "+0%")
                )
                self.assertEqual(audio, b"CLEAN_AUDIO")
                self.assertNotIn(b"PARTIAL_GARBAGE", audio)

    def test_empty_audio_raises_and_does_not_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(app, "CACHE_DIR", tmpdir), patch.object(
                app.edge_tts, "Communicate", FakeCommunicateEmpty
            ), patch("asyncio.sleep", return_value=None):
                with self.assertRaises(RuntimeError):
                    asyncio.run(
                        app.synth("Κενό.", "el-GR-AthinaNeural", "+0%", "+0Hz", "+0%")
                    )
                self.assertEqual(os.listdir(tmpdir), [])

    def test_validate_public_url_blocks_ssrf(self):
        for bad in [
            "http://127.0.0.1:8080/api/briefing-news",
            "http://localhost:4533/rest/ping",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
        ]:
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    app.validate_public_url(bad)


if __name__ == "__main__":
    unittest.main()
