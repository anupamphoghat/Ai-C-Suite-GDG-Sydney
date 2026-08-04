"""Cloud Run service-to-service authentication.

The five executive agents are deployed with ``--no-allow-unauthenticated``:
IAM permits only the orchestrator's service account to call them. It proves
that identity by attaching a Google-signed OIDC token whose audience is the
target service URL.

Note that IAM is the access control here, not ingress. The agents are
deployed with ``--ingress=all`` because a Cloud Run service is *not* treated
as internal traffic for another Cloud Run service on a ``run.app`` URL --
ingress=internal would make them unreachable from the orchestrator (Cloud Run
returns an HTML 404) regardless of IAM.

Set ``SERVICE_AUTH_MODE=none`` for local development, where there is no
metadata server to mint tokens from.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# audience -> (token, expiry epoch seconds)
_token_cache: Dict[str, Tuple[str, float]] = {}
_lock = threading.Lock()

# Identity tokens are valid for an hour. Refresh early so a call never travels
# with a token that expires in flight.
_EXPIRY_SKEW_SECONDS = 300.0


class ServiceAuthError(RuntimeError):
    """Raised when an identity token cannot be minted."""


def build_auth_headers(*, audience: str, mode: str) -> Dict[str, str]:
    """Return the Authorization header for a call to ``audience``."""
    if mode == "none":
        return {}
    if mode != "id_token":
        raise ServiceAuthError(
            f"Unsupported SERVICE_AUTH_MODE '{mode}'. Use 'id_token' or 'none'."
        )
    return {"Authorization": f"Bearer {_fetch_id_token(audience)}"}


def _fetch_id_token(audience: str) -> str:
    """Mint an OIDC identity token for ``audience``, cached until it expires."""
    now = time.time()
    with _lock:
        cached = _token_cache.get(audience)
        if cached and cached[1] - _EXPIRY_SKEW_SECONDS > now:
            return cached[0]

    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(request, audience)
    except Exception as exc:  # noqa: BLE001
        raise ServiceAuthError(
            f"Could not mint an identity token for '{audience}'. In Cloud Run this "
            "usually means the service account cannot reach the metadata server. "
            "Locally, set SERVICE_AUTH_MODE=none."
        ) from exc

    expiry = _expiry_of(token, default=now + 3300.0)
    with _lock:
        _token_cache[audience] = (token, expiry)
    logger.debug("Minted identity token for %s (expires in %.0fs)", audience, expiry - now)
    return token


def invalidate_token(audience: str) -> None:
    """Drop a cached token, e.g. after the target rejects it."""
    with _lock:
        _token_cache.pop(audience, None)


# --------------------------------------------------------------------------
# Claim inspection (non-secret fields only, for diagnostics)
# --------------------------------------------------------------------------


def _decode_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it.

    Verification is Cloud Run's job. This is only ever used to show *which*
    identity and audience a call carried, which is the fastest way to debug a
    rejected call.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


def _expiry_of(token: str, *, default: float) -> float:
    exp = _decode_claims(token).get("exp")
    try:
        return float(exp) if exp else default
    except (TypeError, ValueError):
        return default


def diagnose_call_failure(status: int, body: str, url: str) -> str:
    """Turn an unhelpful HTTP failure into the actual root cause.

    The distinction that matters most: Cloud Run returns a Google-branded HTML
    404 when *ingress* blocks the request, and 403 when *IAM* does. Those look
    similar in a log and have completely different fixes.
    """
    is_html = body.lstrip().lower().startswith(("<!doctype", "<html"))
    run_app = ".run.app" in url

    if status == 404 and is_html and run_app:
        return (
            "Ingress restriction, not IAM. Cloud Run's front end returned an HTML 404, "
            "so the request never reached the container. A Cloud Run service does not "
            "count as internal traffic for another Cloud Run service on a run.app URL. "
            "Redeploy this agent with --ingress=all -- it stays private via "
            "--no-allow-unauthenticated -- or check for an org policy on "
            "constraints/run.allowedIngress."
        )
    if status == 403:
        return (
            "IAM denied the call. The orchestrator's service account needs "
            "roles/run.invoker on this agent service."
        )
    if status == 401:
        return (
            "The identity token was rejected. Its audience must exactly match the "
            "target service URL."
        )
    if status == 503:
        return f"The container is running but failed to initialise: {body[:300]}"
    if status == 404:
        return f"The container responded but has no such route: {body[:200]}"
    return f"HTTP {status}: {body[:300]}"


def describe_token(authorization_header: str) -> dict:
    """Summarise a bearer token for diagnostics. Never returns the token."""
    if not authorization_header.startswith("Bearer "):
        return {"present": False}
    claims = _decode_claims(authorization_header[7:])
    if not claims:
        return {"present": True, "readable": False}
    expires_in = None
    if claims.get("exp"):
        try:
            expires_in = int(float(claims["exp"]) - time.time())
        except (TypeError, ValueError):
            pass
    return {
        "present": True,
        "readable": True,
        "email": claims.get("email", ""),
        "audience": claims.get("aud", ""),
        "expires_in_seconds": expires_in,
    }
