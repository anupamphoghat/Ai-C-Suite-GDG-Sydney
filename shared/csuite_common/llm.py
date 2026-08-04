"""Gemini client wrapper with schema-constrained JSON output.

Structured output is the first line of defence against hallucination: the
model must return an object matching a Pydantic schema, so it cannot ramble,
and it must populate a ``confidence`` and ``citations`` field for every claim
it makes.
"""

from __future__ import annotations

import json
import logging
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when the model call fails or returns unusable output."""


class GeminiClient:
    """Thin, parameterised wrapper over ``google-genai``."""

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        temperature: float = 0.2,
        max_output_tokens: int = 4096,
    ) -> None:
        from google import genai

        if not api_key:
            raise LLMError("GeminiClient requires a non-empty API key.")
        self._client = genai.Client(api_key=api_key)
        self._model_name = model_name
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    @property
    def model_name(self) -> str:
        return self._model_name

    async def generate_structured(
        self,
        *,
        system_instruction: str,
        prompt: str,
        schema: Type[T],
    ) -> T:
        """Call Gemini and parse the reply into ``schema``."""
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self._temperature,
            max_output_tokens=self._max_output_tokens,
            response_mime_type="application/json",
            response_schema=schema,
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gemini generate_content failed")
            raise LLMError(f"Model call failed: {type(exc).__name__}") from exc

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed

        raw = (getattr(response, "text", "") or "").strip()
        if not raw:
            raise LLMError("Model returned an empty response.")
        try:
            return schema.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.error("Model output did not match schema %s", schema.__name__)
            raise LLMError(
                f"Model output did not match the required {schema.__name__} schema."
            ) from exc
