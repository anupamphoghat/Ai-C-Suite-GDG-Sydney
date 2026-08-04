"""Unit tests for identity-token caching and failure diagnosis.

Both exist because of real failures during deployment:
  * an HTML 404 from Cloud Run was reported as an opaque body, when it in fact
    means an ingress restriction and nothing to do with IAM;
  * identity tokens were cached forever, so calls would have started failing
    about an hour into a demo day.

    python tests/auth_diagnostics_test.py
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from csuite_common import auth  # noqa: E402

PASS, FAIL = "\033[1;32m✓\033[0m", "\033[1;31m✗\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}  {detail}")
        failures.append(label)


def make_token(*, exp: float, email: str = "sa@example.iam.gserviceaccount.com",
               aud: str = "https://tech-cfo-abc-uc.a.run.app") -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'RS256'})}.{seg({'exp': exp, 'email': email, 'aud': aud})}.sig"


GOOGLE_404 = (
    '<!DOCTYPE html>\n<html lang=en>\n  <meta charset=utf-8>\n'
    '  <title>Error 404 (Not Found)!!1</title>\n'
)
RUN_URL = "https://tech-cfo-jsxl5ao7da-uc.a.run.app"


def main() -> int:
    print("\n-- Failure diagnosis --")

    d = auth.diagnose_call_failure(404, GOOGLE_404, RUN_URL)
    check("HTML 404 on run.app is identified as ingress", "Ingress restriction" in d, d[:90])
    check("HTML 404 explicitly rules out IAM", "not IAM" in d, d[:90])
    check("HTML 404 names the fix", "--ingress=all" in d, d[:120])

    d = auth.diagnose_call_failure(403, "Forbidden", RUN_URL)
    check("403 is identified as IAM", "IAM denied" in d and "run.invoker" in d, d[:90])
    check("403 is not confused with ingress", "Ingress restriction" not in d, d[:90])

    d = auth.diagnose_call_failure(401, "Unauthorized", RUN_URL)
    check("401 points at the token audience", "audience" in d, d[:90])

    d = auth.diagnose_call_failure(503, '{"detail":"GEMINI_API_KEY missing"}', RUN_URL)
    check("503 surfaces the container's own message", "GEMINI_API_KEY" in d, d[:90])

    d = auth.diagnose_call_failure(404, '{"detail":"Not Found"}', RUN_URL)
    check("JSON 404 is a routing problem, not ingress",
          "Ingress restriction" not in d and "no such route" in d, d[:90])

    d = auth.diagnose_call_failure(404, GOOGLE_404, "http://127.0.0.1:8101")
    check("HTML 404 off run.app is not called an ingress problem",
          "Ingress restriction" not in d, d[:90])

    print("\n-- Token caching --")
    now = time.time()
    auth._token_cache.clear()

    fresh = make_token(exp=now + 3600)
    auth._token_cache["aud-a"] = (fresh, now + 3600)
    check("a valid cached token is reused", auth._fetch_id_token("aud-a") == fresh)

    # Expiring inside the safety margin must NOT be reused. There is no
    # metadata server here, so a refresh attempt raises -- which is the proof.
    auth._token_cache["aud-b"] = (make_token(exp=now + 60), now + 60)
    try:
        auth._fetch_id_token("aud-b")
        check("a near-expiry token is refreshed, not reused", False, "no refresh attempted")
    except auth.ServiceAuthError:
        check("a near-expiry token is refreshed, not reused", True)

    auth._token_cache["aud-c"] = (make_token(exp=now - 10), now - 10)
    try:
        auth._fetch_id_token("aud-c")
        check("an expired token is refreshed", False, "no refresh attempted")
    except auth.ServiceAuthError:
        check("an expired token is refreshed", True)

    auth._token_cache["aud-d"] = (fresh, now + 3600)
    auth.invalidate_token("aud-d")
    check("invalidate_token clears the entry", "aud-d" not in auth._token_cache)

    check("expiry is read from the token's exp claim",
          abs(auth._expiry_of(make_token(exp=now + 1234), default=0.0) - (now + 1234)) < 1)
    check("unparseable token falls back to the default expiry",
          auth._expiry_of("not-a-jwt", default=42.0) == 42.0)

    print("\n-- Token description (diagnostics must not leak the token) --")
    token = make_token(exp=now + 3600)
    desc = auth.describe_token(f"Bearer {token}")
    check("reports the caller identity", desc["email"] == "sa@example.iam.gserviceaccount.com")
    check("reports the audience", desc["audience"] == "https://tech-cfo-abc-uc.a.run.app")
    check("reports remaining lifetime", 3500 < desc["expires_in_seconds"] <= 3600)
    check("never returns the token itself", token not in json.dumps(desc))
    check("handles a missing header", auth.describe_token("") == {"present": False})

    print("\n-- Auth mode --")
    check("mode 'none' attaches no header",
          auth.build_auth_headers(audience=RUN_URL, mode="none") == {})
    try:
        auth.build_auth_headers(audience=RUN_URL, mode="bogus")
        check("an unknown auth mode is rejected", False)
    except auth.ServiceAuthError:
        check("an unknown auth mode is rejected", True)

    print()
    if failures:
        print(f"\033[1;31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\033[1;32mAll checks passed.\033[0m\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
