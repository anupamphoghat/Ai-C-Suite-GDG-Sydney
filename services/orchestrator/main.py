"""Orchestrator service.

Public entry point for the demo. Serves the dashboard, accepts the uploaded
document plus a CEO objective, and drives the executive agents over HTTP.

Deployed to Cloud Run with public access; the five executive services it
calls are private.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from csuite_common.config import orchestrator_settings
from csuite_common.llm import GeminiClient, LLMError
from csuite_common.models import (
    DecisionLogEntry,
    HealthResponse,
    ReviewDecision,
    ReviewItem,
    Run,
    SourceDocument,
)
from csuite_common.roles import ROLE_REGISTRY, get_role, number_lines, sha256_of
from csuite_common.secrets import SecretResolutionError, resolve_secret

from decision_log import build_decision_log
from engine import ReviewNotFoundError, RunEngine, RunNotFoundError

settings = orchestrator_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("orchestrator")

STATIC_DIR = Path(__file__).parent / "static"


class _State:
    engine: Optional[RunEngine] = None
    init_error: str = ""


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    decision_log = build_decision_log(
        backend=settings.decision_log_backend,
        project_id=settings.gcp_project_id,
        collection=settings.firestore_collection,
        database=settings.firestore_database,
    )

    llm: Optional[GeminiClient] = None
    try:
        api_key = resolve_secret(
            inline_value=settings.gemini_api_key,
            project_id=settings.gcp_project_id,
            secret_name=settings.gemini_api_key_secret,
            version=settings.gemini_api_key_secret_version,
            label="Gemini API key",
        )
        llm = GeminiClient(
            api_key=api_key,
            model_name=settings.model_name,
            temperature=settings.model_temperature,
            max_output_tokens=settings.model_max_output_tokens,
        )
    except (SecretResolutionError, LLMError) as exc:
        # The orchestrator can still demonstrate handoff and the decision log
        # without a synthesis model, so degrade rather than refuse to start.
        state.init_error = str(exc)
        logger.error("Synthesis model unavailable: %s", exc)

    state.engine = RunEngine(settings=settings, decision_log=decision_log, llm=llm)
    logger.info(
        "Orchestrator ready: roles=%s agents=%d auth=%s hitl=%s",
        settings.active_roles,
        len(settings.agent_urls),
        settings.service_auth_mode,
        settings.hitl_enabled,
    )
    yield


app = FastAPI(
    title="AI C-Suite Orchestrator",
    description="Multi-agent executive committee on Google Cloud Run.",
    version="1.0.0",
    lifespan=lifespan,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _engine() -> RunEngine:
    if state.engine is None:
        raise HTTPException(status_code=503, detail="Orchestrator is still starting.")
    return state.engine


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Dashboard asset not found.")
    return FileResponse(index)


# --------------------------------------------------------------------------
# Health and configuration
# --------------------------------------------------------------------------


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", role="orchestrator", model=settings.model_name)


@app.get("/api/config")
async def config() -> dict:
    """Everything the dashboard needs to render itself. No secrets."""
    urls = settings.agent_urls
    return {
        "model": settings.model_name,
        "region": settings.gcp_region,
        "service_auth_mode": settings.service_auth_mode,
        "decision_log_backend": settings.decision_log_backend,
        "synthesis_available": not state.init_error,
        "init_error": state.init_error,
        "hitl": {
            "enabled": settings.hitl_enabled,
            "confidence_floor": settings.hitl_confidence_floor,
            "require_citations": settings.hitl_require_citations,
            "final_signoff": settings.hitl_final_signoff,
            "timeout_seconds": settings.hitl_timeout_seconds,
        },
        "upload": {
            "max_bytes": settings.max_upload_bytes,
            "allowed_extensions": sorted(settings.allowed_upload_extensions),
        },
        "roles": [
            {
                "key": key,
                "title": spec.title,
                "short_title": spec.short_title,
                "accent": spec.accent,
                "lens": spec.lens,
                "endpoint": urls.get(key, ""),
                "configured": key in urls,
            }
            for key, spec in ROLE_REGISTRY.items()
            if key in settings.active_roles
        ],
    }


@app.get("/api/agents/health")
async def agents_health() -> dict:
    """Probe every executive service so a failure is visible before the demo."""
    import httpx

    from csuite_common.auth import ServiceAuthError, build_auth_headers

    results = []
    urls = settings.agent_urls
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        for role_key in settings.active_roles:
            base = urls.get(role_key)
            if not base:
                results.append(
                    {"role": role_key, "ok": False, "detail": "No URL configured."}
                )
                continue
            try:
                headers = build_auth_headers(
                    audience=base, mode=settings.service_auth_mode
                )
                response = await client.get(f"{base}/healthz", headers=headers)
                results.append(
                    {
                        "role": role_key,
                        "ok": response.status_code == 200,
                        "http_status": response.status_code,
                        "detail": response.text[:200],
                        "url": base,
                    }
                )
            except ServiceAuthError as exc:
                results.append({"role": role_key, "ok": False, "detail": str(exc), "url": base})
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "role": role_key,
                        "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                        "url": base,
                    }
                )
    return {"agents": results, "all_ok": all(r["ok"] for r in results)}


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


@app.post("/api/runs", response_model=Run)
async def start_run(
    objective: str = Form(...),
    file: Optional[UploadFile] = File(default=None),
) -> Run:
    """Kick off a C-suite engagement from an objective and an uploaded file."""
    objective = objective.strip()
    if not objective:
        raise HTTPException(status_code=422, detail="An objective is required.")

    document: Optional[SourceDocument] = None
    if file is not None and file.filename:
        document = await _read_upload(file)

    run = await _engine().start_run(objective=objective, document=document)
    return run


async def _read_upload(file: UploadFile) -> SourceDocument:
    suffix = Path(file.filename).suffix.lower()
    allowed = settings.allowed_upload_extensions
    if suffix not in allowed:
        raise HTTPException(
            status_code=415,
            detail=f"'{suffix}' is not an accepted file type. Allowed: {', '.join(sorted(allowed))}.",
        )

    raw = await file.read()
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {settings.max_upload_bytes} byte limit.",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="File must be UTF-8 text.") from exc
    if not text.strip():
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")

    return SourceDocument(
        filename=file.filename,
        content_type=file.content_type or "text/markdown",
        numbered_content=number_lines(text),
        line_count=len(text.splitlines()),
        sha256=sha256_of(text),
    )


@app.get("/api/runs", response_model=List[Run])
async def list_runs() -> List[Run]:
    return _engine().list_runs()


@app.get("/api/runs/{run_id}", response_model=Run)
async def get_run(run_id: str) -> Run:
    try:
        return _engine().get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc


@app.get("/api/runs/{run_id}/decisions", response_model=List[DecisionLogEntry])
async def get_decisions(run_id: str) -> List[DecisionLogEntry]:
    try:
        return _engine().get_run(run_id).decision_log
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events: the live feed the dashboard renders."""
    try:
        engine = _engine()
        run = engine.get_run(run_id)
        queue = engine.subscribe(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc

    async def event_stream():
        try:
            # Replay current state so a late-joining dashboard is never behind.
            yield _sse("snapshot", run.model_dump(mode="json"))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(message["event"], message["data"])
        finally:
            engine.unsubscribe(run_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# --------------------------------------------------------------------------
# Human in the loop
# --------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/reviews", response_model=List[ReviewItem])
async def list_reviews(run_id: str) -> List[ReviewItem]:
    try:
        return _engine().get_run(run_id).reviews
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc


@app.post("/api/runs/{run_id}/reviews/{review_id}", response_model=ReviewItem)
async def resolve_review(
    run_id: str, review_id: str, decision: ReviewDecision
) -> ReviewItem:
    """Approve, edit or reject an escalated finding. This unblocks the run."""
    try:
        return await _engine().resolve_review(
            run_id=run_id, review_id=review_id, decision=decision
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No review '{review_id}'.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/source")
async def get_source(run_id: str) -> dict:
    """The numbered source document, so a reviewer can verify a citation."""
    try:
        run = _engine().get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc
    if run.document is None:
        return {"filename": None, "lines": []}
    return {
        "filename": run.document.filename,
        "sha256": run.document.sha256,
        "line_count": run.document.line_count,
        "lines": run.document.numbered_content.splitlines(),
    }
