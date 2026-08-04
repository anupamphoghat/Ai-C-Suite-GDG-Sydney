"""A deterministic stand-in for Gemini, used only by the end-to-end test.

Lets the whole system -- six services, real HTTP, the human-in-the-loop gate,
the decision log -- be exercised in CI without an API key or a model call.

It deliberately emits one of each escalation trigger so the human-in-the-loop
path is always covered:
  * a clean, well-cited, high-confidence finding  -> auto-accepted
  * a finding with no citations                   -> MISSING_CITATION
  * a low-confidence finding                      -> LOW_CONFIDENCE
  * a finding citing a line beyond the document   -> citation discarded,
                                                     confidence forced down
"""

from __future__ import annotations

from typing import Any, Type

from pydantic import BaseModel


class StubGeminiClient:
    """Drop-in replacement for ``csuite_common.llm.GeminiClient``."""

    def __init__(self, *, api_key: str = "", model_name: str = "stub-model", **_: Any) -> None:
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    async def generate_structured(
        self, *, system_instruction: str, prompt: str, schema: Type[BaseModel]
    ) -> BaseModel:
        name = schema.__name__
        if name == "_AgentOutput":
            return schema.model_validate(self._agent_output(system_instruction))
        if name == "_SynthesisOutput":
            return schema.model_validate(self._synthesis_output(prompt))
        if name == "_PlanOutput":
            return schema.model_validate(self._plan_output(prompt))
        raise AssertionError(f"StubGeminiClient does not know schema {name}")

    @staticmethod
    def _plan_output(prompt: str) -> dict:
        """Deterministically engage three of the five executives.

        Selecting a strict subset is the point: it proves the Orchestrator
        routes rather than fanning out to everyone, and that the pipeline and
        synthesis honour that choice.
        """
        engaged = {"cto": 1, "cfo": 2, "cmo": 3}
        offered = [
            key for key in ("cfo", "cso", "cmo", "chro", "cto")
            if f"- {key}:" in prompt
        ] or ["cfo", "cso", "cmo", "chro", "cto"]

        return {
            "interpretation": "Stub interpretation of the objective.",
            "strategy": "Stub strategy: engage only the implicated domains.",
            "routing": [
                {
                    "role": key,
                    "selected": key in engaged,
                    "reason": (
                        f"{key.upper()} is implicated by the source material."
                        if key in engaged
                        else f"{key.upper()}'s domain is not raised by this objective."
                    ),
                    "order": engaged.get(key, 0),
                }
                for key in offered
            ],
        }

    @staticmethod
    def _agent_output(system_instruction: str) -> dict:
        role = "EXEC"
        for candidate in ("CFO", "CSO", "CMO", "CHRO", "CTO"):
            if f"({candidate})" in system_instruction:
                role = candidate
                break

        return {
            "summary": f"{role} stub read of the situation.",
            "recommendation": f"{role} recommends the stub course of action.",
            "open_questions": [f"{role}: what is the budget envelope?"],
            "findings": [
                {
                    "headline": f"{role} finding A — fully grounded",
                    "detail": "Supported directly by the source document.",
                    "confidence": 0.93,
                    "citations": [{"line_start": 1, "line_end": 2, "quote": "stub quote"}],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{role} finding B — no citation",
                    "detail": "Asserted without pointing at the source.",
                    "confidence": 0.9,
                    "citations": [],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{role} finding C — low confidence",
                    "detail": "An inference that goes beyond what the document states.",
                    "confidence": 0.41,
                    "citations": [{"line_start": 2, "line_end": 3, "quote": "stub quote"}],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{role} finding D — citation out of range",
                    "detail": "Cites a line number the document does not contain.",
                    "confidence": 0.95,
                    "citations": [
                        {"line_start": 99_000, "line_end": 99_001, "quote": "does not exist"}
                    ],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{role} finding E — agent asked for a human",
                    "detail": "Well grounded, but it would commit budget.",
                    "confidence": 0.96,
                    "citations": [{"line_start": 1, "line_end": 1, "quote": "stub quote"}],
                    "requires_human_review": True,
                    "review_rationale": "Commits spend, so a human must approve it.",
                },
            ],
        }

    @staticmethod
    def _synthesis_output(prompt: str) -> dict:
        return {
            "executive_summary": "Stub synthesis of the human-approved findings.",
            "recommendation": "Proceed with the stub recommendation.",
            "key_risks": ["Stub risk"],
            "next_actions": ["Stub action (owner: CTO)"],
            "dissent": ["CFO and CSO disagreed on sequencing"],
            "unresolved": ["Budget envelope still unknown"],
        }
