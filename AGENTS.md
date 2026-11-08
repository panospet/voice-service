# AGENTS.md — voice-service

Guidance for AI agents and humans working in this repository.

## What this repo is

A lightweight, zero-cost, high-reliability Text-to-Speech (TTS) microservice in Python:
- **TTS Engine**: Microsoft Edge's unofficial neural speech API via `edge_tts`. Default voice `el-GR-NestorasNeural` (male) or `el-GR-AthinaNeural` (female).
- **Article Extractor**: `trafilatura` extracts clean title + body when given a web article URL (`type: "url"`).
- **Server**: Python standard library `http.server.ThreadingHTTPServer` (no Flask/FastAPI/Uvicorn overhead).

## The 4-Service Ecosystem Context

This repository is the dedicated speech engine within a 4-service ecosystem:
- **`media-stack`**: Orchestrates `voice-service` as a Docker container (`media-stack/docker-compose.yml`, context `../voice-service`), network mode `host`, bound to `127.0.0.1:8002`, volume-mounting persistent cache `${BASE_DIR}/voice-cache` to `/app/cache`.
- **`audio-briefing`**: Primary consumer. Calls `POST /synthesize` (Basic Auth) on port `8002` to render Greek news summaries and spoken DJ track intros.
- **`daily-news-aggregator`**: Secondary consumer. Can call `POST /synthesize` for in-browser narrated articles.

## Ports & Auth

| Port | Bind | Protocol | Auth |
|---|---|---|---|
| `8002` | `127.0.0.1` (configurable via `VOICE_BIND`) | HTTP/1.1 | HTTP Basic (`AUTH_USERNAME` / `AUTH_PASSWORD`) |

- **Auth check**: Constant-time `hmac.compare_digest`. Fails closed at startup unless `ALLOW_UNAUTHENTICATED=true`.
- **Health check**: `GET /healthz` is unauthenticated and returns `{"ok": true}`.

## Core Invariants & Architecture

1. **Chunking & Pacing**:
   - Long text is chunked into <= 1,000 characters prioritizing sentence boundaries (`.`, `!`, `?`, `;`, `…`, `\n`) or whitespace (`split_chunks`).
   - Paces chunks with `await asyncio.sleep(0.8)` to avoid Edge TTS connection resets.
2. **Exponential Backoff Retries**:
   - Upstream Edge TTS intermittently returns 0 bytes under load.
   - Each chunk retries up to 3 times with exponential backoff (`2 ** attempt` = 1s, 2s).
3. **Content-Addressable Disk Cache**:
   - Audio output is cached in `CACHE_DIR` keyed by `sha256("{text}|{voice}|{rate}|{pitch}|{volume}") + ".mp3"`.
   - Writes are atomic (`tmp` file then `os.replace`). Cache hits return in ~0ms.
   - `maybe_prune_cache()`: Hourly check deletes cached files older than `CACHE_RETENTION_DAYS` (default 30).
4. **SSRF Protection (`validate_public_url`)**:
   - For `type: "url"`, resolves the hostname via `socket.getaddrinfo` and rejects loopback, RFC-1918 private, link-local, multicast, or reserved IPs before passing to `trafilatura`.

## Development & Testing

```bash
# Run unit tests (verifies chunking, auth, URL validation, synthesis error handling):
python3 test_app.py

# Run standalone locally:
export AUTH_USERNAME="dev"
export AUTH_PASSWORD="pass"
python3 app.py
```

## Conventions & Rules

- Keep dependencies minimal (standard library HTTP server, `edge-tts`, `trafilatura`). Do not add heavy web frameworks.
- Preserve constant-time comparison on auth and SSRF validation on URLs.
- Always maintain atomic writes for disk cache files (`.tmp` -> rename).
