"""RAINGOD FastAPI Backend.

Provides a REST API over ComfyUI for the RAINGOD AI Music Kit visual
generation pipeline.

Endpoints
---------
GET  /                   Root / version info
GET  /health             Health check (backend + ComfyUI upstream)
GET  /config             Configuration summary
GET  /presets            All available presets
POST /generate           Single image generation
POST /batch-generate     Batch image generation
GET  /queue/status       ComfyUI queue state
DEL  /queue/{prompt_id}  Cancel a queued prompt
GET  /outputs/{filename} Retrieve a generated image file
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Path as FPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .comfyui_client import ComfyUIClient
from .rain_backend_config import (
    LORA_MAPPINGS,
    RESOLUTION_PRESETS,
    SAMPLER_PRESETS,
    QualityTier,
    config as rain_config,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, rain_config.logging.level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application & CORS
# ---------------------------------------------------------------------------
app = FastAPI(
    title="RAINGOD Visual Generation API",
    description="ComfyUI integration for the RAINGOD AI Music Kit",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# ComfyUI Client (singleton per worker)
# ---------------------------------------------------------------------------
client: ComfyUIClient | None = None
OUTPUT_DIR = Path("outputs")


@app.on_event("startup")
async def startup() -> None:
    global client
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    client = ComfyUIClient()
    logger.info(
        "RAINGOD backend started — ComfyUI: %s — GPU: %s",
        rain_config.comfyui.base_url,
        rain_config.gpu_tier.value,
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    logger.info("RAINGOD backend shutting down")


def _get_client() -> ComfyUIClient:
    if client is None:
        raise HTTPException(status_code=503, detail="Backend not initialised")
    return client


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4096, description="Positive prompt")
    negative_prompt: str = Field(default="", description="Negative prompt")
    preset: str = Field(default="quality", description="Sampler preset key")
    resolution: str = Field(default="cover_art", description="Resolution preset key")
    lora_style: str | None = Field(default=None, description="LoRA style key")
    seed: int | None = Field(default=None, ge=0, description="Random seed")
    quality_tier: QualityTier = Field(default=QualityTier.STANDARD)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BatchGenerateRequest(BaseModel):
    requests: list[GenerateRequest] = Field(..., min_length=1, max_length=50)
    priority: str = Field(default="normal", description="Queue priority hint")


class GenerateResponse(BaseModel):
    prompt_id: str
    job_id: str
    status: str
    estimated_time: str
    preset_used: str
    resolution_used: dict[str, int]
    metadata: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    comfyui_available: bool
    gpu_tier: str
    version: str
    uptime_seconds: float


# ---------------------------------------------------------------------------
# Helper: build a minimal txt2img workflow for ComfyUI
# ---------------------------------------------------------------------------

def _build_txt2img_workflow(
    positive: str,
    negative: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    sampler_name: str,
    scheduler: str,
    seed: int,
    lora_filename: str | None = None,
) -> dict[str, Any]:
    """Return a minimal ComfyUI API-format workflow (node graph dict)."""
    base: dict[str, Any] = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": positive, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "denoise": 1.0,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "raingod"},
        },
    }

    if lora_filename:
        # Insert LoRA loader between checkpoint and samplers
        base["8"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["1", 0],
                "clip": ["1", 1],
                "lora_name": lora_filename,
                "strength_model": 0.8,
                "strength_clip": 0.8,
            },
        }
        # Re-wire sampler to use LoRA output
        base["5"]["inputs"]["model"] = ["8", 0]
        base["2"]["inputs"]["clip"] = ["8", 1]
        base["3"]["inputs"]["clip"] = ["8", 1]

    return base


# ---------------------------------------------------------------------------
# Application startup time (for uptime reporting)
# ---------------------------------------------------------------------------
_START_TIME = time.monotonic()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", summary="Root")
async def root() -> dict[str, str]:
    return {
        "name": "RAINGOD Visual Generation API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    cl = _get_client()
    comfyui_ok = cl.health_check()
    return HealthResponse(
        status="healthy" if comfyui_ok else "degraded",
        comfyui_available=comfyui_ok,
        gpu_tier=rain_config.gpu_tier.value,
        version="1.0.0",
        uptime_seconds=time.monotonic() - _START_TIME,
    )


@app.get("/config", summary="Configuration summary")
async def get_config() -> dict[str, Any]:
    return {
        "comfyui_url": rain_config.comfyui.base_url,
        "gpu_tier": rain_config.gpu_tier.value,
        "resolution_presets": list(RESOLUTION_PRESETS.keys()),
        "sampler_presets": list(SAMPLER_PRESETS.keys()),
        "lora_styles": list(LORA_MAPPINGS.keys()),
        "batch_max_concurrent": rain_config.batch.max_concurrent,
        "cache_enabled": rain_config.cache.enabled,
    }


@app.get("/presets", summary="All presets")
async def get_presets() -> dict[str, Any]:
    return {
        "resolution": RESOLUTION_PRESETS,
        "samplers": {
            k: {
                "steps": v.steps,
                "cfg": v.cfg,
                "sampler_name": v.sampler_name,
                "scheduler": v.scheduler,
                "description": v.description,
            }
            for k, v in SAMPLER_PRESETS.items()
        },
        "lora": {
            k: {
                "filename": v.filename,
                "strength_model": v.strength_model,
                "description": v.description,
            }
            for k, v in LORA_MAPPINGS.items()
        },
        "quality_tiers": [t.value for t in QualityTier],
    }


@app.post("/generate", response_model=GenerateResponse, status_code=202)
async def generate(
    req: GenerateRequest,
    background_tasks: BackgroundTasks,
) -> GenerateResponse:
    """Submit a single image generation request to ComfyUI."""
    cl = _get_client()

    if req.preset not in SAMPLER_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {req.preset}")
    if req.resolution not in RESOLUTION_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown resolution: {req.resolution}")

    sampler = SAMPLER_PRESETS[req.preset]
    resolution = RESOLUTION_PRESETS[req.resolution]
    lora_filename = LORA_MAPPINGS[req.lora_style].filename if req.lora_style and req.lora_style in LORA_MAPPINGS else None
    seed = req.seed if req.seed is not None else int(uuid.uuid4().int % (2**32))

    workflow = _build_txt2img_workflow(
        positive=req.prompt,
        negative=req.negative_prompt,
        width=resolution["width"],
        height=resolution["height"],
        steps=sampler.steps,
        cfg=sampler.cfg,
        sampler_name=sampler.sampler_name,
        scheduler=sampler.scheduler,
        seed=seed,
        lora_filename=lora_filename,
    )

    try:
        prompt_id = cl.queue_prompt(workflow)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    estimated_steps = sampler.steps
    estimated_seconds = estimated_steps * 0.3  # rough estimate
    job_id = str(uuid.uuid4())

    logger.info(
        "Generation queued job_id=%s prompt_id=%s preset=%s resolution=%s",
        job_id,
        prompt_id,
        req.preset,
        req.resolution,
    )

    return GenerateResponse(
        prompt_id=prompt_id,
        job_id=job_id,
        status="queued",
        estimated_time=f"{estimated_seconds:.0f}s",
        preset_used=req.preset,
        resolution_used=resolution,
        metadata={**req.metadata, "seed": seed},
    )


@app.post("/batch-generate", status_code=202)
async def batch_generate(
    batch_req: BatchGenerateRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Submit multiple generation requests as a batch."""
    if len(batch_req.requests) > rain_config.batch.max_queue_size:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size exceeds max {rain_config.batch.max_queue_size}",
        )

    batch_id = str(uuid.uuid4())
    results = []

    for req in batch_req.requests:
        try:
            single_response = await generate(req, background_tasks)
            results.append({"status": "queued", "prompt_id": single_response.prompt_id})
        except HTTPException as exc:
            results.append({"status": "error", "detail": exc.detail})

    return {
        "batch_id": batch_id,
        "total": len(batch_req.requests),
        "queued": sum(1 for r in results if r["status"] == "queued"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }


@app.get("/queue/status", summary="ComfyUI queue status")
async def queue_status() -> dict[str, Any]:
    cl = _get_client()
    try:
        return cl.get_queue_status()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/queue/{prompt_id}", summary="Cancel a queued prompt")
async def cancel_queue_item(
    prompt_id: Annotated[str, FPath(description="prompt_id to cancel")],
) -> dict[str, Any]:
    cl = _get_client()
    cancelled = cl.cancel_prompt(prompt_id)
    return {"prompt_id": prompt_id, "cancelled": cancelled}


@app.get("/outputs/{filename}", summary="Retrieve generated image")
async def get_output(
    filename: Annotated[str, FPath(description="Output filename")],
) -> FileResponse:
    path = OUTPUT_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    # Prevent path traversal
    resolved = path.resolve()
    if not str(resolved).startswith(str(OUTPUT_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(resolved)
