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
    HandoffStatus,
    HealthResponse,
    PlanDecision,
    ReviewDecision,
    ReviewItem,
    ReviewStatus,
    Run,
    RunPlan,
    SourceDocument,
)
from csuite_common.roles import ROLE_REGISTRY, get_role, number_lines, sha256_of
from csuite_common.secrets import SecretResolutionError, resolve_secret

from decision_log import build_decision_log
from engine import (
    PlanNotPendingError,
    ReviewNotFoundError,
    RunEngine,
    RunNotFoundError,
)

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


@app.get("/summary/{run_id}", include_in_schema=False)
async def summary_page(run_id: str) -> FileResponse:
    """The CEO-facing brief. The dashboard is the how; this is the what."""
    page = STATIC_DIR / "summary.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="Summary asset not found.")
    return FileResponse(page)


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
        "routing": {
            "mode": settings.routing_mode,
            "min_roles": settings.routing_min_roles,
            "max_roles": settings.routing_max_roles,
        },
        "hitl": {
            "enabled": settings.hitl_enabled,
            "confidence_floor": settings.hitl_confidence_floor,
            "require_citations": settings.hitl_require_citations,
            "final_signoff": settings.hitl_final_signoff,
            "plan_approval": settings.hitl_plan_approval,
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

    from csuite_common.auth import (
        ServiceAuthError,
        build_auth_headers,
        describe_token,
        diagnose_call_failure,
    )

    results = []
    urls = settings.agent_urls
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        for role_key in settings.active_roles:
            base = urls.get(role_key)
            if not base:
                results.append(
                    {
                        "role": role_key,
                        "ok": False,
                        "detail": "No URL configured for this role. Check AGENT_URLS_JSON.",
                    }
                )
                continue

            entry: dict = {"role": role_key, "url": base, "ok": False}
            try:
                headers = build_auth_headers(
                    audience=base, mode=settings.service_auth_mode
                )
                # Non-secret claims only -- never the token itself.
                entry["caller_identity"] = describe_token(headers.get("Authorization", ""))

                response = await client.get(f"{base}/healthz", headers=headers)
                entry["http_status"] = response.status_code
                if response.status_code == 200:
                    entry["ok"] = True
                    entry["detail"] = response.json()
                else:
                    entry["detail"] = diagnose_call_failure(
                        response.status_code, response.text, base
                    )
            except ServiceAuthError as exc:
                entry["detail"] = f"Could not mint an identity token: {exc}"
            except Exception as exc:  # noqa: BLE001
                entry["detail"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)

    return {
        "all_ok": bool(results) and all(r["ok"] for r in results),
        "service_auth_mode": settings.service_auth_mode,
        "agents": results,
    }


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


@app.post("/api/runs/{run_id}/plan", response_model=RunPlan)
async def resolve_plan(run_id: str, decision: PlanDecision) -> RunPlan:
    """Approve or amend the routing plan. This unblocks dispatch."""
    try:
        return await _engine().resolve_plan(run_id=run_id, decision=decision)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc
    except PlanNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@app.get("/api/runs/{run_id}/summary")
async def get_summary(run_id: str) -> dict:
    """The CEO-facing view of a run.

    Deliberately the Orchestrator's consolidated position -- not five
    independent executive summaries. What the CEO needs is one answer, plus
    honest provenance: who was consulted, what was held back for a human, and
    what the human changed.
    """
    try:
        run = _engine().get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc

    total_findings = sum(len(f.findings) for f in run.findings)
    escalated = [r for r in run.reviews if r.reason.value != "final_signoff"]
    by_status = {
        status.value: len([r for r in escalated if r.status == status])
        for status in ReviewStatus
    }
    signoff = next(
        (r for r in run.reviews if r.reason.value == "final_signoff"), None
    )

    duration_seconds = None
    if run.completed_at:
        duration_seconds = int((run.completed_at - run.created_at).total_seconds())

    return {
        "run_id": run.id,
        "status": run.status.value,
        "objective": run.objective,
        "document": (
            {
                "filename": run.document.filename,
                "line_count": run.document.line_count,
                "sha256": run.document.sha256,
            }
            if run.document
            else None
        ),
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "duration_seconds": duration_seconds,
        "plan": (
            {
                "interpretation": run.plan.interpretation,
                "strategy": run.plan.strategy,
                "approved_by_human": run.plan.approved_by_human,
                "amended_by_human": run.plan.amended_by_human,
                "reviewer_note": run.plan.reviewer_note,
                "engaged": [r.model_dump(mode="json") for r in run.plan.engaged],
                "skipped": [r.model_dump(mode="json") for r in run.plan.skipped],
            }
            if run.plan
            else None
        ),
        "synthesis": run.synthesis.model_dump(mode="json") if run.synthesis else None,
        "provenance": {
            "executives_engaged": len(
                [h for h in run.handoffs if h.status == HandoffStatus.SUCCEEDED]
            ),
            "executives_available": len(settings.active_roles),
            "findings_produced": total_findings,
            "findings_auto_accepted": total_findings - len(escalated),
            "findings_escalated": len(escalated),
            "human_approved": by_status.get("approved", 0),
            "human_edited": by_status.get("edited", 0),
            "human_rejected": by_status.get("rejected", 0),
            "timed_out": by_status.get("timed_out", 0),
            "final_signoff": signoff.status.value if signoff else "not_required",
            "signoff_note": signoff.reviewer_note if signoff else "",
        },
        # Every point at which a human changed the outcome. This is the
        # anti-hallucination story told as evidence rather than assertion.
        "human_interventions": [
            {
                "role_title": r.role_title,
                "action": r.status.value,
                "reason": r.reason.value,
                "headline": r.original_headline,
                "note": r.reviewer_note,
                "at": r.resolved_at,
            }
            for r in run.reviews
            if r.status
            in {ReviewStatus.EDITED, ReviewStatus.REJECTED, ReviewStatus.APPROVED}
            and r.resolved_at is not None
        ],
        "model": settings.model_name,
    }


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
