# RAINGOD ComfyUI Integration — Architecture

> **Accuracy Notice**: This document reflects the currently implemented code.
> Features marked 🔲 Planned are not yet in the repository.

---

## System Overview

```
RAINGOD AI Music Kit
┌──────────────────────────────────────────┐
│                                          │
│  ┌───────────┐  HTTP  ┌───────────┐  │
│  │ Client /   ├──────►│  FastAPI   │  │
│  │ example.py │       │  Backend  │  │
│  └───────────┘       └──────┬───┘  │
│                             │          │
│                        HTTP │          │
│                             │          │
│                      ┌─────┴─────┐    │
│                      │  ComfyUI  │    │
│                      │  :8188    │    │
│                      └───────────┘    │
│                                          │
└──────────────────────────────────────────┘
```

---

## Component Status

| Component | File | Status | Notes |
|-----------|------|--------|-------|
| **Config** | `backend/rain_backend_config.py` | ✅ Implemented | Dataclasses, GPU detection, all presets |
| **ComfyUI Client** | `backend/comfyui_client.py` | ✅ Implemented | Circuit breaker, retry, dedup, polling |
| **FastAPI Backend** | `backend/rain_backend.py` | ✅ Implemented | 9 endpoints, Pydantic models |
| **Album Art Example** | `examples/generate_album_art.py` | ✅ Implemented | Full CLI, 5 style presets |
| **Quickstart Script** | `scripts/rain_quickstart.sh` | ✅ Implemented | System checks, env setup |
| **Start All Script** | `scripts/start_all.sh` | ✅ Implemented | Service orchestration |
| **Docker** | `Dockerfile` | ✅ Implemented | Multi-stage, non-root user |
| **docker-compose** | `docker-compose.yml` | ✅ Implemented | ComfyUI + backend services |
| **CI Pipeline** | `.github/workflows/ci.yml` | ✅ Implemented | lint/test/docker-build |
| **Workflow Templates** | `workflows/*.json` | 🔲 Planned | ComfyUI JSON templates |
| **Workflow Builder** | `backend/workflow_builder.py` | 🔲 Planned | Dynamic workflow construction |
| **LoRA Manager** | `backend/lora_manager.py` | 🔲 Planned | LoRA loading + blending |
| **Test Suite** | `tests/` | 🔲 Planned | pytest coverage for all endpoints |
| **Switchboard UI** | `switchboard/` | 🔲 Planned | HTML production dashboard |
| **Audio-Visual Sync** | `backend/av_sync.py` | 🔲 Planned | Beat detection integration |

---

## Backend REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Version and links |
| GET | `/health` | Backend + ComfyUI health status |
| GET | `/config` | Active configuration summary |
| GET | `/presets` | All resolution / sampler / LoRA presets |
| POST | `/generate` | Single image generation (async) |
| POST | `/batch-generate` | Batch image generation |
| GET | `/queue/status` | ComfyUI queue state |
| DELETE | `/queue/{prompt_id}` | Cancel a queued prompt |
| GET | `/outputs/{filename}` | Retrieve a generated file |

---

## Request Flow

```
Client
  │
  ├─ POST /generate
  │     │
  │     ├─ Validate request (Pydantic)
  │     ├─ Resolve preset + resolution
  │     ├─ Build txt2img workflow graph
  │     ├─ ComfyUIClient.queue_prompt()
  │     │     ├─ Circuit breaker check
  │     │     ├─ SHA-256 dedup check
  │     │     ├─ POST /prompt → ComfyUI
  │     │     └─ Return prompt_id
  │     └─ Return 202 GenerateResponse
  │
  └─ GET /outputs/{filename}  (poll until ready)
```

---

## Circuit Breaker

The `ComfyUIClient` embeds a circuit breaker with three states:

| State | Behaviour |
|-------|----------|
| `CLOSED` | Normal operation — all requests pass through |
| `OPEN` | ComfyUI unreachable; requests fail immediately with 503 |
| `HALF_OPEN` | Probe request sent after 60s cooldown; recovers on success |

Failure threshold: **5 consecutive failures** before opening.

---

## Configuration

All runtime configuration lives in `backend/rain_backend_config.py` and is
loaded via the module-level `config` singleton. Environment variables
override defaults for `COMFYUI_HOST` and `COMFYUI_PORT`.

```bash
export COMFYUI_HOST=192.168.1.100
export COMFYUI_PORT=8188
uvicorn backend.rain_backend:app --host 0.0.0.0 --port 8000
```

---

## Known Issues

### 12 MB GIF Binary in Repository

`DEVIANT2026_small.gif` (12,109,044 bytes) is committed directly to Git
history. This bloats every clone. **Recommended action:**

```bash
# 1. Install Git LFS
git lfs install
git lfs track "*.gif"
git add .gitattributes

# 2. Re-add the file (LFS will store it outside the pack)
git rm --cached DEVIANT2026_small.gif
git add DEVIANT2026_small.gif
git commit -m "chore: migrate DEVIANT2026_small.gif to Git LFS"
git push
```

Alternatively, host the GIF on a CDN and replace the README `<img>` tag
with a URL.

---

## Security Notes

- The FastAPI backend sets `allow_origins=["*"]` in CORS middleware. Restrict
  this to known origins before public deployment.
- The Docker image runs as non-root user `raingod` (UID 1000).
- `GET /outputs/{filename}` includes path-traversal protection via
  `Path.resolve()` comparison against the `outputs/` directory.
- Never commit `.env` files — the `.gitignore` excludes them.
