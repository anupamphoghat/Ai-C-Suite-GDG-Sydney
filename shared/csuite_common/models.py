"""The HTTP contract between the Orchestrator and the Executive agents.

This module is the single source of truth for the wire format. Both sides of
every HTTP call validate against these models, which is what makes the
handoffs inspectable on the dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class RunStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    DISPATCHING = "dispatching"
    AWAITING_REVIEW = "awaiting_review"
    SYNTHESISING = "synthesising"
    AWAITING_SIGNOFF = "awaiting_signoff"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class HandoffStatus(str, Enum):
    QUEUED = "queued"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReviewStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class EscalationReason(str, Enum):
    LOW_CONFIDENCE = "low_confidence"
    MISSING_CITATION = "missing_citation"
    AGENT_FLAGGED = "agent_flagged"
    FINAL_SIGNOFF = "final_signoff"


class DecisionKind(str, Enum):
    RUN_STARTED = "run_started"
    PLAN_PRODUCED = "plan_produced"
    PLAN_APPROVED = "plan_approved"
    PLAN_AMENDED = "plan_amended"
    HANDOFF_DISPATCHED = "handoff_dispatched"
    HANDOFF_RETURNED = "handoff_returned"
    HANDOFF_FAILED = "handoff_failed"
    FINDING_ACCEPTED = "finding_accepted"
    FINDING_ESCALATED = "finding_escalated"
    HUMAN_APPROVED = "human_approved"
    HUMAN_EDITED = "human_edited"
    HUMAN_REJECTED = "human_rejected"
    REVIEW_TIMED_OUT = "review_timed_out"
    SYNTHESIS_PRODUCED = "synthesis_produced"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


# --------------------------------------------------------------------------
# Executive agent request / response
# --------------------------------------------------------------------------


class SourceDocument(BaseModel):
    """The incoming data the C-suite is asked to work on."""

    filename: str
    content_type: str = "text/markdown"
    # Numbered lines are what agents cite against; this is the anti-
    # hallucination anchor.
    numbered_content: str
    line_count: int = 0
    sha256: str = ""


class InvokeRequest(BaseModel):
    """Orchestrator -> Executive agent."""

    run_id: str
    handoff_id: str
    objective: str = Field(..., description="The CEO objective for this run.")
    document: Optional[SourceDocument] = None
    # Findings from agents that already reported, so later agents can build on
    # (or contest) earlier work. Populated in sequential mode.
    prior_findings: List["AgentFinding"] = Field(default_factory=list)
    requested_at: datetime = Field(default_factory=_now)
    trace_id: str = ""


class Citation(BaseModel):
    """A pointer back into the source document."""

    line_start: int = Field(..., ge=0)
    line_end: int = Field(..., ge=0)
    quote: str = Field(default="", description="Verbatim excerpt being relied on.")


class Finding(BaseModel):
    """One discrete claim or recommendation from an executive."""

    id: str = Field(default_factory=lambda: _new_id("fnd"))
    headline: str
    detail: str
    # 0.0 - 1.0. The agent self-reports; the orchestrator enforces the floor.
    confidence: float = Field(..., ge=0.0, le=1.0)
    citations: List[Citation] = Field(default_factory=list)
    # Agent can explicitly ask for a human even when confident.
    requires_human_review: bool = False
    review_rationale: str = ""

    @property
    def is_cited(self) -> bool:
        return len(self.citations) > 0


class AgentFinding(BaseModel):
    """Executive agent -> Orchestrator."""

    run_id: str
    handoff_id: str
    role: str
    role_title: str
    summary: str
    findings: List[Finding] = Field(default_factory=list)
    recommendation: str = ""
    # Things the agent could not determine from the source material. Naming
    # these explicitly is how the agent avoids filling gaps with invention.
    open_questions: List[str] = Field(default_factory=list)
    model_used: str = ""
    latency_ms: int = 0
    responded_at: datetime = Field(default_factory=_now)


class HealthResponse(BaseModel):
    status: str = "ok"
    role: str = ""
    role_title: str = ""
    model: str = ""
    version: str = "1.0.0"


# --------------------------------------------------------------------------
# Orchestration state
# --------------------------------------------------------------------------


class RoleRouting(BaseModel):
    """The Orchestrator's decision about one executive for one objective."""

    role: str
    role_title: str = ""
    short_title: str = ""
    selected: bool
    # Why this executive is needed, or why their domain is not in scope.
    # Required either way: an unexplained omission is indistinguishable from
    # an oversight.
    reason: str = ""
    sequence: int = 0
    # True when a human overrode the Orchestrator's choice for this role.
    amended_by_human: bool = False


