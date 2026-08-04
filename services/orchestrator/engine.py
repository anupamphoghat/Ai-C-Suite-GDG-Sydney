"""The Orchestrator engine.

Responsibilities:
  1. Accept a CEO objective plus an uploaded document.
  2. Hand off to each executive agent **over HTTP**, in sequence, passing each
     agent the findings of those before it.
  3. Score every returned finding against the trust policy and escalate the
     ones that fail to a human.
  4. Block synthesis until every escalation is resolved by a human.
  5. Synthesise the approved findings into one recommendation.
  6. Block completion until a human signs the synthesis off.

Everything it does is written to the decision log and broadcast as an event
so the dashboard can render the handoff live.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import httpx
from pydantic import BaseModel, Field

from csuite_common.auth import (
    ServiceAuthError,
    build_auth_headers,
    diagnose_call_failure,
    invalidate_token,
)
from csuite_common.config import OrchestratorSettings
from csuite_common.llm import GeminiClient, LLMError
from csuite_common.models import (
    AgentFinding,
    DecisionKind,
    EscalationReason,
    Handoff,
    HandoffStatus,
    InvokeRequest,
    ReviewDecision,
    ReviewItem,
    ReviewStatus,
    Run,
    RunStatus,
    SourceDocument,
    Synthesis,
)
from csuite_common.roles import get_role

from decision_log import DecisionLogBackend, make_entry

logger = logging.getLogger("orchestrator.engine")


class RunNotFoundError(KeyError):
    pass


class ReviewNotFoundError(KeyError):
    pass


# --------------------------------------------------------------------------
# Synthesis output schema
# --------------------------------------------------------------------------


class _SynthesisOutput(BaseModel):
    executive_summary: str = Field(..., description="3-4 sentences for the CEO.")
    recommendation: str = Field(..., description="The single decision you recommend.")
    key_risks: List[str] = Field(..., description="Material risks raised by the C-suite.")
    next_actions: List[str] = Field(..., description="Concrete next steps with an owner role.")
    dissent: List[str] = Field(
        ...,
        description=(
            "Where executives disagreed. Preserve the disagreement and name who "
            "held which position. Empty list if there was none."
        ),
    )
    unresolved: List[str] = Field(
        ..., description="Questions the C-suite could not answer from the source material."
    )


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class RunEngine:
    def __init__(
        self,
        *,
        settings: OrchestratorSettings,
        decision_log: DecisionLogBackend,
        llm: Optional[GeminiClient],
    ) -> None:
        self._settings = settings
        self._log = decision_log
        self._llm = llm
        self._runs: Dict[str, Run] = {}
        self._sequences: Dict[str, int] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._review_signals: Dict[str, asyncio.Event] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ---------------- public API ----------------

    def get_run(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def list_runs(self) -> List[Run]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    async def start_run(self, *, objective: str, document: Optional[SourceDocument]) -> Run:
        run = Run(objective=objective.strip(), document=document)
        self._runs[run.id] = run
        self._sequences[run.id] = 0
        self._subscribers[run.id] = set()
        self._review_signals[run.id] = asyncio.Event()

        await self._record(
            run,
            DecisionKind.RUN_STARTED,
            actor="orchestrator",
            summary=f"Run started for objective: {objective.strip()[:120]}",
            detail={
                "objective": run.objective,
                "document": document.filename if document else None,
                "document_lines": document.line_count if document else 0,
                "document_sha256": document.sha256 if document else "",
                "roles": self._settings.active_roles,
                "hitl_enabled": self._settings.hitl_enabled,
                "confidence_floor": self._settings.hitl_confidence_floor,
            },
        )

        self._tasks[run.id] = asyncio.create_task(self._execute(run.id))
        return run

    def subscribe(self, run_id: str) -> asyncio.Queue:
        if run_id not in self._runs:
            raise RunNotFoundError(run_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs:
            subs.discard(queue)

    async def resolve_review(
        self, *, run_id: str, review_id: str, decision: ReviewDecision
    ) -> ReviewItem:
        run = self.get_run(run_id)
        item = next((r for r in run.reviews if r.id == review_id), None)
        if item is None:
            raise ReviewNotFoundError(review_id)
        if item.status != ReviewStatus.PENDING:
            return item

        action = decision.action.strip().lower()
        item.reviewer_note = decision.reviewer_note.strip()
        item.resolved_at = datetime.now(timezone.utc)

        if action == "approve":
            item.status = ReviewStatus.APPROVED
            kind, verb = DecisionKind.HUMAN_APPROVED, "approved"
        elif action == "edit":
            item.status = ReviewStatus.EDITED
            item.edited_detail = decision.edited_detail.strip()
            kind, verb = DecisionKind.HUMAN_EDITED, "edited"
            self._apply_edit(run, item)
        elif action == "reject":
            item.status = ReviewStatus.REJECTED
            kind, verb = DecisionKind.HUMAN_REJECTED, "rejected"
        else:
            raise ValueError(f"Unknown review action '{decision.action}'.")

        await self._record(
            run,
            kind,
            actor="human",
            summary=(
                f"Human {verb} {item.role_title} finding: {item.original_headline[:90]}"
                if item.reason is not EscalationReason.FINAL_SIGNOFF
                else f"Human {verb} the final synthesis"
            ),
            detail={
                "review_id": item.id,
                "role": item.role,
                "reason": item.reason.value,
                "confidence": item.confidence,
                "reviewer_note": item.reviewer_note,
                "edited_detail": item.edited_detail,
            },
        )
        await self._emit(run, "review_resolved", item.model_dump(mode="json"))

        if not run.pending_reviews:
            self._review_signals[run_id].set()
        return item

    # ---------------- execution ----------------

    async def _execute(self, run_id: str) -> None:
        run = self._runs[run_id]
        try:
            await self._dispatch_all(run)

            if self._settings.hitl_enabled and run.pending_reviews:
                run.status = RunStatus.AWAITING_REVIEW
                await self._emit(run, "run_status", {"status": run.status.value})
                await self._await_reviews(run)

            run.status = RunStatus.SYNTHESISING
            await self._emit(run, "run_status", {"status": run.status.value})
            synthesis = await self._synthesise(run)
            run.synthesis = synthesis

            await self._record(
                run,
                DecisionKind.SYNTHESIS_PRODUCED,
                actor="orchestrator",
                summary="Synthesis produced from human-approved findings",
                detail={
                    "contributing_roles": synthesis.contributing_roles,
                    "recommendation": synthesis.recommendation,
                    "risk_count": len(synthesis.key_risks),
                    "dissent_count": len(synthesis.dissent),
                },
            )
            await self._emit(run, "synthesis", synthesis.model_dump(mode="json"))

            if self._settings.hitl_enabled and self._settings.hitl_final_signoff:
                signoff = await self._request_final_signoff(run)
                if signoff.status == ReviewStatus.REJECTED:
                    run.status = RunStatus.REJECTED
                    run.completed_at = datetime.now(timezone.utc)
                    await self._record(
                        run,
                        DecisionKind.RUN_COMPLETED,
                        actor="human",
                        summary="Run closed: CEO rejected the recommendation",
                        detail={"reviewer_note": signoff.reviewer_note},
                    )
                    await self._emit(run, "run_status", {"status": run.status.value})
                    return
                if signoff.status == ReviewStatus.EDITED and signoff.edited_detail:
                    synthesis.recommendation = signoff.edited_detail

            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            await self._record(
                run,
                DecisionKind.RUN_COMPLETED,
                actor="orchestrator",
                summary="Run completed and signed off",
                detail={
                    "handoffs": len(run.handoffs),
                    "findings": sum(len(f.findings) for f in run.findings),
                    "escalations": len(run.reviews),
                    "duration_ms": int(
                        (run.completed_at - run.created_at).total_seconds() * 1000
                    ),
                },
            )
            await self._emit(run, "run_status", {"status": run.status.value})

        except Exception as exc:  # noqa: BLE001
            logger.exception("Run %s failed", run_id)
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.completed_at = datetime.now(timezone.utc)
            await self._record(
                run,
                DecisionKind.RUN_FAILED,
                actor="orchestrator",
                summary="Run failed",
                detail={"error": run.error},
            )
            await self._emit(run, "run_status", {"status": run.status.value, "error": run.error})

    async def _dispatch_all(self, run: Run) -> None:
        """Call each executive agent over HTTP, in sequence."""
        run.status = RunStatus.DISPATCHING
        await self._emit(run, "run_status", {"status": run.status.value})

        agent_urls = self._settings.agent_urls
        roles = self._settings.active_roles
        missing = [r for r in roles if r not in agent_urls]
        if missing:
            raise RuntimeError(
                "No URL configured for role(s): "
                f"{', '.join(missing)}. Set AGENT_URLS_JSON (deploy.sh does this)."
            )

        timeout = httpx.Timeout(self._settings.agent_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, role_key in enumerate(roles, start=1):
                spec = get_role(role_key)
                handoff = Handoff(
                    run_id=run.id,
                    role=role_key,
                    role_title=spec.title,
                    sequence=index,
                    url=f"{agent_urls[role_key]}/invoke",
                    auth_mode=self._settings.service_auth_mode,
                )
                run.handoffs.append(handoff)
                await self._emit(run, "handoff", handoff.model_dump(mode="json"))

                # Each agent sees what the executives before it concluded.
                request = InvokeRequest(
                    run_id=run.id,
                    handoff_id=handoff.id,
                    objective=run.objective,
                    document=run.document,
                    prior_findings=list(run.findings),
                    trace_id=run.id,
                )
                await self._call_agent(client, run, handoff, request, agent_urls[role_key])

    async def _call_agent(
        self,
        client: httpx.AsyncClient,
        run: Run,
        handoff: Handoff,
        request: InvokeRequest,
        base_url: str,
    ) -> None:
        payload = request.model_dump(mode="json")
        handoff.request_bytes = len(str(payload))
        handoff.status = HandoffStatus.IN_FLIGHT
        handoff.dispatched_at = datetime.now(timezone.utc)

        await self._record(
            run,
            DecisionKind.HANDOFF_DISPATCHED,
            actor="orchestrator",
            summary=f"HTTP POST -> {handoff.role_title}",
            detail={
                "handoff_id": handoff.id,
                "url": handoff.url,
                "auth_mode": handoff.auth_mode,
                "prior_findings": len(request.prior_findings),
                "request_bytes": handoff.request_bytes,
            },
        )
        await self._emit(run, "handoff", handoff.model_dump(mode="json"))

        started = time.perf_counter()
        last_error = ""

        for attempt in range(1, self._settings.agent_max_retries + 2):
            handoff.attempt = attempt
            try:
                headers = build_auth_headers(
                    audience=base_url, mode=self._settings.service_auth_mode
                )
                headers["Content-Type"] = "application/json"
                response = await client.post(handoff.url, json=payload, headers=headers)
                handoff.http_status = response.status_code

                if response.status_code in (401, 403):
                    # Force a fresh token, in case a cached one went stale.
                    invalidate_token(base_url)
                    last_error = diagnose_call_failure(
                        response.status_code, response.text, handoff.url
                    )
                    continue
                if response.status_code == 404:
                    # Almost always an ingress restriction, which retrying
                    # will not fix.
                    last_error = diagnose_call_failure(404, response.text, handoff.url)
                    break
                if response.status_code >= 500:
                    last_error = diagnose_call_failure(
                        response.status_code, response.text, handoff.url
                    )
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
                    continue

                response.raise_for_status()
                handoff.response_bytes = len(response.content)
                finding = AgentFinding.model_validate(response.json())
                await self._accept_finding(run, handoff, finding, started)
                return

            except ServiceAuthError as exc:
                last_error = str(exc)
                break  # auth misconfiguration will not fix itself on retry
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                break

        handoff.status = HandoffStatus.FAILED
        handoff.returned_at = datetime.now(timezone.utc)
        handoff.latency_ms = int((time.perf_counter() - started) * 1000)
        handoff.error = last_error

        await self._record(
            run,
            DecisionKind.HANDOFF_FAILED,
            actor="orchestrator",
            summary=f"{handoff.role_title} did not respond; continuing without it",
            detail={
                "handoff_id": handoff.id,
                "url": handoff.url,
                "attempts": handoff.attempt,
                "http_status": handoff.http_status,
                "error": last_error,
            },
        )
        await self._emit(run, "handoff", handoff.model_dump(mode="json"))

    async def _accept_finding(
        self, run: Run, handoff: Handoff, finding: AgentFinding, started: float
    ) -> None:
        handoff.status = HandoffStatus.SUCCEEDED
        handoff.returned_at = datetime.now(timezone.utc)
        handoff.latency_ms = int((time.perf_counter() - started) * 1000)
        handoff.finding_count = len(finding.findings)
        run.findings.append(finding)

        await self._record(
            run,
            DecisionKind.HANDOFF_RETURNED,
            actor=finding.role,
            summary=f"{finding.role_title} returned {len(finding.findings)} finding(s)",
            detail={
                "handoff_id": handoff.id,
                "http_status": handoff.http_status,
                "latency_ms": handoff.latency_ms,
                "agent_latency_ms": finding.latency_ms,
                "model": finding.model_used,
                "recommendation": finding.recommendation,
                "open_questions": finding.open_questions,
            },
        )

        escalated = 0
        for item in finding.findings:
            reason, detail = self._triage(item)
            if reason is None:
                await self._record(
                    run,
                    DecisionKind.FINDING_ACCEPTED,
                    actor="orchestrator",
                    summary=f"Accepted: {item.headline[:90]}",
                    detail={
                        "role": finding.role,
                        "finding_id": item.id,
                        "confidence": item.confidence,
                        "citations": len(item.citations),
                    },
                )
                continue

            escalated += 1
            review = ReviewItem(
                run_id=run.id,
                role=finding.role,
                role_title=finding.role_title,
                finding_id=item.id,
                reason=reason,
                reason_detail=detail,
                original_headline=item.headline,
                original_detail=item.detail,
                confidence=item.confidence,
                citations=item.citations,
            )
            run.reviews.append(review)
            self._review_signals[run.id].clear()

            await self._record(
                run,
                DecisionKind.FINDING_ESCALATED,
                actor="orchestrator",
                summary=f"Escalated to human ({reason.value}): {item.headline[:80]}",
                detail={
                    "role": finding.role,
                    "finding_id": item.id,
                    "review_id": review.id,
                    "confidence": item.confidence,
                    "citations": len(item.citations),
                    "reason_detail": detail,
                },
            )
            await self._emit(run, "review", review.model_dump(mode="json"))

        handoff.escalated_count = escalated
        await self._emit(run, "handoff", handoff.model_dump(mode="json"))
        await self._emit(run, "finding", finding.model_dump(mode="json"))

    def _triage(self, item) -> tuple[Optional[EscalationReason], str]:
        """Decide whether a finding may pass without a human.

        Checked most-specific first, so the reviewer is shown the sharpest
        diagnosis available: an uncited claim is reported as uncited rather
        than as a generic agent request.
        """
        if not self._settings.hitl_enabled:
            return None, ""
        if self._settings.hitl_require_citations and not item.citations:
            return (
                EscalationReason.MISSING_CITATION,
                "No citation into the source document, so the claim cannot be verified. "
                + item.review_rationale,
            )
        if item.confidence < self._settings.hitl_confidence_floor:
            return (
                EscalationReason.LOW_CONFIDENCE,
                f"Confidence {item.confidence:.2f} is below the "
                f"{self._settings.hitl_confidence_floor:.2f} floor. "
                + item.review_rationale,
            )
        if item.requires_human_review:
            return (
                EscalationReason.AGENT_FLAGGED,
                item.review_rationale or "The agent asked for human verification.",
            )
        return None, ""

    def _apply_edit(self, run: Run, item: ReviewItem) -> None:
        """Write a human's edit back onto the underlying finding."""
        for agent_finding in run.findings:
            for finding in agent_finding.findings:
                if finding.id == item.finding_id:
                    finding.detail = item.edited_detail
                    finding.confidence = 1.0
                    return

    async def _await_reviews(self, run: Run) -> None:
        signal = self._review_signals[run.id]
        if not run.pending_reviews:
            return
        logger.info(
            "Run %s blocked on %d human review(s)", run.id, len(run.pending_reviews)
        )
        try:
            await asyncio.wait_for(signal.wait(), timeout=self._settings.hitl_timeout_seconds)
        except asyncio.TimeoutError:
            for item in run.pending_reviews:
                item.status = ReviewStatus.TIMED_OUT
                item.resolved_at = datetime.now(timezone.utc)
                await self._record(
                    run,
                    DecisionKind.REVIEW_TIMED_OUT,
                    actor="orchestrator",
                    summary=f"Review timed out and was dropped: {item.original_headline[:80]}",
                    detail={"review_id": item.id, "role": item.role},
                )
                await self._emit(run, "review_resolved", item.model_dump(mode="json"))

    async def _request_final_signoff(self, run: Run) -> ReviewItem:
        synthesis = run.synthesis
        item = ReviewItem(
            run_id=run.id,
            role="orchestrator",
            role_title="Orchestrator",
            reason=EscalationReason.FINAL_SIGNOFF,
            reason_detail="The CEO must sign off before this recommendation is acted on.",
            original_headline="Final recommendation",
            original_detail=synthesis.recommendation if synthesis else "",
            confidence=1.0,
        )
        run.reviews.append(item)
        self._review_signals[run.id].clear()
        run.status = RunStatus.AWAITING_SIGNOFF

        await self._record(
            run,
            DecisionKind.FINDING_ESCALATED,
            actor="orchestrator",
            summary="Final synthesis held for human sign-off",
            detail={"review_id": item.id, "reason": item.reason.value},
        )
        await self._emit(run, "review", item.model_dump(mode="json"))
        await self._emit(run, "run_status", {"status": run.status.value})

        await self._await_reviews(run)
        return item

    # ---------------- synthesis ----------------

    async def _synthesise(self, run: Run) -> Synthesis:
        approved = self._approved_material(run)
        contributing = sorted({role for role, _ in approved})

        if self._llm is None:
            return Synthesis(
                executive_summary=(
                    "Synthesis model unavailable; showing approved findings verbatim."
                ),
                recommendation="\n".join(
                    f.recommendation for f in run.findings if f.recommendation
                ),
                contributing_roles=contributing,
                model_used="none",
            )

        lines: List[str] = [
            "## CEO OBJECTIVE",
            run.objective,
            "",
            "## HUMAN-APPROVED EXECUTIVE FINDINGS",
        ]
        for agent_finding in run.findings:
            approved_for_role = [
                text for role, text in approved if role == agent_finding.role
            ]
            if not approved_for_role:
                continue
            lines.append(f"\n### {agent_finding.role_title}")
            lines.append(agent_finding.summary)
            lines.extend(f"- {t}" for t in approved_for_role)
            if agent_finding.recommendation:
                lines.append(f"Recommendation: {agent_finding.recommendation}")
            for question in agent_finding.open_questions:
                lines.append(f"Open question: {question}")

        rejected = [r for r in run.reviews if r.status == ReviewStatus.REJECTED]
        if rejected:
            lines.append("\n## FINDINGS A HUMAN REJECTED -- DO NOT USE THESE")
            lines.extend(f"- ({r.role_title}) {r.original_headline}" for r in rejected)

        failed = [h for h in run.handoffs if h.status == HandoffStatus.FAILED]
        if failed:
            lines.append("\n## EXECUTIVES WHO DID NOT REPORT")
            lines.extend(
                f"- {h.role_title}: no input available, treat their domain as unassessed"
                for h in failed
            )

        system = (
            "You are the Orchestrator of the GlobalTech Solutions executive committee. "
            "Consolidate the executives' human-approved findings into one recommendation "
            "for the CEO.\n\n"
            "RULES:\n"
            "1. Use ONLY the findings supplied below. Introduce no new facts, figures or "
            "names of your own.\n"
            "2. Findings a human rejected must not influence the outcome in any way.\n"
            "3. Do not average away disagreement -- surface it in `dissent`, naming who "
            "held which position.\n"
            "4. Anything the committee could not answer belongs in `unresolved`.\n"
            "5. Lead with the logic, close with the recommendation. No filler."
        )

        try:
            output = await self._llm.generate_structured(
                system_instruction=system, prompt="\n".join(lines), schema=_SynthesisOutput
            )
        except LLMError as exc:
            logger.error("Synthesis failed: %s", exc)
            return Synthesis(
                executive_summary=f"Synthesis could not be generated: {exc}",
                recommendation="",
                contributing_roles=contributing,
                model_used=self._llm.model_name,
            )

        return Synthesis(
            executive_summary=output.executive_summary.strip(),
            recommendation=output.recommendation.strip(),
            key_risks=[r.strip() for r in output.key_risks if r.strip()],
            next_actions=[a.strip() for a in output.next_actions if a.strip()],
            dissent=[d.strip() for d in output.dissent if d.strip()],
            unresolved=[u.strip() for u in output.unresolved if u.strip()],
            contributing_roles=contributing,
            model_used=self._llm.model_name,
        )

    def _approved_material(self, run: Run) -> List[tuple[str, str]]:
        """Findings that survived the human gate, with edits applied."""
        blocked = {
            r.finding_id
            for r in run.reviews
            if r.status in {ReviewStatus.REJECTED, ReviewStatus.TIMED_OUT, ReviewStatus.PENDING}
        }
        result: List[tuple[str, str]] = []
        for agent_finding in run.findings:
            for finding in agent_finding.findings:
                if finding.id in blocked:
                    continue
                cites = ", ".join(
                    f"L{c.line_start}-L{c.line_end}" for c in finding.citations
                )
                suffix = f" [source: {cites}]" if cites else ""
                result.append(
                    (agent_finding.role, f"{finding.headline} — {finding.detail}{suffix}")
                )
        return result

    # ---------------- plumbing ----------------

    async def _record(
        self,
        run: Run,
        kind: DecisionKind,
        *,
        actor: str,
        summary: str,
        detail: Dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self._sequences[run.id] = self._sequences.get(run.id, 0) + 1
            sequence = self._sequences[run.id]
        entry = make_entry(
            run_id=run.id,
            sequence=sequence,
            kind=kind,
            actor=actor,
            summary=summary,
            detail=detail,
        )
        run.decision_log.append(entry)
        await self._log.append(entry)
        await self._emit(run, "decision", entry.model_dump(mode="json"))

    async def _emit(self, run: Run, event: str, data: Dict[str, Any]) -> None:
        message = {"event": event, "data": data}
        for queue in list(self._subscribers.get(run.id, set())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Dropping event for a slow dashboard subscriber")
