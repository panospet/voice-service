# AGENTS.md — voice-service

Guidance for AI agents and humans working in this repository.

## What this repo is

A lightweight, zero-cost, high-reliability Text-to-Speech (TTS) microservice in Python:
- **TTS engine**: Microsoft Edge's unofficial neural speech API via `edge_tts`. Default voice `el-GR-NestorasNeural` or `el-GR-AthinaNeural`.
- **Article extractor**: `trafilatura` extracts a clean title + body from a web article URL (`type: "url"`).
- **Server**: Python stdlib `http.server.ThreadingHTTPServer` (no Flask/FastAPI).

This service is self-contained. It does not know or care who calls it — any client that can `POST /synthesize` with Basic Auth is a valid consumer.

## Ports & Auth

| Port | Bind | Protocol | Auth |
|---|---|---|---|
| `8002` | `127.0.0.1` (configurable via `VOICE_BIND`) | HTTP/1.1 | HTTP Basic (`AUTH_USERNAME` / `AUTH_PASSWORD`) |

- **Auth check**: constant-time `hmac.compare_digest`. Fails closed at startup unless `ALLOW_UNAUTHENTICATED=true`.
- **Health check**: `GET /healthz` is unauthenticated, returns `{"ok": true}`.

## Core Invariants & Architecture

1. **Chunking & pacing**: text chunked into <= 1,000 chars at sentence boundaries; `await asyncio.sleep(0.8)` between chunks.
2. **Exponential backoff**: each chunk retries up to 3 times (`2 ** attempt` = 1s, 2s) on empty/dropped responses.
3. **Content-addressable disk cache**: audio cached in `CACHE_DIR` keyed by `sha256("{text}|{voice}|{rate}|{pitch}|{volume}") + ".mp3"`; atomic writes (`tmp` then `os.replace`).
4. **SSRF protection (`validate_public_url`)**: resolves the hostname and rejects loopback/private/link-local/multicast/reserved IPs before passing to `trafilatura`.

## Development & Testing

```bash
python3 test_app.py   # chunking, auth, URL validation, synthesis error handling

export AUTH_USERNAME="dev" AUTH_PASSWORD="pass"
python3 app.py
```

## Conventions & Rules

- Keep dependencies minimal (stdlib HTTP server, `edge-tts`, `trafilatura`); no heavy web frameworks.
- Preserve constant-time auth comparison and SSRF validation on URLs.
- Always write disk cache files atomically (`.tmp` -> rename).