class RunPlan(BaseModel):
    """Which executives to engage, in what order, and why.

    Produced before any agent is called. The Orchestrator reads the objective
    and the document, then delegates only to the executives whose domain is
    actually implicated.
    """

    interpretation: str = Field(
        default="", description="The Orchestrator's read of what is being asked."
    )
    strategy: str = Field(
        default="", description="Why this set of executives, in this order."
    )
    routing: List[RoleRouting] = Field(default_factory=list)
    model_used: str = ""
    approved_by_human: bool = False
    amended_by_human: bool = False
    reviewer_note: str = ""
    created_at: datetime = Field(default_factory=_now)

    @property
    def engaged(self) -> List[RoleRouting]:
        return sorted(
            [r for r in self.routing if r.selected], key=lambda r: r.sequence
        )

    @property
    def skipped(self) -> List[RoleRouting]:
        return [r for r in self.routing if not r.selected]

    @property
    def engaged_roles(self) -> List[str]:
        return [r.role for r in self.engaged]


class PlanDecision(BaseModel):
    """Human -> Orchestrator, resolving the plan gate."""

    action: str = Field(default="approve", description="approve | amend")
    # Only meaningful for "amend": the full set of roles to engage, in order.
    roles: List[str] = Field(default_factory=list)
    reviewer_note: str = ""


class Handoff(BaseModel):
    """One HTTP call from the Orchestrator to an Executive agent.

    Every field here is surfaced on the dashboard -- this record *is* the
    handoff visualisation.
    """

    id: str = Field(default_factory=lambda: _new_id("hop"))
    run_id: str
    role: str
    role_title: str = ""
    sequence: int = 0
    status: HandoffStatus = HandoffStatus.QUEUED
    method: str = "POST"
    url: str = ""
    auth_mode: str = ""
    http_status: Optional[int] = None
    attempt: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    dispatched_at: Optional[datetime] = None
    returned_at: Optional[datetime] = None
    latency_ms: Optional[int] = None
    error: str = ""
    finding_count: int = 0
    escalated_count: int = 0


class ReviewItem(BaseModel):
    """A finding held at the human-in-the-loop gate."""

    id: str = Field(default_factory=lambda: _new_id("rev"))
    run_id: str
    role: str
    role_title: str = ""
    finding_id: str = ""
    reason: EscalationReason
    reason_detail: str = ""
    status: ReviewStatus = ReviewStatus.PENDING
    original_headline: str = ""
    original_detail: str = ""
    confidence: float = 0.0
    citations: List[Citation] = Field(default_factory=list)
    edited_detail: str = ""
    reviewer_note: str = ""
    created_at: datetime = Field(default_factory=_now)
    resolved_at: Optional[datetime] = None


class DecisionLogEntry(BaseModel):
    """Append-only audit record. Nothing happens in a run without an entry."""

    id: str = Field(default_factory=lambda: _new_id("dec"))
    run_id: str
    sequence: int = 0
    kind: DecisionKind
    actor: str = Field(..., description="orchestrator | <role> | human")
    summary: str
    detail: Dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=_now)


class Synthesis(BaseModel):
    """The Orchestrator's consolidated answer to the CEO objective."""

    executive_summary: str = ""
    recommendation: str = ""
    key_risks: List[str] = Field(default_factory=list)
    next_actions: List[str] = Field(default_factory=list)
    dissent: List[str] = Field(
        default_factory=list,
        description="Where executives disagreed, preserved rather than averaged away.",
    )
    unresolved: List[str] = Field(default_factory=list)
    contributing_roles: List[str] = Field(default_factory=list)
    # Domains deliberately not assessed, so the CEO knows what this does not cover.
    not_assessed: List[str] = Field(default_factory=list)
    model_used: str = ""


class Run(BaseModel):
    """Full state of one C-suite engagement."""

    id: str = Field(default_factory=lambda: _new_id("run"))
    objective: str
    document: Optional[SourceDocument] = None
    status: RunStatus = RunStatus.PENDING
    plan: Optional[RunPlan] = None
    handoffs: List[Handoff] = Field(default_factory=list)
    findings: List[AgentFinding] = Field(default_factory=list)
    reviews: List[ReviewItem] = Field(default_factory=list)
    decision_log: List[DecisionLogEntry] = Field(default_factory=list)
    synthesis: Optional[Synthesis] = None
    error: str = ""
    created_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None

    @property
    def pending_reviews(self) -> List[ReviewItem]:
        return [r for r in self.reviews if r.status == ReviewStatus.PENDING]


class ReviewDecision(BaseModel):
    """Human -> Orchestrator."""

    action: str = Field(..., description="approve | edit | reject")
    edited_detail: str = ""
    reviewer_note: str = ""


class StartRunRequest(BaseModel):
    objective: str = Field(..., min_length=1)


InvokeRequest.model_rebuild()
