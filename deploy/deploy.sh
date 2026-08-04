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
MODEL_NAME="${MODEL_NAME:?Set MODEL_NAME in .env}"
ACTIVE_ROLES_CSV="${ACTIVE_ROLES_CSV:-cfo,cso,cmo,chro,cto}"
GEMINI_API_KEY_SECRET="${GEMINI_API_KEY_SECRET:-GEMINI_API_KEY}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
DECISION_LOG_BACKEND="${DECISION_LOG_BACKEND:-memory}"
HITL_CONFIDENCE_FLOOR="${HITL_CONFIDENCE_FLOOR:-0.75}"

# --------------------------------------------------------------------------
# 0a. Service naming
#
# SERVICE_PREFIX namespaces every resource this script creates, so the demo
# can share a project with other workloads without colliding. Cloud Run,
# Artifact Registry and IAM all require lowercase names, so we normalise
# rather than fail mid-deploy on a capitalised prefix.
# --------------------------------------------------------------------------
SERVICE_PREFIX="${SERVICE_PREFIX:?Set SERVICE_PREFIX in .env}"
SERVICE_PREFIX_RAW="$SERVICE_PREFIX"
SERVICE_PREFIX="$(printf '%s' "$SERVICE_PREFIX" | tr '[:upper:]' '[:lower:]')"
[[ "$SERVICE_PREFIX" == "$SERVICE_PREFIX_RAW" ]] \
  || log "Normalised SERVICE_PREFIX '${SERVICE_PREFIX_RAW}' -> '${SERVICE_PREFIX}' (Cloud Run requires lowercase)."

# 3-25 characters, starting with a letter and ending alphanumeric. The lower
# bound keeps the derived service account ID above Google's 6-character
# minimum; the trailing-character rule avoids names like "tech--cfo".
[[ "$SERVICE_PREFIX" =~ ^[a-z][a-z0-9-]{1,23}[a-z0-9]$ ]] || die \
  "SERVICE_PREFIX '${SERVICE_PREFIX_RAW}' is not a valid name component.
   It must start with a letter, end with a letter or digit, contain only
   lowercase letters, digits and hyphens, and be 3-25 characters long."

# Derived resource names, all namespaced by the prefix. Override any of them
# in .env if you need to.
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-${SERVICE_PREFIX}-csuite-runtime}"
ARTIFACT_REPO="${ARTIFACT_REPO:-${SERVICE_PREFIX}-csuite}"

