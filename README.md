# Voice Service 🎙️

A lightweight, high-reliability Text-to-Speech (TTS) microservice powered by Microsoft Edge's neural speech engine (unofficial, free) and Trafilatura web article extraction. Built with the Python standard library HTTP server (`http.server.ThreadingHTTPServer`) and `edge-tts`.

It is a standalone service: it accepts text or an article URL and returns synthesized MP3 audio. It has no knowledge of (and no dependency on) its callers.

---

## ✨ Features

- **High-quality neural voices**: native Greek neural voices (`el-GR-NestorasNeural`, `el-GR-AthinaNeural`) with adjustable rate, pitch, and volume.
- **Direct text or URL extraction**: synthesize raw text, or provide an article URL to auto-extract a cleaned body via `trafilatura`.
- **SSRF protection**: validates that URL targets resolve only to public IPs (blocks loopback, RFC-1918, link-local, multicast, broadcast).
- **Intelligent chunking**: splits long text into <= 1,000-char chunks at sentence boundaries (`.`, `!`, `?`, `;`, `…`, newlines).
- **Exponential backoff**: retries dropped/empty chunks up to 3 times (1s, 2s).
- **Content-addressable disk cache**: MP3s cached by `sha256(text|voice|rate|pitch|volume)`; repeat requests return in ~0ms.
- **Retention**: hourly prune of cached MP3s older than `CACHE_RETENTION_DAYS`.
- **Zero heavy frameworks**: stdlib `ThreadingHTTPServer`.
- **Security**: HTTP Basic Auth with constant-time comparison.

---

## 🔌 API Reference

### Health check
```http
GET /healthz
```
→ `200 OK` `{"ok": true}` (unauthenticated)

### Synthesize speech
```http
POST /synthesize
Authorization: Basic <base64(AUTH_USERNAME:AUTH_PASSWORD)>
Content-Type: application/json
```

Text payload:
```json
{
  "type": "text",
  "text": "Καλημέρα σας. Αυτές είναι οι σημαντικότερες ειδήσεις της ημέρας.",
  "voice": "el-GR-NestorasNeural",
  "rate": "+0%",
  "pitch": "+0Hz",
  "volume": "+0%"
}
```

URL payload:
```json
{ "type": "url", "url": "https://www.example.com/article-slug" }
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | string | yes | — | `"text"` or `"url"` |
| `text` | string | when `type="text"` | — | Plain text (max 20,000 chars) |
| `url` | string | when `type="url"` | — | Public HTTP(S) URL to extract |
| `voice` | string | no | `TTS_VOICE` | Edge TTS voice id |
| `rate` | string | no | `TTS_RATE` (`+0%`) | Speed |
| `pitch` | string | no | `TTS_PITCH` (`+0Hz`) | Pitch |
| `volume` | string | no | `TTS_VOLUME` (`+0%`) | Volume |

Response: `200` → binary `audio/mpeg` (24 kHz MP3); `400` invalid input/private URL; `401` auth; `413` body > 64 KB; `500` synthesis failure.

```bash
curl -u "user:pass" -X POST http://127.0.0.1:8002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"type":"text","text":"Δοκιμαστική αναπαραγωγή."}' --output test.mp3
```

---

## ⚙️ Configuration

| Variable | Default | Description |
|---|---|---|
| `VOICE_BIND` | `127.0.0.1` | Bind interface |
| `VOICE_PORT` | `8002` | Listen port |
| `AUTH_USERNAME` | *(required)* | Basic auth username |
| `AUTH_PASSWORD` | *(required)* | Basic auth password |
| `ALLOW_UNAUTHENTICATED` | `false` | Set `true` to disable auth (not recommended) |
| `TTS_VOICE` | `el-GR-NestorasNeural` | Default voice |
| `TTS_RATE` | `+0%` | Default rate |
| `TTS_PITCH` | `+0Hz` | Default pitch |
| `TTS_VOLUME` | `+0%` | Default volume |
| `CACHE_DIR` | `/tmp/voice-cache` | MP3 cache directory |
| `CACHE_RETENTION_DAYS` | `30` | Cache retention in days |

---

## 🚀 Deployment

### Docker Compose

A standalone [`docker-compose.yml`](docker-compose.yml) is included:

```bash
docker compose up -d
```

### Standalone Docker

```bash
docker build -t voice-service .
docker run -d --name voice-service \
  -e AUTH_USERNAME=myuser -e AUTH_PASSWORD=mypass \
  -e CACHE_DIR=/data/cache \
  -v /var/cache/voice:/data/cache \
  -p 127.0.0.1:8002:8002 \
  voice-service
```

### Local development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export AUTH_USERNAME="devuser" AUTH_PASSWORD="devpassword"
python3 app.py
```

---

## 🧪 Testing

```bash
python3 test_app.py
```

---

## 📄 License

MIT License. See repository for details.
