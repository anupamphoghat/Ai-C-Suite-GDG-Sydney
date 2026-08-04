"""Append-only decision log.

Every state change in a run is recorded here before it takes effect, so the
audit trail can never drift from what actually happened. Two backends:

  * ``memory``    -- in-process, zero external dependencies, ideal for a live
                     demo where a Firestore outage must not take the stage.
  * ``firestore`` -- persists runs for inspection after the demo.

The backend is selected by ``DECISION_LOG_BACKEND``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Protocol

from csuite_common.models import DecisionKind, DecisionLogEntry

logger = logging.getLogger("orchestrator.decision_log")


class DecisionLogBackend(Protocol):
    async def append(self, entry: DecisionLogEntry) -> None: ...
    async def list_for_run(self, run_id: str) -> List[DecisionLogEntry]: ...


class MemoryDecisionLog:
    """In-process decision log."""

    def __init__(self) -> None:
        self._entries: Dict[str, List[DecisionLogEntry]] = {}
        self._lock = asyncio.Lock()

    async def append(self, entry: DecisionLogEntry) -> None:
        async with self._lock:
            self._entries.setdefault(entry.run_id, []).append(entry)

    async def list_for_run(self, run_id: str) -> List[DecisionLogEntry]:
        async with self._lock:
            return list(self._entries.get(run_id, []))


class FirestoreDecisionLog:
    """Firestore-backed decision log, mirrored in memory for fast reads."""

    def __init__(self, *, project_id: str, collection: str, database: str) -> None:
        from google.cloud import firestore

        self._client = firestore.AsyncClient(project=project_id, database=database)
        self._collection = collection
        self._mirror = MemoryDecisionLog()

    async def append(self, entry: DecisionLogEntry) -> None:
        await self._mirror.append(entry)
        try:
            doc = (
                self._client.collection(self._collection)
                .document(entry.run_id)
                .collection("entries")
                .document(entry.id)
            )
            await doc.set(entry.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - never let logging break a run
            logger.exception("Failed to persist decision log entry %s", entry.id)

    async def list_for_run(self, run_id: str) -> List[DecisionLogEntry]:
        return await self._mirror.list_for_run(run_id)


def build_decision_log(
    *, backend: str, project_id: str, collection: str, database: str
) -> DecisionLogBackend:
    if backend == "firestore":
        if not project_id:
            logger.warning(
                "DECISION_LOG_BACKEND=firestore but GCP_PROJECT_ID is unset; "
                "falling back to the in-memory decision log."
            )
            return MemoryDecisionLog()
        try:
            log = FirestoreDecisionLog(
                project_id=project_id, collection=collection, database=database
            )
            logger.info("Decision log backend: firestore (%s)", collection)
            return log
        except Exception:  # noqa: BLE001
            logger.exception("Firestore unavailable; falling back to in-memory log")
            return MemoryDecisionLog()

    logger.info("Decision log backend: memory")
    return MemoryDecisionLog()


def make_entry(
    *,
    run_id: str,
    sequence: int,
    kind: DecisionKind,
    actor: str,
    summary: str,
    detail: Dict[str, Any] | None = None,
) -> DecisionLogEntry:
    return DecisionLogEntry(
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        actor=actor,
        summary=summary,
        detail=detail or {},
    )
