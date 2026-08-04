#!/usr/bin/env bash
#
# Run the whole system locally: five executive agents plus the orchestrator,
# each as its own process on its own port, talking real HTTP to each other.
# This is the same code path as Cloud Run, minus the identity tokens.
#
#   ./deploy/run_local.sh
#
# Requires GEMINI_API_KEY in .env (local development only -- in Cloud Run the
# key comes from Secret Manager).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env not found. Run: cp .env.example .env"
set -a; source .env; set +a

[[ -n "${GEMINI_API_KEY:-}" ]] \
  || die "GEMINI_API_KEY must be set in .env for local runs."

ACTIVE_ROLES_CSV="${ACTIVE_ROLES_CSV:-cfo,cso,cmo,chro,cto}"
LOCAL_AGENT_BASE_PORT="${LOCAL_AGENT_BASE_PORT:-8101}"
LOCAL_ORCHESTRATOR_PORT="${LOCAL_ORCHESTRATOR_PORT:-8080}"
IFS=',' read -ra ROLES <<< "${ACTIVE_ROLES_CSV// /}"

export PYTHONPATH="${REPO_ROOT}/shared:${PYTHONPATH:-}"
export SERVICE_AUTH_MODE="none"          # no metadata server locally
export AGENTS_DIR="${REPO_ROOT}/agents"
export CONTEXT_DIR="${REPO_ROOT}/context"

PIDS=()
cleanup() {
  log "Shutting down..."
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

mkdir -p .local_logs
AGENT_URLS_JSON="{"; SEP=""; PORT=$LOCAL_AGENT_BASE_PORT

for ROLE in "${ROLES[@]}"; do
  log "Starting $ROLE on port $PORT"
  ( cd "${REPO_ROOT}/services/executive" \
    && EXEC_ROLE="$ROLE" PORT="$PORT" \
       python -m uvicorn main:app --host 127.0.0.1 --port "$PORT" \
       > "${REPO_ROOT}/.local_logs/${ROLE}.log" 2>&1 ) &
  PIDS+=($!)
  AGENT_URLS_JSON+="${SEP}\"${ROLE}\":\"http://127.0.0.1:${PORT}\""
  SEP=","
  PORT=$((PORT + 1))
done
AGENT_URLS_JSON+="}"

export AGENT_URLS_JSON
export ACTIVE_ROLES_CSV

log "Waiting for agents to become healthy..."
READY=0
for _ in $(seq 1 40); do
  READY=1
  P=$LOCAL_AGENT_BASE_PORT
  for _ROLE in "${ROLES[@]}"; do
    curl -sf "http://127.0.0.1:${P}/healthz" >/dev/null || READY=0
    P=$((P + 1))
  done
  [[ $READY -eq 1 ]] && break
  sleep 1
done
[[ $READY -eq 1 ]] || die "Agents did not come up. Check .local_logs/*.log"
log "All ${#ROLES[@]} executive agents healthy."

printf '\n\033[1;32m✓ Dashboard: http://127.0.0.1:%s\033[0m\n\n' "$LOCAL_ORCHESTRATOR_PORT"

cd "${REPO_ROOT}/services/orchestrator"
python -m uvicorn main:app --host 127.0.0.1 --port "$LOCAL_ORCHESTRATOR_PORT"
