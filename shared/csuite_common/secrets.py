"""Runtime secret resolution via Google Secret Manager.

No secret is ever committed to this repository or baked into a container
image. At runtime a service asks Secret Manager for the value, caches it in
memory for the life of the process, and never logs it.

Resolution order:
  1. ``GEMINI_API_KEY`` env var -- local development only.
  2. Google Secret Manager, using the secret *name* from config.

Cloud Run's ``--set-secrets`` flag mounts the secret as an env var, so in the
deployed demo path 1 is satisfied by Secret Manager itself. Path 2 covers
running against Secret Manager directly (e.g. from a workstation with ADC).
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


class SecretResolutionError(RuntimeError):
    """Raised when a required secret cannot be resolved."""


@lru_cache(maxsize=32)
def _fetch_from_secret_manager(project_id: str, secret_name: str, version: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    resource = f"projects/{project_id}/secrets/{secret_name}/versions/{version}"
    logger.info("Resolving secret %s from Secret Manager", secret_name)
    response = client.access_secret_version(request={"name": resource})
    return response.payload.data.decode("utf-8").strip()


def resolve_secret(
    *,
    inline_value: str,
    project_id: str,
    secret_name: str,
    version: str = "latest",
    label: str = "secret",
) -> str:
    """Return the secret value, preferring an already-injected env value."""
    if inline_value:
        logger.debug("Using injected value for %s", label)
        return inline_value.strip()

    if not project_id:
        raise SecretResolutionError(
            f"Cannot resolve {label}: neither an injected value nor GCP_PROJECT_ID "
            "is available. Set GCP_PROJECT_ID, or deploy with "
            f"--set-secrets={secret_name}."
        )
    if not secret_name:
        raise SecretResolutionError(f"Cannot resolve {label}: no secret name configured.")

    try:
        value = _fetch_from_secret_manager(project_id, secret_name, version)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean error
        raise SecretResolutionError(
            f"Failed to read secret '{secret_name}' (version {version}) from "
            f"project '{project_id}'. Confirm the secret exists and the service "
            "account holds roles/secretmanager.secretAccessor."
        ) from exc

    if not value:
        raise SecretResolutionError(f"Secret '{secret_name}' resolved to an empty value.")
    return value
