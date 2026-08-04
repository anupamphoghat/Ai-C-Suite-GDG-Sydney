#!/usr/bin/env bash
#
# Deploy the AI C-Suite multi-agent system to Google Cloud Run.
#
#   ./deploy/deploy.sh
#
# Creates six Cloud Run services:
#   <PREFIX>-cfo, -cso, -cmo, -chro, -cto   PRIVATE, one per executive agent
#   <PREFIX>-orchestrator                   PUBLIC, serves the dashboard
#
# Every value comes from .env -- nothing is hard-coded here. The Gemini API key
# is never passed on a command line or baked into an image; each service reads
# it from Secret Manager at runtime.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log()  { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 0. Configuration
# --------------------------------------------------------------------------
[[ -f .env ]] || die ".env not found. Run: cp .env.example .env  then fill it in."
set -a; source .env; set +a

command -v gcloud >/dev/null || die "gcloud CLI not found."

GCP_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$GCP_PROJECT_ID" && "$GCP_PROJECT_ID" != "your-project-id" ]] \
  || die "Set GCP_PROJECT_ID in .env, or run: gcloud config set project <id>"

GCP_REGION="${GCP_REGION:?Set GCP_REGION in .env}"
SERVICE_PREFIX="${SERVICE_PREFIX:?Set SERVICE_PREFIX in .env}"
MODEL_NAME="${MODEL_NAME:?Set MODEL_NAME in .env}"
ACTIVE_ROLES_CSV="${ACTIVE_ROLES_CSV:-cfo,cso,cmo,chro,cto}"
GEMINI_API_KEY_SECRET="${GEMINI_API_KEY_SECRET:-GEMINI_API_KEY}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-csuite-runtime}"
ARTIFACT_REPO="${ARTIFACT_REPO:-csuite}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
DECISION_LOG_BACKEND="${DECISION_LOG_BACKEND:-memory}"
HITL_CONFIDENCE_FLOOR="${HITL_CONFIDENCE_FLOOR:-0.75}"

RUNTIME_SA="${RUNTIME_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
REGISTRY="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${ARTIFACT_REPO}"
IFS=',' read -ra ROLES <<< "${ACTIVE_ROLES_CSV// /}"

log "Project ......... $GCP_PROJECT_ID"
log "Region .......... $GCP_REGION"
log "Prefix .......... $SERVICE_PREFIX"
log "Roles ........... ${ROLES[*]}"
log "Model ........... $MODEL_NAME"
log "Image tag ....... $IMAGE_TAG"
log "Decision log .... $DECISION_LOG_BACKEND"
echo

# --------------------------------------------------------------------------
# 1. APIs
# --------------------------------------------------------------------------
log "Enabling required APIs (idempotent)…"
APIS=(run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
      artifactregistry.googleapis.com generativelanguage.googleapis.com)
[[ "$DECISION_LOG_BACKEND" == "firestore" ]] && APIS+=(firestore.googleapis.com)
gcloud services enable "${APIS[@]}" --project "$GCP_PROJECT_ID" --quiet

# --------------------------------------------------------------------------
# 2. Secret must already exist -- we never create or print its value
# --------------------------------------------------------------------------
if ! gcloud secrets describe "$GEMINI_API_KEY_SECRET" --project "$GCP_PROJECT_ID" &>/dev/null; then
  die "Secret '$GEMINI_API_KEY_SECRET' not found in project $GCP_PROJECT_ID. Create it:

  gcloud secrets create $GEMINI_API_KEY_SECRET --replication-policy=automatic --project=$GCP_PROJECT_ID
  printf '%s' 'YOUR_GEMINI_API_KEY' | gcloud secrets versions add $GEMINI_API_KEY_SECRET --data-file=- --project=$GCP_PROJECT_ID"
fi
log "Secret '$GEMINI_API_KEY_SECRET' present."

