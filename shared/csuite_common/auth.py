"""Cloud Run service-to-service authentication.

The five executive agents are deployed with ``--no-allow-unauthenticated``:
they are not reachable from the public internet. Only the orchestrator's
service account can call them, and it proves that by attaching a
Google-signed OIDC identity token whose audience is the target service URL.

Set ``SERVICE_AUTH_MODE=none`` for local development, where there is no
metadata server to mint tokens from.
"""

from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

_token_cache: Dict[str, str] = {}


class ServiceAuthError(RuntimeError):
    """Raised when an identity token cannot be minted."""


def build_auth_headers(*, audience: str, mode: str) -> Dict[str, str]:
    """Return the Authorization header for a call to ``audience``."""
    if mode == "none":
        return {}
    if mode != "id_token":
        raise ServiceAuthError(f"Unsupported SERVICE_AUTH_MODE '{mode}'.")

    token = _fetch_id_token(audience)
    return {"Authorization": f"Bearer {token}"}


def _fetch_id_token(audience: str) -> str:
    """Mint (and cache) an OIDC identity token for ``audience``."""
    cached = _token_cache.get(audience)
    if cached:
        return cached

    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(request, audience)
    except Exception as exc:  # noqa: BLE001
        raise ServiceAuthError(
            f"Could not mint an identity token for '{audience}'. In Cloud Run this "
            "means the service account lacks roles/run.invoker on the target. "
            "Locally, set SERVICE_AUTH_MODE=none."
        ) from exc

    _token_cache[audience] = token
    return token


def invalidate_token(audience: str) -> None:
    """Drop a cached token, e.g. after a 401 from the target service."""
    _token_cache.pop(audience, None)
