#!/usr/bin/env bash
#
# Remove everything ./deploy/deploy.sh created for the current SERVICE_PREFIX.
#
#   ./deploy/teardown.sh            # prompts before deleting
#   ./deploy/teardown.sh --yes      # no prompt
#
# Only touches resources labelled managed-by=ai-csuite-demo. The Gemini API
# key secret is never deleted -- removing it is a deliberate, separate act.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env not found."
set -a; source .env; set +a

GCP_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$GCP_PROJECT_ID" ]] || die "Set GCP_PROJECT_ID in .env."
GCP_REGION="${GCP_REGION:?Set GCP_REGION in .env}"
SERVICE_PREFIX="$(printf '%s' "${SERVICE_PREFIX:?Set SERVICE_PREFIX in .env}" \
                  | tr '[:upper:]' '[:lower:]')"
ACTIVE_ROLES_CSV="${ACTIVE_ROLES_CSV:-cfo,cso,cmo,chro,cto}"
IFS=',' read -ra ROLES <<< "${ACTIVE_ROLES_CSV// /}"

# A newline-delimited list rather than an array: bash 3.2 (the macOS default)
# errors on ${arr[@]} for an empty array when `set -u` is on.
TARGETS=""
TARGET_COUNT=0
for ROLE in "${ROLES[@]}" orchestrator; do
  SVC="${SERVICE_PREFIX}-${ROLE}"
  LABEL="$(gcloud run services describe "$SVC" --project "$GCP_PROJECT_ID" \
            --region "$GCP_REGION" --format='value(metadata.labels.managed-by)' 2>/dev/null || true)"
  if [[ "$LABEL" == "ai-csuite-demo" ]]; then
    TARGETS="${TARGETS}${SVC}"$'\n'
    TARGET_COUNT=$((TARGET_COUNT + 1))
  fi
done

if [[ $TARGET_COUNT -eq 0 ]]; then
  log "Nothing to remove for prefix '${SERVICE_PREFIX}' in ${GCP_REGION}."
  exit 0
fi

printf '\nThe following Cloud Run services will be DELETED from %s (%s):\n\n' \
  "$GCP_PROJECT_ID" "$GCP_REGION"
printf '%s' "$TARGETS" | sed 's/^/  /'
printf '\nThe Gemini API key secret will NOT be touched.\n\n'

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Type the prefix '${SERVICE_PREFIX}' to confirm: " CONFIRM
  [[ "$CONFIRM" == "$SERVICE_PREFIX" ]] || die "Cancelled."
fi

while IFS= read -r SVC; do
  [[ -n "$SVC" ]] || continue
  log "Deleting ${SVC}..."
  gcloud run services delete "$SVC" --project "$GCP_PROJECT_ID" \
    --region "$GCP_REGION" --quiet
done <<< "$TARGETS"

printf '\n\033[1;32m✓ Removed %d service(s).\033[0m\n' "$TARGET_COUNT"
printf '  Left in place: Artifact Registry images, the runtime service account,\n'
printf '  and the Secret Manager secret. Remove those by hand if you want to.\n\n'