# --------------------------------------------------------------------------
# 3. Artifact Registry
# --------------------------------------------------------------------------
if ! gcloud artifacts repositories describe "$ARTIFACT_REPO" \
      --location "$GCP_REGION" --project "$GCP_PROJECT_ID" &>/dev/null; then
  log "Creating Artifact Registry repo '$ARTIFACT_REPO'…"
  gcloud artifacts repositories create "$ARTIFACT_REPO" \
    --repository-format=docker --location "$GCP_REGION" \
    --description="AI C-Suite demo images" --project "$GCP_PROJECT_ID" --quiet
fi

# --------------------------------------------------------------------------
# 4. Runtime service account (single least-privilege identity)
# --------------------------------------------------------------------------
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$GCP_PROJECT_ID" &>/dev/null; then
  log "Creating runtime service account $RUNTIME_SA…"
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="AI C-Suite runtime" --project "$GCP_PROJECT_ID" --quiet
fi

log "Granting Secret Manager access to the runtime service account…"
gcloud secrets add-iam-policy-binding "$GEMINI_API_KEY_SECRET" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "$GCP_PROJECT_ID" --quiet >/dev/null

if [[ "$DECISION_LOG_BACKEND" == "firestore" ]]; then
  log "Granting Firestore access…"
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/datastore.user" --quiet >/dev/null
fi

# --------------------------------------------------------------------------
# 5. Build both images
# --------------------------------------------------------------------------
EXEC_IMAGE="${REGISTRY}/executive:${IMAGE_TAG}"
ORCH_IMAGE="${REGISTRY}/orchestrator:${IMAGE_TAG}"

log "Building executive image…"
gcloud builds submit --config deploy/cloudbuild.yaml \
  --substitutions "_DOCKERFILE=services/executive/Dockerfile,_IMAGE=${EXEC_IMAGE}" \
  --project "$GCP_PROJECT_ID" --quiet

log "Building orchestrator image…"
gcloud builds submit --config deploy/cloudbuild.yaml \
  --substitutions "_DOCKERFILE=services/orchestrator/Dockerfile,_IMAGE=${ORCH_IMAGE}" \
  --project "$GCP_PROJECT_ID" --quiet

# --------------------------------------------------------------------------
# 6. Executive agents -- one image, five PRIVATE deployments
# --------------------------------------------------------------------------
declare -A AGENT_URL
for ROLE in "${ROLES[@]}"; do
  SVC="${SERVICE_PREFIX}-${ROLE}"
  log "Deploying executive agent: $SVC (EXEC_ROLE=$ROLE)"
  gcloud run deploy "$SVC" \
    --image "$EXEC_IMAGE" \
    --project "$GCP_PROJECT_ID" \
    --region "$GCP_REGION" \
    --service-account "$RUNTIME_SA" \
    --no-allow-unauthenticated \
    --memory "${AGENT_MEMORY:-512Mi}" \
    --cpu "${AGENT_CPU:-1}" \
    --timeout "${AGENT_TIMEOUT:-300}" \
    --min-instances "${AGENT_MIN_INSTANCES:-0}" \
    --max-instances "${AGENT_MAX_INSTANCES:-4}" \
    --set-env-vars "EXEC_ROLE=${ROLE},GCP_PROJECT_ID=${GCP_PROJECT_ID},GCP_REGION=${GCP_REGION},MODEL_NAME=${MODEL_NAME},CONFIDENCE_FLOOR=${HITL_CONFIDENCE_FLOOR},LOG_LEVEL=${LOG_LEVEL:-INFO}" \
    --set-secrets "GEMINI_API_KEY=${GEMINI_API_KEY_SECRET}:latest" \
    --quiet

  # Only the orchestrator's identity may invoke this agent.
  gcloud run services add-iam-policy-binding "$SVC" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/run.invoker" \
    --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --quiet >/dev/null

  AGENT_URL["$ROLE"]="$(gcloud run services describe "$SVC" \
    --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --format='value(status.url)')"
  log "  → ${AGENT_URL[$ROLE]}  (private, invoker: $RUNTIME_SA)"
