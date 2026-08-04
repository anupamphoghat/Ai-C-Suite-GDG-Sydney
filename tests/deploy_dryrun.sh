#!/usr/bin/env bash
#
# Dry-run deploy.sh against a mocked gcloud.
#
#   ./tests/deploy_dryrun.sh
#
# Executes every line of the real deploy script -- no Google Cloud access, no
# cost, no side effects -- and asserts the resource names and the agent URL
# map it produces. Catches unbound variables, array bugs and quoting mistakes
# that only surface partway through a real deploy.
#
# Run this on the machine you will deploy from: it exercises deploy.sh under
# *your* bash, which is the version that matters (macOS ships bash 3.2).
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS="\033[1;32m✓\033[0m"; FAILM="\033[1;31m✗\033[0m"
FAILURES=0
check() {
  if [[ "$2" == "$3" ]]; then
    printf "  ${PASS} %s\n" "$1"
  else
    printf "  ${FAILM} %s\n      expected: %s\n      actual:   %s\n" "$1" "$3" "$2"
    FAILURES=$((FAILURES + 1))
  fi
}
contains() {
  if grep -qF "$2" "$3"; then printf "  ${PASS} %s\n" "$1"
  else printf "  ${FAILM} %s (missing: %s)\n" "$1" "$2"; FAILURES=$((FAILURES + 1)); fi
}

printf '\nbash %s\n' "${BASH_VERSION}"

