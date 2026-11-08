"""voice-service — text/URL to speech via Microsoft Edge TTS (unofficial, free).

One endpoint: POST /synthesize

Request (JSON):
  { "type": "text", "text": "..." }            # speak this text directly
  { "type": "url",  "url":  "https://..." }    # extract article title+text, then speak it
  optional: voice (default el-GR-AthinaNeural), rate (+0%), pitch (+0Hz), volume (+0%)

Response: audio/mpeg bytes (24 kHz). Errors are JSON.

Auth: HTTP Basic (AUTH_USERNAME / AUTH_PASSWORD env). If either is unset, auth is off.

Runs on stdlib http.server (no web framework). Install: pip install -r requirements.txt
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts
import trafilatura

AUTH_USER = os.getenv("AUTH_USERNAME", "")
AUTH_PASS = os.getenv("AUTH_PASSWORD", "")

DEFAULT_VOICE = os.getenv("TTS_VOICE", "el-GR-NestorasNeural")
DEFAULT_RATE = os.getenv("TTS_RATE", "+0%")
DEFAULT_PITCH = os.getenv("TTS_PITCH", "+0Hz")
DEFAULT_VOLUME = os.getenv("TTS_VOLUME", "+0%")

# On-disk cache keyed by content hash, so retries/re-runs don't re-synthesize.
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/voice-cache")
CACHE_RETENTION_DAYS = int(os.getenv("CACHE_RETENTION_DAYS", "30"))

CHUNK_CHARS = 1000  # split long text into small chunks the service handles reliably

_last_prune_ts = 0.0
_prune_lock = threading.Lock()


def log_err(msg):
    print(msg, file=sys.stderr, flush=True)


def split_chunks(text, size):
    """Split text into non-empty chunks of at most `size` chars, preferring sentence breaks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        if end >= len(text):
            tail = text[start:].strip()
            if tail:
                chunks.append(tail)
            break
        window = start + int(size * 0.5)
        cut = 0
        for i in range(end, window, -1):
            if text[i - 1] in ".!?;…\n":
                cut = i
                break
        if cut == 0:
            for i in range(end, window, -1):
                if text[i - 1].isspace():
                    cut = i
                    break
        if cut == 0:
            cut = end
        piece = text[start:cut].strip()
        if piece:
            chunks.append(piece)
        start = cut
    return chunks


def maybe_prune_cache():
    """Remove cached MP3 files older than CACHE_RETENTION_DAYS (at most once per hour)."""
    global _last_prune_ts
    if CACHE_RETENTION_DAYS <= 0:
        return
    now = time.time()
    if now - _last_prune_ts < 3600:
        return
    with _prune_lock:
        if now - _last_prune_ts < 3600:
            return
        _last_prune_ts = now
        cutoff = now - (CACHE_RETENTION_DAYS * 86400)
        try:
            for name in os.listdir(CACHE_DIR):
                if not name.endswith(".mp3"):
                    continue
                path = os.path.join(CACHE_DIR, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass


async def synth(text, voice, rate, pitch, volume):
    key = hashlib.sha256(f"{text}|{voice}|{rate}|{pitch}|{volume}".encode("utf-8")).hexdigest()
    cache_path = os.path.join(CACHE_DIR, key + ".mp3")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = f.read()
            if cached:
                return cached
        except OSError:
            pass

    chunks = split_chunks(text, CHUNK_CHARS)
    if not chunks:
        raise ValueError("no text to synthesize")

    out = bytearray()
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            await asyncio.sleep(0.8)  # gentle pacing between chunks
        # Retry each chunk with backoff — the service intermittently returns
        # "no audio" under load, and a short retry usually succeeds.
        last_err = None
        chunk_buf = bytearray()
        for attempt in range(3):
            chunk_buf.clear()
            try:
                communicate = edge_tts.Communicate(
                    chunk, voice=voice, rate=rate, pitch=pitch, volume=volume
                )
                async for piece in communicate.stream():
                    if piece["type"] == "audio":
                        chunk_buf.extend(piece["data"])
                if not chunk_buf:
                    raise RuntimeError("edge-tts returned 0 audio bytes")
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                log_err(
                    f"[voice-service] chunk {idx + 1}/{len(chunks)} attempt {attempt + 1}/3 failed: {exc}"
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff
        if last_err is not None:
            raise last_err
        out.extend(chunk_buf)

    if not out:
        raise RuntimeError("synthesis produced 0 audio bytes")

    audio = bytes(out)
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{cache_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(audio)
    os.replace(tmp, cache_path)
    maybe_prune_cache()
    return audio


def extract_article(url):
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ValueError("could not fetch article at URL")
    body = trafilatura.extract(downloaded, include_comments=False, include_tables=False) or ""
    title = ""
    try:
        meta = trafilatura.extract_metadata(downloaded)
        if meta and meta.title:
            title = meta.title
    except Exception:
        title = ""
    if title:
        return f"{title.strip()}. {body.strip()}"
    return body.strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quieter logs

    def check_auth(self):
        if not AUTH_USER or not AUTH_PASS:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            self.send_json(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Basic"})
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            u, _, p = decoded.partition(":")
        except Exception:
            self.send_json(401, {"error": "unauthorized"})
            return False
        if u != AUTH_USER or p != AUTH_PASS:
            self.send_json(401, {"error": "unauthorized"})
            return False
        return True

    def send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {"ok": True})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/synthesize":
            self.send_json(404, {"error": "not found"})
            return
        if not self.check_auth():
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        typ = body.get("type")
        voice = body.get("voice") or DEFAULT_VOICE
        rate = body.get("rate") or DEFAULT_RATE
        pitch = body.get("pitch") or DEFAULT_PITCH
        volume = body.get("volume") or DEFAULT_VOLUME

        if typ == "text":
            text = body.get("text", "")
        elif typ == "url":
            try:
                text = extract_article(body.get("url", ""))
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
                return
        else:
            self.send_json(400, {"error": "type must be 'text' or 'url'"})
            return

        text = text.strip()
        if not text:
            self.send_json(400, {"error": "no text to synthesize"})
            return

        try:
            audio = asyncio.run(synth(text, voice, rate, pitch, volume))
        except Exception as exc:
            self.send_json(500, {"error": f"synthesis failed: {exc}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


def main():
    port = int(os.getenv("VOICE_PORT", "8002"))
    bind = os.getenv("VOICE_BIND", "127.0.0.1")
    server = ThreadingHTTPServer((bind, port), Handler)
    print(f"voice-service listening on http://{bind}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