done

# Assemble the role -> URL map the orchestrator needs.
AGENT_URLS_JSON="{"; SEP=""
for ROLE in "${ROLES[@]}"; do
  AGENT_URLS_JSON+="${SEP}\"${ROLE}\":\"${AGENT_URL[$ROLE]}\""; SEP=","
done
AGENT_URLS_JSON+="}"

# --------------------------------------------------------------------------
# 7. Orchestrator -- PUBLIC
# --------------------------------------------------------------------------
ORCH_SVC="${SERVICE_PREFIX}-orchestrator"
log "Deploying orchestrator: $ORCH_SVC"

ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
cat > "$ENV_FILE" <<YAML
GCP_PROJECT_ID: "${GCP_PROJECT_ID}"
GCP_REGION: "${GCP_REGION}"
MODEL_NAME: "${MODEL_NAME}"
MODEL_TEMPERATURE: "${MODEL_TEMPERATURE:-0.2}"
LOG_LEVEL: "${LOG_LEVEL:-INFO}"
SERVICE_AUTH_MODE: "id_token"
AGENT_URLS_JSON: '${AGENT_URLS_JSON}'
ACTIVE_ROLES_CSV: "${ACTIVE_ROLES_CSV}"
AGENT_TIMEOUT_SECONDS: "${AGENT_TIMEOUT_SECONDS:-120}"
AGENT_MAX_RETRIES: "${AGENT_MAX_RETRIES:-2}"
HITL_ENABLED: "${HITL_ENABLED:-true}"
HITL_CONFIDENCE_FLOOR: "${HITL_CONFIDENCE_FLOOR}"
HITL_REQUIRE_CITATIONS: "${HITL_REQUIRE_CITATIONS:-true}"
HITL_FINAL_SIGNOFF: "${HITL_FINAL_SIGNOFF:-true}"
HITL_TIMEOUT_SECONDS: "${HITL_TIMEOUT_SECONDS:-900}"
DECISION_LOG_BACKEND: "${DECISION_LOG_BACKEND}"
FIRESTORE_COLLECTION: "${FIRESTORE_COLLECTION:-csuite_decision_log}"
MAX_UPLOAD_BYTES: "${MAX_UPLOAD_BYTES:-1048576}"
ALLOWED_UPLOAD_EXTENSIONS_CSV: "${ALLOWED_UPLOAD_EXTENSIONS_CSV:-.md,.markdown,.txt,.csv}"
YAML

# max-instances 1 keeps a run's live state on one instance, which the SSE
# dashboard and the blocking human-review gate both rely on.
gcloud run deploy "$ORCH_SVC" \
  --image "$ORCH_IMAGE" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --memory "${ORCHESTRATOR_MEMORY:-1Gi}" \
  --cpu "${ORCHESTRATOR_CPU:-1}" \
  --timeout "${ORCHESTRATOR_TIMEOUT:-3600}" \
  --min-instances "${ORCHESTRATOR_MIN_INSTANCES:-1}" \
  --max-instances 1 \
  --env-vars-file "$ENV_FILE" \
  --set-secrets "GEMINI_API_KEY=${GEMINI_API_KEY_SECRET}:latest" \
  --quiet

ORCH_URL="$(gcloud run services describe "$ORCH_SVC" \
  --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --format='value(status.url)')"

printf '\n\033[1;32m✓ Deployment complete\033[0m\n\n'
printf '  Dashboard ....... %s\n' "$ORCH_URL"
printf '  Agent health .... %s/api/agents/health\n' "$ORCH_URL"
printf '  Executive agents  %d private Cloud Run services\n\n' "${#AGENT_URL[@]}"
printf '  Verify before you present:\n'
printf '    curl -s %s/api/agents/health | jq\n\n' "$ORCH_URL"
