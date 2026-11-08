import asyncio
import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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

    def test_cache_hit_returns_audio_without_synthesis(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            text = "Cached text"
            voice = "el-GR-NestorasNeural"
            rate, pitch, volume = "+0%", "+0Hz", "+0%"
            key = hashlib.sha256(f"{text}|{voice}|{rate}|{pitch}|{volume}".encode("utf-8")).hexdigest()
            cache_file = os.path.join(tmpdir, key + ".mp3")
            with open(cache_file, "wb") as f:
                f.write(b"PRE_CACHED_AUDIO")

            with patch.object(app, "CACHE_DIR", tmpdir), patch.object(
                app.edge_tts, "Communicate"
            ) as mock_comm:
                audio = asyncio.run(app.synth(text, voice, rate, pitch, volume))
                self.assertEqual(audio, b"PRE_CACHED_AUDIO")
                mock_comm.assert_not_called()

    def test_maybe_prune_cache_deletes_expired_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_file = os.path.join(tmpdir, "old.mp3")
            new_file = os.path.join(tmpdir, "new.mp3")
            other_file = os.path.join(tmpdir, "ignore.txt")
            with open(old_file, "wb") as f:
                f.write(b"old")
            with open(new_file, "wb") as f:
                f.write(b"new")
            with open(other_file, "wb") as f:
                f.write(b"txt")

            old_time = time.time() - (35 * 86400)
            os.utime(old_file, (old_time, old_time))

            with patch.object(app, "CACHE_DIR", tmpdir), patch.object(
                app, "CACHE_RETENTION_DAYS", 30
            ), patch.object(app, "_last_prune_ts", 0.0):
                app.maybe_prune_cache()

            self.assertFalse(os.path.exists(old_file))
            self.assertTrue(os.path.exists(new_file))
            self.assertTrue(os.path.exists(other_file))


class VoiceServiceHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth_user = "testuser"
        cls.auth_pass = "testsecret"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _auth_header(self, user="testuser", password="testsecret"):
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {token}"}

    def test_healthz_endpoint_is_unauthenticated(self):
        with patch.object(app, "AUTH_USER", self.auth_user), patch.object(
            app, "AUTH_PASS", self.auth_pass
        ):
            with urllib.request.urlopen(self._url("/healthz")) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data, {"ok": True})

    def test_synthesize_unauthorized_when_credentials_missing_or_invalid(self):
        with patch.object(app, "AUTH_USER", self.auth_user), patch.object(
            app, "AUTH_PASS", self.auth_pass
        ):
            # No auth header
            req = urllib.request.Request(self._url("/synthesize"), data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 401)

            # Wrong credentials
            bad_hdr = self._auth_header("wrong", "credentials")
            req_bad = urllib.request.Request(
                self._url("/synthesize"), data=b"{}", headers=bad_hdr, method="POST"
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_bad)
            self.assertEqual(ctx.exception.code, 401)

    def test_synthesize_validation_errors(self):
        with patch.object(app, "AUTH_USER", self.auth_user), patch.object(
            app, "AUTH_PASS", self.auth_pass
        ):
            headers = self._auth_header()
            headers["Content-Type"] = "application/json"

            # Invalid JSON
            req = urllib.request.Request(
                self._url("/synthesize"), data=b"{not-valid-json}", headers=headers, method="POST"
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400)

            # Invalid type
            req = urllib.request.Request(
                self._url("/synthesize"),
                data=json.dumps({"type": "invalid", "text": "abc"}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400)

            # Empty text
            req = urllib.request.Request(
                self._url("/synthesize"),
                data=json.dumps({"type": "text", "text": "   "}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400)

            # Text too long
            req = urllib.request.Request(
                self._url("/synthesize"),
                data=json.dumps(
                    {"type": "text", "text": "a" * (app.MAX_TEXT_CHARS + 1)}
                ).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
