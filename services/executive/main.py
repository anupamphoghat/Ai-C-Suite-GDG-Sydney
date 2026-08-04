"""Executive agent service.

One container image, five Cloud Run deployments. ``EXEC_ROLE`` selects the
persona (cfo | cso | cmo | chro | cto) and therefore which SKILL.md is loaded
as the system instruction.

The service exposes a single working endpoint, ``POST /invoke``, which the
Orchestrator calls over HTTP. These services are deployed privately
(--no-allow-unauthenticated), so the only caller is the Orchestrator's
service account.

Anti-hallucination measures enforced here:
  * output is schema-constrained -- the model cannot return free prose;
  * every finding must carry a self-reported confidence;
  * every finding must cite the numbered lines of the source document it
    relies on, and uncited findings are down-ranked and flagged;
  * anything the agent cannot support from the source goes into
    ``open_questions`` rather than into a claim.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from csuite_common.config import executive_settings
from csuite_common.llm import GeminiClient, LLMError
from csuite_common.models import (
    AgentFinding,
    Citation,
    Finding,
    HealthResponse,
    InvokeRequest,
)
from csuite_common.roles import RoleSpec, get_role, load_context, load_skill
from csuite_common.secrets import SecretResolutionError, resolve_secret

settings = executive_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("executive")


# --------------------------------------------------------------------------
# Model output schema (what Gemini must return)
# --------------------------------------------------------------------------


class _Citation(BaseModel):
    line_start: int = Field(..., description="First source line number relied on.")
    line_end: int = Field(..., description="Last source line number relied on.")
    quote: str = Field(..., description="Verbatim excerpt from those lines.")


class _Finding(BaseModel):
    headline: str = Field(..., description="One sentence stating the finding.")
    detail: str = Field(..., description="2-4 sentences of reasoning.")
    confidence: float = Field(
        ...,
        description=(
            "0.0-1.0. Your honest confidence that this finding is fully supported "
            "by the source document. Use below 0.75 whenever you are inferring "
            "beyond what the document states."
        ),
    )
    citations: List[_Citation] = Field(
        ..., description="Source lines supporting this finding. Empty only if none exist."
    )
    requires_human_review: bool = Field(
        ..., description="True if a human should verify this before it is acted on."
    )
    review_rationale: str = Field(
        ..., description="Why a human is needed. Empty string if not needed."
    )


class _AgentOutput(BaseModel):
    summary: str = Field(..., description="Your read of the situation, 2-3 sentences.")
    findings: List[_Finding] = Field(..., description="3-5 findings from your lens.")
    recommendation: str = Field(..., description="Your single clearest recommendation.")
    open_questions: List[str] = Field(
        ...,
        description=(
            "What you could NOT determine from the source document. Put gaps here "
            "instead of guessing."
        ),
    )


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


def _system_instruction(role: RoleSpec, skill: str, context: str) -> str:
    return f"""You are the {role.title} ({role.short_title}) of GlobalTech Solutions,
sitting on a live executive committee. Your lens is: {role.lens}.

You contribute ONLY from your own functional perspective. You do not opine on
another executive's domain -- if a point belongs to the CFO, CSO, CMO, CHRO or
CTO and it is not you, leave it to them.

## GROUNDING RULES -- these override everything else
1. Every finding must be traceable to specific numbered lines of the source
   document. Cite them using the L<n> numbers exactly as they appear.
2. Never state a figure, name, date or metric that does not appear in the
   source document. Do not reconstruct one from memory or from plausibility.
3. If the document does not contain what you need, say so in `open_questions`.
   An acknowledged gap is a correct answer; an invented number is a failure.
4. Set `confidence` honestly. Below 0.75 means "a human must check this".
   Directly quoted facts warrant high confidence; extrapolations do not.
5. Set `requires_human_review` to true for anything that would commit money,
   headcount, customer communication or a production change.

## YOUR ROLE DEFINITION
{skill}

## COMPANY CONTEXT
{context}