# --------------------------------------------------------------------------
# Portability lint
#
# macOS ships bash 3.2, so the deploy scripts must avoid bash 4+ syntax. The
# adjacency check catches the specific bug where a Unicode character sits
# directly against a variable name ("$RUNTIME_SA…"), which bash 3.2 parses as
# part of the variable name and `set -u` then kills.
# --------------------------------------------------------------------------
printf '\n-- Portability lint (bash 3.2 target) --\n'
SCRIPTS=("$REPO_ROOT"/deploy/*.sh)

lint() {  # label, grep-args...  (comment lines are ignored)
  local label="$1"; shift
  local hits
  hits="$(grep -nHE "$@" "${SCRIPTS[@]}" 2>/dev/null \
          | grep -vE '^[^:]*:[0-9]+:[[:space:]]*#' || true)"
  if [[ -z "$hits" ]]; then
    printf "  ${PASS} %s\n" "$label"
  else
    printf "  ${FAILM} %s\n%s\n" "$label" "$(echo "$hits" | sed 's/^/      /')"
    FAILURES=$((FAILURES + 1))
  fi
}

lint "no associative arrays (declare -A)"      'declare[[:space:]]+-A'
lint "no \${var^^} / \${var,,} case expansion"  '\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^|,,)'
lint "no mapfile / readarray"                  '\b(mapfile|readarray)\b'
lint "no &>> append redirect"                  '&>>'

# The original bug: a variable reference touching a multi-byte character.
ADJACENT="$(grep -nHP '\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]' "${SCRIPTS[@]}" 2>/dev/null \
            | grep -vE '^[^:]*:[0-9]+:[[:space:]]*#' || true)"
if [[ -z "$ADJACENT" ]]; then
  printf "  ${PASS} no unbraced variable touching a non-ASCII character\n"
else
  printf "  ${FAILM} unbraced variable touching a non-ASCII character\n%s\n" \
    "$(echo "$ADJACENT" | sed 's/^/      /')"
  FAILURES=$((FAILURES + 1))
fi

# --------------------------------------------------------------------------
# Cloud Build config validation
#
# Cloud Build resolves enum-typed fields when it parses cloudbuild.yaml,
# before substitutions are applied. A ${_VAR} in one of those fields fails at
# submit time with e.g. ".options.machineType: unused". The mocked gcloud
# below cannot catch that, so check it statically.
# --------------------------------------------------------------------------
printf '\n-- Cloud Build config --\n'
CB="$REPO_ROOT/deploy/cloudbuild.yaml"

if command -v python3 >/dev/null; then
  CB_REPORT="$(python3 - "$CB" <<'PY'
import re, sys

path = sys.argv[1]
raw = open(path, encoding="utf-8").read()
# Scan the config only, not the comments explaining it.
text = "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("#"))
problems, notes = [], []

# Fields Cloud Build treats as enums; a substitution in any of them is fatal.
ENUM_FIELDS = (
    "machineType", "logging", "substitutionOption", "requestedVerifyOption",
    "logStreamingOption", "defaultLogsBucketBehavior", "sourceProvenanceHash",
    "status", "pool",
)
for field in ENUM_FIELDS:
    for m in re.finditer(rf"^\s*{field}\s*:\s*(.+?)\s*$", text, re.M):
        if "${" in m.group(1):
            problems.append(f"{field}: substitution '{m.group(1)}' in an enum field")

# Every ${_VAR} used must have a default declared under substitutions:.
used = set(re.findall(r"\$\{(_[A-Z0-9_]+)\}", text))
sub_block = re.search(r"^substitutions:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
declared = set(re.findall(r"^\s+(_[A-Z0-9_]+)\s*:", sub_block.group(1), re.M)) if sub_block else set()
for name in sorted(used - declared):
    problems.append(f"{name} is used but not declared under substitutions:")

notes.append(f"substitutions used: {', '.join(sorted(used)) or 'none'}")

try:
    import yaml
    doc = yaml.safe_load(raw)
    if not doc.get("steps"):
        problems.append("no build steps defined")
    notes.append(f"steps: {len(doc.get('steps', []))}, images: {len(doc.get('images', []))}")
except ImportError:
    notes.append("PyYAML not installed; skipped structural parse")
except Exception as exc:
    problems.append(f"YAML parse error: {exc}")

for n in notes:
    print("NOTE " + n)
for p in problems:
    print("FAIL " + p)
PY
)"
  while IFS= read -r LINE; do
    [[ -z "$LINE" ]] && continue
    if [[ "$LINE" == FAIL* ]]; then
      printf "  ${FAILM} %s\n" "${LINE#FAIL }"; FAILURES=$((FAILURES + 1))
    else
      printf "  ${PASS} %s\n" "${LINE#NOTE }"
    fi
  done <<< "$CB_REPORT"
  [[ "$CB_REPORT" == *FAIL* ]] || printf "  ${PASS} no substitutions in enum-typed fields\n"
else
  printf "  ${FAILM} python3 not found; skipped cloudbuild.yaml validation\n"
fi

# --------------------------------------------------------------------------
# Mock gcloud: records every invocation, returns plausible output.
# --------------------------------------------------------------------------
mkdir -p "$SANDBOX/bin"
cat > "$SANDBOX/bin/gcloud" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "${MOCK_LOG}"

# deploy.sh removes its temp env file on exit, so snapshot it here instead.
PREV=""
for ARG in "$@"; do
  if [[ "$PREV" == "--env-vars-file" && -f "$ARG" ]]; then cp "$ARG" "${ENV_CAPTURE}"; fi
  PREV="$ARG"
done

case "$1 ${2:-} ${3:-}" in
  "config get-value project")   echo "mock-project" ;;
  "run services describe")
      # New services: no managed-by label, but a URL once "deployed".
      if [[ "$*" == *"metadata.labels.managed-by"* ]]; then
        exit 0
      elif [[ "$*" == *"status.url"* ]]; then
        echo "https://${4}-abc123-uc.a.run.app"
      else
        exit 1   # does not exist yet
      fi ;;
  "secrets describe "*)         exit 0 ;;
  "artifacts repositories describe") exit 1 ;;   # force the create path
  "iam service-accounts describe")   exit 1 ;;   # force the create path
  *) : ;;
esac
exit 0
MOCK
chmod +x "$SANDBOX/bin/gcloud"

# --------------------------------------------------------------------------
# Case 1: SERVICE_PREFIX="Tech" -- capitalised, as a user would naturally type
# --------------------------------------------------------------------------
run_deploy() {
  local prefix="$1"
  cp -R "$REPO_ROOT/" "$SANDBOX/repo" 2>/dev/null || {
    rm -rf "$SANDBOX/repo"; mkdir -p "$SANDBOX/repo"
    ( cd "$REPO_ROOT" && tar cf - --exclude=.git . ) | ( cd "$SANDBOX/repo" && tar xf - )
  }
  cat > "$SANDBOX/repo/.env" <<ENV
GCP_PROJECT_ID="mock-project"
GCP_REGION="us-central1"
SERVICE_PREFIX="${prefix}"
MODEL_NAME="gemini-3.6-flash"
ACTIVE_ROLES_CSV="cfo,cso,cmo,chro,cto"
DECISION_LOG_BACKEND="memory"
ENV
  : > "$SANDBOX/gcloud.log"
  rm -f "$SANDBOX/orchestrator_env.yaml"
  MOCK_LOG="$SANDBOX/gcloud.log" \
  ENV_CAPTURE="$SANDBOX/orchestrator_env.yaml" \
  PATH="$SANDBOX/bin:$PATH" \
    bash "$SANDBOX/repo/deploy/deploy.sh" > "$SANDBOX/out.txt" 2>&1
  echo $?
}

printf '\n── deploy.sh with SERVICE_PREFIX="Tech" ──\n'
EXIT_CODE="$(run_deploy "Tech")"
check "script exits 0" "$EXIT_CODE" "0"
if [[ "$EXIT_CODE" != "0" ]]; then
  printf '\n--- output ---\n'; cat "$SANDBOX/out.txt"; exit 1
fi

contains "normalises Tech -> tech" "Normalised SERVICE_PREFIX 'Tech' -> 'tech'" "$SANDBOX/out.txt"

printf '\n── Cloud Run service names ──\n'
for ROLE in cfo cso cmo chro cto orchestrator; do
  contains "deploys tech-${ROLE}" "run deploy tech-${ROLE} " "$SANDBOX/gcloud.log"
done
if grep -qE "run deploy tech-(cfo|cso|cmo|chro|cto|orchestrator)-" "$SANDBOX/gcloud.log"; then
  printf "  ${FAILM} no doubled role suffixes\n"; FAILURES=$((FAILURES + 1))
else
  printf "  ${PASS} no doubled role suffixes\n"
fi

printf '\n── Derived resource names ──\n'
contains "artifact repo tech-csuite"  "repositories create tech-csuite " "$SANDBOX/gcloud.log"
contains "service account tech-csuite-runtime" \
         "service-accounts create tech-csuite-runtime " "$SANDBOX/gcloud.log"

printf '\n── Security posture ──\n'
AGENT_PRIVATE=$(grep -c "run deploy tech-\(cfo\|cso\|cmo\|chro\|cto\) .*--no-allow-unauthenticated" "$SANDBOX/gcloud.log" || true)
check "all five agents deployed private" "$AGENT_PRIVATE" "5"
contains "orchestrator is public" "run deploy tech-orchestrator" "$SANDBOX/gcloud.log"
if grep -q "run deploy tech-orchestrator .*--allow-unauthenticated" "$SANDBOX/gcloud.log"; then
  printf "  ${PASS} orchestrator allows unauthenticated\n"
else
  printf "  ${FAILM} orchestrator missing --allow-unauthenticated\n"; FAILURES=$((FAILURES+1))
fi
INVOKER=$(grep -c "add-iam-policy-binding tech-.* --role=roles/run.invoker" "$SANDBOX/gcloud.log" || true)
check "run.invoker granted on all five agents" "$INVOKER" "5"
LABELS=$(grep -c "managed-by=ai-csuite-demo" "$SANDBOX/gcloud.log" || true)
check "every service labelled" "$LABELS" "6"

printf '\n── Secret handling ──\n'
if grep -qE "AIza|--set-env-vars.*GEMINI_API_KEY=[^:]*$" "$SANDBOX/gcloud.log"; then
  printf "  ${FAILM} an API key value reached a gcloud command line\n"; FAILURES=$((FAILURES+1))
else
  printf "  ${PASS} no key value on any command line\n"
fi
SECRET_REFS=$(grep -c -- "--set-secrets GEMINI_API_KEY=GEMINI_API_KEY:latest" "$SANDBOX/gcloud.log" || true)
check "all six services read the secret by reference" "$SECRET_REFS" "6"

printf '\n── Orchestrator configuration ──\n'
if [[ -f "$SANDBOX/orchestrator_env.yaml" ]]; then
  contains "agent URL map has all five roles" \
    '"cfo":"https://tech-cfo-abc123-uc.a.run.app","cso":"https://tech-cso-abc123-uc.a.run.app","cmo":"https://tech-cmo-abc123-uc.a.run.app","chro":"https://tech-chro-abc123-uc.a.run.app","cto":"https://tech-cto-abc123-uc.a.run.app"' \
    "$SANDBOX/orchestrator_env.yaml"
  contains "service auth is id_token"  'SERVICE_AUTH_MODE: "id_token"' "$SANDBOX/orchestrator_env.yaml"
  contains "HITL enabled"              'HITL_ENABLED: "true"'          "$SANDBOX/orchestrator_env.yaml"
  if grep -qE "GEMINI_API_KEY: " "$SANDBOX/orchestrator_env.yaml"; then
    printf "  ${FAILM} secret written into the env file\n"; FAILURES=$((FAILURES+1))
  else
    printf "  ${PASS} no secret in the env file\n"
  fi
else
  printf "  ${FAILM} orchestrator env file was never written\n"; FAILURES=$((FAILURES+1))
fi

# --------------------------------------------------------------------------
# Case 2: invalid prefixes must fail fast, before any resource is created
# --------------------------------------------------------------------------
printf '\n── Invalid prefixes rejected before any API call ──\n'
for BAD in "9tech" "tech_demo" "tech-" "ab"; do
  EXIT_CODE="$(run_deploy "$BAD")"
  CREATED=$(grep -cE "run deploy|repositories create|service-accounts create" "$SANDBOX/gcloud.log" || true)
  if [[ "$EXIT_CODE" != "0" && "$CREATED" == "0" ]]; then
    printf "  ${PASS} '%s' rejected, nothing created\n" "$BAD"
  else
    printf "  ${FAILM} '%s' exit=%s created=%s\n" "$BAD" "$EXIT_CODE" "$CREATED"
    FAILURES=$((FAILURES + 1))
  fi
done

printf '\n'
if [[ $FAILURES -gt 0 ]]; then
  printf '\033[1;31m%d check(s) failed.\033[0m\n\n' "$FAILURES"; exit 1
fi
printf '\033[1;32mAll checks passed.\033[0m\n\n'
