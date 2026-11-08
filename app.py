"""voice-service — text/URL to speech via Microsoft Edge TTS (unofficial, free).

One endpoint: POST /synthesize

Request (JSON):
  { "type": "text", "text": "..." }            # speak this text directly
  { "type": "url",  "url":  "https://..." }    # extract article title+text, then speak it
  optional: voice (default el-GR-AthinaNeural), rate (+0%), pitch (+0Hz), volume (+0%)

Response: audio/mpeg bytes (24 kHz). Errors are JSON.

Auth: HTTP Basic (AUTH_USERNAME / AUTH_PASSWORD env). Fails closed at startup unless
ALLOW_UNAUTHENTICATED=true is explicitly set.

Runs on stdlib http.server (no web framework). Install: pip install -r requirements.txt
"""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import edge_tts
import trafilatura

AUTH_USER = os.getenv("AUTH_USERNAME", "")
AUTH_PASS = os.getenv("AUTH_PASSWORD", "")
ALLOW_UNAUTHENTICATED = os.getenv("ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true", "yes")

DEFAULT_VOICE = os.getenv("TTS_VOICE", "el-GR-NestorasNeural")
DEFAULT_RATE = os.getenv("TTS_RATE", "+0%")
DEFAULT_PITCH = os.getenv("TTS_PITCH", "+0Hz")
DEFAULT_VOLUME = os.getenv("TTS_VOLUME", "+0%")

# On-disk cache keyed by content hash, so retries/re-runs don't re-synthesize.
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/voice-cache")
CACHE_RETENTION_DAYS = int(os.getenv("CACHE_RETENTION_DAYS", "30"))

CHUNK_CHARS = 1000  # split long text into small chunks the service handles reliably
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_CHARS = 20000

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


def validate_public_url(url):
    """Validate that url uses http/https and does not resolve to private/loopback IPs."""
    if not url or not isinstance(url, str):
        raise ValueError("missing URL")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL scheme must be http or https")
    host = parsed.hostname
    if not host:
        raise ValueError("URL missing hostname")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host {host}: {exc}") from exc
    if not infos:
        raise ValueError(f"could not resolve host {host}")
    for info in infos:
        addr_str = info[4][0]
        ip = ipaddress.ip_address(addr_str)
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"disallowed target IP address for host {host}")
    return url.strip()


def extract_article(url):
    safe_url = validate_public_url(url)
    downloaded = trafilatura.fetch_url(safe_url)
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
    timeout = 30

    def log_message(self, fmt, *args):
        # Keep healthz checks quiet; log errors or non-200 responses.
        pass

    def check_auth(self):
        if ALLOW_UNAUTHENTICATED and (not AUTH_USER or not AUTH_PASS):
            return True
        if not AUTH_USER or not AUTH_PASS:
            self.send_json(500, {"error": "server authentication not configured"})
            return False
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
        if not (hmac.compare_digest(u, AUTH_USER) and hmac.compare_digest(p, AUTH_PASS)):
            self.send_json(401, {"error": "unauthorized"})
            return False
        return True

    def send_json(self, status, obj, extra_headers=None):
        if status >= 400 and status != 401:
            log_err(f"[voice-service] HTTP {status} {self.path}: {obj}")
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
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
        except ValueError:
            self.send_json(400, {"error": "invalid Content-Length"})
            return

        if length < 0 or length > MAX_BODY_BYTES:
            self.send_json(413, {"error": f"request body too large (max {MAX_BODY_BYTES} bytes)"})
            return

        try:
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw_body)
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
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
            if not isinstance(text, str):
                self.send_json(400, {"error": "text must be a string"})
                return
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
        if len(text) > MAX_TEXT_CHARS:
            self.send_json(400, {"error": f"text too long (max {MAX_TEXT_CHARS} characters)"})
            return

        try:
            audio = asyncio.run(synth(text, voice, rate, pitch, volume))
        except Exception as exc:
            self.send_json(500, {"error": f"synthesis failed: {exc}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(len_bytes := audio)))
        self.end_headers()
        self.wfile.write(len_bytes)


def main():
    if not ALLOW_UNAUTHENTICATED and (not AUTH_USER or not AUTH_PASS):
        log_err("FATAL: AUTH_USERNAME and AUTH_PASSWORD must be set in the environment")
        sys.exit(1)
    port = int(os.getenv("VOICE_PORT", "8002"))
    bind = os.getenv("VOICE_BIND", "127.0.0.1")
    server = ThreadingHTTPServer((bind, port), Handler)
    print(f"voice-service listening on http://{bind}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