Write in the brand voice: lead with the logic, close with the recommendation,
no filler."""


def _user_prompt(request: InvokeRequest, role: RoleSpec) -> str:
    blocks = [
        "## CEO OBJECTIVE",
        request.objective.strip(),
    ]

    if request.document:
        blocks += [
            "",
            f"## SOURCE DOCUMENT: {request.document.filename}",
            "Each line is prefixed with its line number. Cite these numbers.",
            "",
            request.document.numbered_content,
        ]
    else:
        blocks += [
            "",
            "## SOURCE DOCUMENT",
            "None supplied. You have no document to cite, so every finding must "
            "carry low confidence and be flagged for human review.",
        ]

    if request.prior_findings:
        blocks += ["", "## WHAT OTHER EXECUTIVES HAVE ALREADY REPORTED"]
        for prior in request.prior_findings:
            blocks.append(f"\n### {prior.role_title}")
            blocks.append(prior.summary)
            for finding in prior.findings:
                blocks.append(f"- {finding.headline} (confidence {finding.confidence:.2f})")
        blocks.append(
            "\nBuild on or challenge the above where it touches your domain. "
            "Do not simply restate it."
        )

    blocks += [
        "",
        f"## YOUR TASK",
        f"Respond as {role.short_title}. Produce 3-5 findings within your lens, "
        "each cited to source lines, each with an honest confidence score.",
    ]
    return "\n".join(blocks)


# --------------------------------------------------------------------------
# App lifecycle
# --------------------------------------------------------------------------


class _State:
    role: Optional[RoleSpec] = None
    skill: str = ""
    context: str = ""
    client: Optional[GeminiClient] = None
    init_error: str = ""


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        state.role = get_role(settings.exec_role)
        state.skill = load_skill(state.role.key, settings.agents_dir)
        state.context = load_context(settings.context_dir)
        api_key = resolve_secret(
            inline_value=settings.gemini_api_key,
            project_id=settings.gcp_project_id,
            secret_name=settings.gemini_api_key_secret,
            version=settings.gemini_api_key_secret_version,
            label="Gemini API key",
        )
        state.client = GeminiClient(
            api_key=api_key,
            model_name=settings.model_name,
            temperature=settings.model_temperature,
            max_output_tokens=settings.model_max_output_tokens,
        )
        logger.info(
            "Executive agent ready: role=%s model=%s", state.role.key, settings.model_name
        )
    except (KeyError, FileNotFoundError, SecretResolutionError, LLMError) as exc:
        # Start anyway so /healthz can report *why* the service is unhealthy,
        # which is far easier to debug on stage than a crash-looping revision.
        state.init_error = str(exc)
        logger.error("Executive agent failed to initialise: %s", exc)
    yield


app = FastAPI(
    title="C-Suite Executive Agent",
    description="One executive persona, exposed over HTTP for the Orchestrator.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    if state.init_error or state.role is None:
        raise HTTPException(status_code=503, detail=state.init_error or "not initialised")
    return HealthResponse(
        status="ok",
        role=state.role.key,
        role_title=state.role.title,
        model=settings.model_name,
    )


@app.post("/invoke", response_model=AgentFinding)
async def invoke(request: InvokeRequest) -> AgentFinding:
    """The Orchestrator's entry point into this executive."""
    if state.init_error or state.role is None or state.client is None:
        raise HTTPException(status_code=503, detail=state.init_error or "not initialised")

    role = state.role
    started = time.perf_counter()
    logger.info(
        "invoke run=%s handoff=%s role=%s doc=%s",
        request.run_id,
        request.handoff_id,
        role.key,
        request.document.filename if request.document else "none",
    )

    try:
        output = await state.client.generate_structured(
            system_instruction=_system_instruction(role, state.skill, state.context),
            prompt=_user_prompt(request, role),
            schema=_AgentOutput,
        )
    except LLMError as exc:
        logger.error("run=%s role=%s model call failed: %s", request.run_id, role.key, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    max_line = request.document.line_count if request.document else 0
    findings = [_to_finding(raw, max_line) for raw in output.findings]

    return AgentFinding(
        run_id=request.run_id,
        handoff_id=request.handoff_id,
        role=role.key,
        role_title=role.title,
        summary=output.summary.strip(),
        findings=findings,
        recommendation=output.recommendation.strip(),
        open_questions=[q.strip() for q in output.open_questions if q.strip()],
        model_used=settings.model_name,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _to_finding(raw: _Finding, max_line: int) -> Finding:
    """Convert model output to the wire model, validating citations.

    A citation pointing outside the document is itself a hallucination, so it
    is dropped and the finding's confidence is capped below the floor -- which
    routes it to a human.
    """
    citations: List[Citation] = []
    dropped = 0
    for c in raw.citations:
        start, end = min(c.line_start, c.line_end), max(c.line_start, c.line_end)
        if start < 1 or (max_line and end > max_line):
            dropped += 1
            continue
        citations.append(Citation(line_start=start, line_end=end, quote=c.quote.strip()))

    confidence = max(0.0, min(1.0, float(raw.confidence)))
    rationale = raw.review_rationale.strip()
    requires_review = bool(raw.requires_human_review)

    if dropped:
        confidence = min(confidence, settings.confidence_floor - 0.01)
        requires_review = True
        rationale = (
            f"{dropped} citation(s) referenced lines outside the source document "
            f"and were discarded. {rationale}"
        ).strip()

    if not citations:
        confidence = min(confidence, settings.confidence_floor - 0.01)
        requires_review = True
        rationale = (rationale + " No source citation was provided.").strip()

    return Finding(
        headline=raw.headline.strip(),
        detail=raw.detail.strip(),
        confidence=confidence,
        citations=citations,
        requires_human_review=requires_review,
        review_rationale=rationale,
    )