# Cloud Run caps service names at 63 characters; the longest suffix here is
# "-orchestrator" (13).
LONGEST_SUFFIX="orchestrator"
for ROLE_CHECK in ${ACTIVE_ROLES_CSV//,/ }; do
  [[ ${#ROLE_CHECK} -gt ${#LONGEST_SUFFIX} ]] && LONGEST_SUFFIX="$ROLE_CHECK"
done
[[ $(( ${#SERVICE_PREFIX} + 1 + ${#LONGEST_SUFFIX} )) -le 63 ]] || die \
  "SERVICE_PREFIX '${SERVICE_PREFIX}' is too long: '${SERVICE_PREFIX}-${LONGEST_SUFFIX}' exceeds the 63-character Cloud Run limit."

# Service account IDs must be 6-30 characters. Fall back through progressively
# shorter forms; "<prefix>-sa" always fits, given the 25-character prefix cap.
if [[ ${#RUNTIME_SA_NAME} -gt 30 ]]; then
  for CANDIDATE in "${SERVICE_PREFIX}-runtime" "${SERVICE_PREFIX}-sa"; do
    if [[ ${#CANDIDATE} -le 30 ]]; then RUNTIME_SA_NAME="$CANDIDATE"; break; fi
  done
  [[ ${#RUNTIME_SA_NAME} -le 30 ]] || die \
    "Cannot derive a service account ID of 30 characters or fewer from
   SERVICE_PREFIX '${SERVICE_PREFIX}'. Set RUNTIME_SA_NAME explicitly in .env."
  log "Shortened runtime service account name to '${RUNTIME_SA_NAME}' (30-character limit)."
fi

RUNTIME_SA="${RUNTIME_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
REGISTRY="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${ARTIFACT_REPO}"
IFS=',' read -ra ROLES <<< "${ACTIVE_ROLES_CSV// /}"

log "Project ......... $GCP_PROJECT_ID"
log "Region .......... $GCP_REGION"
log "Prefix .......... $SERVICE_PREFIX"
log "Model ........... $MODEL_NAME"
log "Image tag ....... $IMAGE_TAG"
log "Decision log .... $DECISION_LOG_BACKEND"
log "Artifact repo ... $ARTIFACT_REPO"
log "Runtime SA ...... $RUNTIME_SA_NAME"
log "Cloud Run services:"
for ROLE in "${ROLES[@]}"; do log "    ${SERVICE_PREFIX}-${ROLE}  (private)"; done
log "    ${SERVICE_PREFIX}-orchestrator  (public)"
echo

# Refuse to clobber a service this deployment does not own.
for ROLE in "${ROLES[@]}" orchestrator; do
  SVC="${SERVICE_PREFIX}-${ROLE}"
  if gcloud run services describe "$SVC" --project "$GCP_PROJECT_ID" \
       --region "$GCP_REGION" --format='value(metadata.name)' &>/dev/null; then
    OWNER="$(gcloud run services describe "$SVC" --project "$GCP_PROJECT_ID" \
      --region "$GCP_REGION" --format='value(metadata.labels.managed-by)' 2>/dev/null || true)"
    if [[ "$OWNER" != "ai-csuite-demo" ]]; then
      die "Cloud Run service '${SVC}' already exists in ${GCP_REGION} and was not
   created by this demo. Pick a different SERVICE_PREFIX in .env so you do not
   overwrite someone else's service."
    fi
    log "Will update existing service ${SVC}."
  fi
done

# --------------------------------------------------------------------------
# 1. APIs
# --------------------------------------------------------------------------
log "Enabling required APIs (idempotent)..."
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
  log "Creating Artifact Registry repo '$ARTIFACT_REPO'..."
  gcloud artifacts repositories create "$ARTIFACT_REPO" \
    --repository-format=docker --location "$GCP_REGION" \
    --description="AI C-Suite demo images" --project "$GCP_PROJECT_ID" --quiet
fi

# --------------------------------------------------------------------------
# 4. Runtime service account (single least-privilege identity)
# --------------------------------------------------------------------------
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$GCP_PROJECT_ID" &>/dev/null; then
  log "Creating runtime service account $RUNTIME_SA..."
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="AI C-Suite runtime" --project "$GCP_PROJECT_ID" --quiet
fi

log "Granting Secret Manager access to the runtime service account..."
gcloud secrets add-iam-policy-binding "$GEMINI_API_KEY_SECRET" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "$GCP_PROJECT_ID" --quiet >/dev/null

if [[ "$DECISION_LOG_BACKEND" == "firestore" ]]; then
  log "Granting Firestore access..."
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/datastore.user" --quiet >/dev/null
fi

# --------------------------------------------------------------------------
# 5. Build both images
# --------------------------------------------------------------------------
EXEC_IMAGE="${REGISTRY}/executive:${IMAGE_TAG}"
ORCH_IMAGE="${REGISTRY}/orchestrator:${IMAGE_TAG}"
EXEC_IMAGE_LATEST="${REGISTRY}/executive:latest"
ORCH_IMAGE_LATEST="${REGISTRY}/orchestrator:latest"

log "Building executive image..."
gcloud builds submit --config deploy/cloudbuild.yaml \
  --substitutions "_DOCKERFILE=services/executive/Dockerfile,_IMAGE=${EXEC_IMAGE},_IMAGE_LATEST=${EXEC_IMAGE_LATEST}" \
  --project "$GCP_PROJECT_ID" --quiet

log "Building orchestrator image..."
gcloud builds submit --config deploy/cloudbuild.yaml \
  --substitutions "_DOCKERFILE=services/orchestrator/Dockerfile,_IMAGE=${ORCH_IMAGE},_IMAGE_LATEST=${ORCH_IMAGE_LATEST}" \
  --project "$GCP_PROJECT_ID" --quiet

# --------------------------------------------------------------------------
# 6. Executive agents -- one image, five PRIVATE deployments
# --------------------------------------------------------------------------
# Parallel indexed array to ROLES. Deliberately not an associative array:
# macOS ships bash 3.2, which has no `declare -A`.
AGENT_URLS=()
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
    --ingress "${AGENT_INGRESS:-all}" \
    --labels "managed-by=ai-csuite-demo,csuite-prefix=${SERVICE_PREFIX},csuite-role=${ROLE}" \
    --set-env-vars "EXEC_ROLE=${ROLE},GCP_PROJECT_ID=${GCP_PROJECT_ID},GCP_REGION=${GCP_REGION},MODEL_NAME=${MODEL_NAME},CONFIDENCE_FLOOR=${HITL_CONFIDENCE_FLOOR},LOG_LEVEL=${LOG_LEVEL:-INFO}" \
    --set-secrets "GEMINI_API_KEY=${GEMINI_API_KEY_SECRET}:latest" \
    --quiet

  # Only the orchestrator's identity may invoke this agent.
  gcloud run services add-iam-policy-binding "$SVC" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/run.invoker" \
    --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --quiet >/dev/null

  SVC_URL="$(gcloud run services describe "$SVC" \
    --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --format='value(status.url)')"
  [[ -n "$SVC_URL" ]] || die "Deployed ${SVC} but could not read back its URL."
  AGENT_URLS+=("$SVC_URL")
  log "  -> ${SVC_URL}  (private, invoker: ${RUNTIME_SA})"
done

# Assemble the role -> URL map the orchestrator needs, pairing ROLES with the
# parallel AGENT_URLS array by index.
AGENT_URLS_JSON="{"
for I in "${!ROLES[@]}"; do
  [[ $I -gt 0 ]] && AGENT_URLS_JSON="${AGENT_URLS_JSON},"
  AGENT_URLS_JSON="${AGENT_URLS_JSON}\"${ROLES[$I]}\":\"${AGENT_URLS[$I]}\""
done
AGENT_URLS_JSON="${AGENT_URLS_JSON}}"

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
  --ingress "${ORCHESTRATOR_INGRESS:-all}" \
  --labels "managed-by=ai-csuite-demo,csuite-prefix=${SERVICE_PREFIX},csuite-role=orchestrator" \
  --env-vars-file "$ENV_FILE" \
  --set-secrets "GEMINI_API_KEY=${GEMINI_API_KEY_SECRET}:latest" \
  --quiet

ORCH_URL="$(gcloud run services describe "$ORCH_SVC" \
  --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --format='value(status.url)')"

# --------------------------------------------------------------------------
# 8. Verify -- do not report success on a system that cannot talk to itself
# --------------------------------------------------------------------------
log "Verifying the orchestrator can reach all ${#AGENT_URLS[@]} agents..."

VERIFY_ATTEMPTS="${VERIFY_ATTEMPTS:-6}"
VERIFY_DELAY_SECONDS="${VERIFY_DELAY_SECONDS:-10}"
HEALTH_JSON=""
ATTEMPT=1
while [[ $ATTEMPT -le $VERIFY_ATTEMPTS ]]; do
  HEALTH_JSON="$(curl -fsS --max-time 60 "${ORCH_URL}/api/agents/health" 2>/dev/null || true)"
  case "$HEALTH_JSON" in
    *'"all_ok": true'*|*'"all_ok":true'*) break ;;
  esac
  if [[ $ATTEMPT -lt $VERIFY_ATTEMPTS ]]; then
    log "  not ready yet (attempt ${ATTEMPT}/${VERIFY_ATTEMPTS}); waiting ${VERIFY_DELAY_SECONDS}s..."
    sleep "$VERIFY_DELAY_SECONDS"
  fi
  ATTEMPT=$((ATTEMPT + 1))
done

case "$HEALTH_JSON" in
  *'"all_ok": true'*|*'"all_ok":true'*)
    printf '\n\033[1;32m✓ Deployment complete and verified\033[0m\n\n'
    printf '  Dashboard ....... %s\n' "$ORCH_URL"
    printf '  Agent health .... %s/api/agents/health\n' "$ORCH_URL"
    printf '  Executive agents  %d private Cloud Run services\n\n' "${#AGENT_URLS[@]}"
    ;;
  *)
    printf '\n\033[1;33m! Deployed, but the orchestrator cannot reach every agent.\033[0m\n\n'
    printf '%s\n\n' "${HEALTH_JSON:-  (no response from ${ORCH_URL}/api/agents/health)}"
    printf '  How to read this:\n'
    printf '    HTTP 404 + an HTML body  -> ingress restriction, NOT IAM. A Cloud Run\n'
    printf '                                service is not "internal" traffic for another\n'
    printf '                                Cloud Run service on a run.app URL. Check:\n'
    printf '                                  gcloud run services describe %s-cfo \\\n' "$SERVICE_PREFIX"
    printf '                                    --region %s --format="value(spec.template.metadata.annotations)"\n' "$GCP_REGION"
    printf '                                If ingress is internal, an org policy\n'
    printf '                                (constraints/run.allowedIngress) is overriding\n'
    printf '                                --ingress=all. You will need Direct VPC egress.\n'
    printf '    HTTP 403                 -> IAM. The runtime service account is missing\n'
    printf '                                roles/run.invoker on the agent service.\n'
    printf '    HTTP 503 + JSON body     -> the container started but could not initialise;\n'
    printf '                                usually the Gemini API key or MODEL_NAME.\n'
    printf '                                  gcloud run services logs read %s-cfo --region %s\n\n' "$SERVICE_PREFIX" "$GCP_REGION"
    exit 1
    ;;
esac
