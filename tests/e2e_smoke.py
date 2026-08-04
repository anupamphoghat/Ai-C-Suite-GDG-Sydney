"""End-to-end smoke test.

Starts all six services as real processes talking real HTTP, then drives one
complete run: upload -> five handoffs -> escalations -> human decisions ->
synthesis -> sign-off. Asserts the behaviour the demo depends on.

    python tests/e2e_smoke.py

No API key and no Google Cloud access required; the model is stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLES = ["cfo", "cso", "cmo", "chro", "cto"]
BASE_PORT = 8301
ORCH_PORT = 8300

PASS, FAIL = "\033[1;32m✓\033[0m", "\033[1;31m✗\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}  {detail}")
        failures.append(label)


PROXY_VARS = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY", "GRPC_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy", "grpc_proxy",
]


def wait_healthy(url: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    with httpx.Client(trust_env=False, timeout=2.0) as probe:
        while time.time() < deadline:
            try:
                if probe.get(url).status_code == 200:
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
    return False


def main() -> int:
    env = os.environ.copy()
    # Loopback traffic must not be routed through any ambient proxy.
    for var in PROXY_VARS:
        env.pop(var, None)
    env["GEMINI_API_KEY"] = "stub-key-not-a-real-secret"
    env["SERVICE_AUTH_MODE"] = "none"
    env["AGENTS_DIR"] = str(REPO_ROOT / "agents")
    env["CONTEXT_DIR"] = str(REPO_ROOT / "context")
    env["DECISION_LOG_BACKEND"] = "memory"
    env["HITL_ENABLED"] = "true"
    env["HITL_CONFIDENCE_FLOOR"] = "0.75"
    env["HITL_REQUIRE_CITATIONS"] = "true"
    env["HITL_FINAL_SIGNOFF"] = "true"
    env["ACTIVE_ROLES_CSV"] = ",".join(ROLES)
    env["MODEL_NAME"] = "stub-model"

    log_dir = REPO_ROOT / ".local_logs"
    log_dir.mkdir(exist_ok=True)

    procs: list[subprocess.Popen] = []
    handles = []
    agent_urls = {}

    def spawn(name: str, service: str, port: int, extra_env: dict) -> None:
        handle = (log_dir / f"test_{name}.log").open("wb")
        handles.append(handle)
        procs.append(
            subprocess.Popen(
                [sys.executable, str(REPO_ROOT / "tests" / "serve_stubbed.py"),
                 service, "--port", str(port)],
                env=dict(env, **extra_env), cwd=REPO_ROOT,
                stdout=handle, stderr=subprocess.STDOUT,
            )
        )

    print("\nStarting five executive agent services…")
    for i, role in enumerate(ROLES):
        port = BASE_PORT + i
        spawn(role, "executive", port, {"EXEC_ROLE": role})
        agent_urls[role] = f"http://127.0.0.1:{port}"

    print("Starting the orchestrator…")
    spawn("orchestrator", "orchestrator", ORCH_PORT,
          {"AGENT_URLS_JSON": json.dumps(agent_urls)})
    orch = f"http://127.0.0.1:{ORCH_PORT}"

    try:
        print("\n── Service health ──")
        for role, url in agent_urls.items():
            check(f"{role.upper()} agent healthy", wait_healthy(f"{url}/healthz", 30))
        check("orchestrator healthy", wait_healthy(f"{orch}/healthz", 30))
        if failures:
            print(f"\nService logs are in {log_dir}/")
            for path in sorted(log_dir.glob("test_*.log")):
                text = path.read_text(errors="replace").strip()
                if text:
                    print(f"\n--- {path.name} ---\n{text[:1500]}")
            return 1

        client = httpx.Client(base_url=orch, timeout=60.0, trust_env=False)

        print("\n── Configuration ──")
        cfg = client.get("/api/config").json()
        check("five roles configured", len(cfg["roles"]) == 5, str(len(cfg["roles"])))
        check("all agent endpoints resolved", all(r["configured"] for r in cfg["roles"]))
        check("no secret leaked via /api/config",
              "stub-key-not-a-real-secret" not in json.dumps(cfg))

        health = client.get("/api/agents/health").json()
        check("orchestrator can reach every agent over HTTP", health["all_ok"],
              json.dumps(health)[:300])

        print("\n── Upload validation ──")
        bad = client.post("/api/runs", data={"objective": "x"},
                          files={"file": ("evil.exe", b"binary", "application/octet-stream")})
        check("rejects a disallowed file type", bad.status_code == 415, str(bad.status_code))
        empty = client.post("/api/runs", data={"objective": ""})
        check("rejects an empty objective", empty.status_code == 422, str(empty.status_code))

        print("\n── Start a run ──")
        doc = REPO_ROOT / "demo_files" / "infrastructure_incident_report.md"
        with doc.open("rb") as handle:
            res = client.post(
                "/api/runs",
                data={"objective": "Three enterprise renewals are at risk after this "
                                   "incident. Tell me what we do."},
                files={"file": (doc.name, handle, "text/markdown")},
            )
        check("run accepted", res.status_code == 200, res.text[:250])
        run = res.json()
        run_id = run["id"]
        check("document line-numbered for citation",
              run["document"]["numbered_content"].startswith("L "),
              run["document"]["numbered_content"][:24])
        check("document hashed", len(run["document"]["sha256"]) == 64)

        print("\n── Handoffs over HTTP ──")
        run = poll(client, run_id, lambda r: r["status"] == "awaiting_review", 90)
        check("run paused for human review", run["status"] == "awaiting_review", run["status"])
        check("five handoffs recorded", len(run["handoffs"]) == 5, str(len(run["handoffs"])))
        check("every handoff succeeded",
              all(h["status"] == "succeeded" for h in run["handoffs"]),
              json.dumps([(h["role"], h["status"], h["error"]) for h in run["handoffs"]]))
        check("handoffs carry HTTP 200",
              all(h["http_status"] == 200 for h in run["handoffs"]))
        check("handoffs are sequenced",
              [h["sequence"] for h in run["handoffs"]] == [1, 2, 3, 4, 5])
        check("latency captured for the dashboard",
              all(isinstance(h["latency_ms"], int) for h in run["handoffs"]))

        print("\n── Anti-hallucination controls ──")
        reasons = {r["reason"] for r in run["reviews"]}
        check("uncited findings escalated", "missing_citation" in reasons, str(reasons))
        check("low-confidence findings escalated", "low_confidence" in reasons, str(reasons))
        check("agent-flagged findings escalated", "agent_flagged" in reasons, str(reasons))
        check("well-grounded findings auto-accepted",
              any(d["kind"] == "finding_accepted" for d in run["decision_log"]))
        check("no synthesis before the human acts", run["synthesis"] is None)

        out_of_range = [
            f for af in run["findings"] for f in af["findings"]
            if "out of range" in f["headline"]
        ]
        check("bad citation discarded, not passed through",
              all(len(f["citations"]) == 0 for f in out_of_range))

        source = client.get(f"/api/runs/{run_id}/source").json()
        check("source retrievable for citation checking", source["line_count"] > 0)

        print("\n── Human in the loop ──")
        pending = [r for r in run["reviews"] if r["status"] == "pending"]
        check("escalations are waiting", len(pending) > 0, str(len(pending)))

        rejected_headline = pending[0]["original_headline"]
        client.post(f"/api/runs/{run_id}/reviews/{pending[0]['id']}",
                    json={"action": "reject", "reviewer_note": "Not supported."})
        edited_text = "HUMAN-CORRECTED TEXT MARKER"
        if len(pending) > 1:
            client.post(f"/api/runs/{run_id}/reviews/{pending[1]['id']}",
                        json={"action": "edit", "edited_detail": edited_text,
                              "reviewer_note": "Reworded."})
        for item in pending[2:]:
            client.post(f"/api/runs/{run_id}/reviews/{item['id']}",
                        json={"action": "approve"})

        run = poll(client, run_id, lambda r: r["status"] == "awaiting_signoff", 60)
        check("run resumed and reached sign-off", run["status"] == "awaiting_signoff",
              run["status"])
        check("synthesis produced", run["synthesis"] is not None)

        edited = [f for af in run["findings"] for f in af["findings"]
                  if f["detail"] == edited_text]
        check("human edit written back onto the finding", len(edited) == 1)
        check("human decisions recorded in the decision log",
              {"human_rejected", "human_edited", "human_approved"}
              <= {d["kind"] for d in run["decision_log"]})

        print("\n── Sign-off ──")
        signoff = [r for r in run["reviews"]
                   if r["reason"] == "final_signoff" and r["status"] == "pending"]
        check("final sign-off gate raised", len(signoff) == 1)
        client.post(f"/api/runs/{run_id}/reviews/{signoff[0]['id']}",
                    json={"action": "approve", "reviewer_note": "Approved."})

        run = poll(client, run_id, lambda r: r["status"] == "completed", 30)
        check("run completed", run["status"] == "completed", run["status"])

        print("\n── Decision log ──")
        log = client.get(f"/api/runs/{run_id}/decisions").json()
        check("decision log is populated", len(log) > 20, str(len(log)))
        check("sequence numbers are contiguous",
              [d["sequence"] for d in log] == list(range(1, len(log) + 1)))
        kinds = {d["kind"] for d in log}
        check("covers the full lifecycle",
              {"run_started", "handoff_dispatched", "handoff_returned",
               "finding_escalated", "synthesis_produced", "run_completed"} <= kinds,
              str(sorted(kinds)))
        check("rejected finding excluded from the synthesis",
              rejected_headline not in json.dumps(run["synthesis"]))
        check("no secret anywhere in the run record",
              "stub-key-not-a-real-secret" not in json.dumps(run))

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for handle in handles:
            handle.close()

    print()
    if failures:
        print(f"\033[1;31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\033[1;32mAll checks passed.\033[0m\n")
    return 0


def poll(client: httpx.Client, run_id: str, predicate, timeout: float) -> dict:
    deadline = time.time() + timeout
    run = {}
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if predicate(run):
            return run
        time.sleep(0.4)
    return run


if __name__ == "__main__":
    sys.exit(main())
