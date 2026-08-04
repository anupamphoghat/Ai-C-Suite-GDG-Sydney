"""Environment-driven configuration.

Nothing in this system is hard-coded: every project id, region, model name,
timeout, threshold and service URL is read from the environment. Secrets are
never read from the environment in a deployed context -- they are resolved at
runtime from Google Secret Manager (see ``secrets.py``).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Dict, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Settings common to every service in the system."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Platform ---
    gcp_project_id: str = Field(default="", description="Google Cloud project id.")
    gcp_region: str = Field(default="us-central1")
    log_level: str = Field(default="INFO")
    port: int = Field(default=8080, description="Cloud Run injects this.")

    # --- Model ---
    # Parameterised so the demo can be re-pointed at a different Gemini model
    # without a code change.
    model_name: str = Field(default="gemini-3.6-flash")
    model_temperature: float = Field(default=0.2)
    model_max_output_tokens: int = Field(default=4096)

    # --- Secrets ---
    gemini_api_key_secret: str = Field(
        default="GEMINI_API_KEY",
        description="Name of the Secret Manager secret holding the Gemini API key.",
    )
    gemini_api_key_secret_version: str = Field(default="latest")
    # Local-only escape hatch. In Cloud Run this stays empty and the key comes
    # from Secret Manager.
    gemini_api_key: str = Field(default="")

    # --- Service-to-service auth ---
    # "id_token" -> mint a Google-signed OIDC token per call (Cloud Run private
    # services). "none" -> plain HTTP, for local development only.
    service_auth_mode: Literal["id_token", "none"] = Field(default="id_token")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class ExecutiveSettings(BaseServiceSettings):
    """Settings for an executive agent service.

    One container image is deployed five times; ``exec_role`` selects which
    C-suite persona the instance adopts and which SKILL.md it loads.
    """

    exec_role: str = Field(default="cfo", description="cfo | cso | cmo | chro | cto")
    agents_dir: str = Field(default="agents")
    context_dir: str = Field(default="context")

    # A finding below this confidence is escalated to a human by the
    # orchestrator rather than being accepted silently.
    confidence_floor: float = Field(default=0.75)

    @field_validator("exec_role")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class OrchestratorSettings(BaseServiceSettings):
    """Settings for the orchestrator service."""

    # JSON map of role -> base URL, e.g.
    #   {"cfo": "https://csuite-cfo-xxxx.a.run.app", ...}
    # Set by deploy.sh once the executive services have been deployed.
    agent_urls_json: str = Field(default="{}")

    agent_timeout_seconds: float = Field(default=120.0)
    agent_max_retries: int = Field(default=2)

    # Roles engaged for a run, in dashboard display order.
    active_roles_csv: str = Field(default="cfo,cso,cmo,chro,cto")

    # --- Human in the loop ---
    hitl_enabled: bool = Field(default=True)
    # Findings at or below this confidence are held for human review.
    hitl_confidence_floor: float = Field(default=0.75)
    # Findings with no citation into the source document are always held.
    hitl_require_citations: bool = Field(default=True)
    # Seconds the orchestrator waits for a human before timing the gate out.
    hitl_timeout_seconds: float = Field(default=900.0)
    # Require an explicit human sign-off on the final synthesis.
    hitl_final_signoff: bool = Field(default=True)

    # --- Decision log ---
    # "memory" keeps everything in-process (zero external dependencies, ideal
    # for a live demo). "firestore" persists runs for post-demo inspection.
    decision_log_backend: Literal["memory", "firestore"] = Field(default="memory")
    firestore_collection: str = Field(default="csuite_decision_log")
    firestore_database: str = Field(default="(default)")

    # --- Uploads ---
    max_upload_bytes: int = Field(default=1_048_576)
    allowed_upload_extensions_csv: str = Field(default=".md,.markdown,.txt,.csv")

    @property
    def agent_urls(self) -> Dict[str, str]:
        raw = (self.agent_urls_json or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - config error
            raise ValueError(
                "AGENT_URLS_JSON must be a JSON object mapping role -> base URL"
            ) from exc
        return {str(k).lower(): str(v).rstrip("/") for k, v in parsed.items()}

    @property
    def active_roles(self) -> list[str]:
        return [r.strip().lower() for r in self.active_roles_csv.split(",") if r.strip()]

    @property
    def allowed_upload_extensions(self) -> set[str]:
        return {
            e.strip().lower()
            for e in self.allowed_upload_extensions_csv.split(",")
            if e.strip()
        }


@lru_cache(maxsize=1)
def executive_settings() -> ExecutiveSettings:
    return ExecutiveSettings()


@lru_cache(maxsize=1)
def orchestrator_settings() -> OrchestratorSettings:
    return OrchestratorSettings()
