# AI C-Suite — Multi-Agent Orchestration on Google Cloud

Upload a document, give an objective, and a committee of five executive AI
agents — CFO, CSO, CMO, CHRO, CTO — works the problem. An Orchestrator agent
calls each of them **over HTTP**, holds anything they can't prove for a human
to check, and only then synthesises a recommendation.

Built for the Google Developer Group Sydney demo.

---

## What this demonstrates

| Message | How it shows up |
|---|---|
| **Automating repetitive work on incoming data** | Drop in a pipeline export, incident report or campaign report; the committee produces a structured executive read every time. |
| **Multi-agent systems on Google Cloud** | Six independent Cloud Run services. The Orchestrator calls the five agents over authenticated HTTP — real network hops, visible on the dashboard. |
| **Agents that don't hallucinate, because a human is in the loop** | Every claim must cite numbered lines of the source document and carry a confidence score. Uncited or low-confidence claims **block** the run until a human approves, edits, or rejects them. |
| **A compelling, non-region-specific use case** | GlobalTech Solutions is a fictional global SaaS company with offices in San Francisco, London and Singapore. Nothing in the scenario is Australia-specific. |

---

## Architecture

```
                    ┌──────────────────────────────────────┐
   markdown ───────▶│  Orchestrator  (Cloud Run, PUBLIC)   │
   + objective      │  dashboard · decision log · HITL gate │
                    └───────────────┬──────────────────────┘
                                    │  HTTPS POST /invoke
                       OIDC identity token per call
        ┌──────────┬────────────┬───┴────────┬────────────┬──────────┐
        ▼          ▼            ▼            ▼            ▼          
   ┌─────────┐┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │   CFO   ││   CSO   │ │   CMO   │ │  CHRO   │ │   CTO   │   Cloud Run
   │ PRIVATE ││ PRIVATE │ │ PRIVATE │ │ PRIVATE │ │ PRIVATE │   (no public
   └─────────┘└─────────┘ └─────────┘ └─────────┘ └─────────┘    ingress)
        └──────────┴────────────┴────────────┴────────────┘
                          Gemini API
                 (key resolved from Secret Manager)
```

The five agents run **the same container image**. `EXEC_ROLE` selects which
persona each deployment adopts and which `agents/<role>/SKILL.md` it loads.
Adding a sixth executive is a new directory plus one registry entry — no
change to either service.

### Handoff is sequential, and that's deliberate

The Orchestrator calls the CFO first, then passes the CFO's findings to the
CSO, and so on. Each executive sees what the committee has already concluded
and can build on it or contest it. On the dashboard you watch the baton move.

---

## The anti-hallucination design

Four independent controls, each visible during the demo:

1. **Numbered source lines.** The uploaded document is line-numbered before it
   reaches any agent. Agents cite `L42-L45`; a reviewer clicks the citation and
   sees exactly those lines.
2. **Schema-constrained output.** Agents cannot return prose. Gemini must
   return an object where every finding carries `confidence`, `citations`, and
   `requires_human_review`.
3. **Orchestrator-side validation.** A citation pointing outside the document
   is itself a hallucination — it's discarded and the finding's confidence is
   forced below the floor, which routes it to a human.
4. **A blocking human gate.** The Orchestrator will not synthesise while any
   escalation is unresolved. Rejected findings are excluded from the synthesis
   prompt entirely; edited findings go in with the human's wording.

Anything an agent can't determine goes into `open_questions` rather than into a
claim. An acknowledged gap is a correct answer.

---

## Repository layout

```
shared/csuite_common/   HTTP contract, config, Secret Manager, Gemini, auth, roles
services/executive/     One agent service — deployed 5x, role via EXEC_ROLE
services/orchestrator/  Orchestrator, decision log, SSE dashboard
agents/<role>/SKILL.md  Executive role definitions (markdown, no code)
context/                Shared company context every agent receives
demo_files/             Realistic inputs to upload during the demo
deploy/                 deploy.sh (Cloud Run), run_local.sh, cloudbuild.yaml
```

---

## Deploy to Cloud Run

```bash
cp .env.example .env          # set GCP_PROJECT_ID, GCP_REGION, MODEL_NAME

# One-time: store the Gemini API key in Secret Manager
gcloud secrets create GEMINI_API_KEY --replication-policy=automatic
printf '%s' 'YOUR_KEY' | gcloud secrets versions add GEMINI_API_KEY --data-file=-

./deploy/deploy.sh
```

`deploy.sh` enables the APIs, creates a least-privilege runtime service
account, builds both images, deploys the five agents **privately**, grants the
Orchestrator's identity `roles/run.invoker` on each, then deploys the
Orchestrator publicly with the agent URL map injected as configuration.

Verify before you present:

```bash
curl -s "$ORCH_URL/api/agents/health" | jq
```

## Run locally

```bash
cp .env.example .env
# uncomment GEMINI_API_KEY in .env (local only)
pip install -r services/orchestrator/requirements.txt \
            -r services/executive/requirements.txt
./deploy/run_local.sh          # 6 processes, real HTTP between them
```

Opens on <http://127.0.0.1:8080>. `SERVICE_AUTH_MODE=none` locally — there's
no metadata server to mint identity tokens from.

---

## Demo runbook

Roughly seven minutes.

**1 · Frame it (30s).** "Five executives, five Cloud Run services. The
orchestrator is going to call each of them over HTTP. Watch the top row."

**2 · Upload (30s).** Choose from `demo_files/`:

| File | Draws out |
|---|---|
| `infrastructure_incident_report.md` | CTO leads; CFO on credits, CMO on customer comms — a genuinely cross-functional incident |
| `q3_enterprise_pipeline.csv` | Three high-risk renewals worth $3.35M; CFO/CSO tension |
| `campaign_performance_report.md` | CMO leads, but the case-study timing question pulls in CSO and CFO |
| `engineering_engagement_survey.csv` | CHRO leads; Core Analytics attrition ties straight to the incident report |

Objective, e.g.: *"Three enterprise accounts are up for renewal within six
weeks and all three raised tickets during this incident. Tell me what we do."*

**3 · Watch the handoff (90s).** Agent cards light up in sequence. The table
below shows the actual endpoint, HTTP status, latency, payload sizes. Point at
the auth column: `id_token`. These services are not on the public internet.

**4 · The run stops (2m).** This is the moment. The status banner reads
*"Paused. The orchestrator will not synthesise until you clear the
escalations."* Walk the review queue:

- a **low-confidence** finding — the agent inferred beyond the document;
- an **uncited** finding — nothing in the source supports it;
- an **agent-flagged** finding — it would commit money or headcount.

Click a citation to open the source at that exact line. Reject one, edit
another. Say plainly: *the model doesn't get to decide this.*

**5 · Synthesis (60s).** Only now does the Orchestrator consolidate. The
rejected finding is absent. The edited wording is present. Disagreement between
executives is preserved under "Dissent" rather than averaged away.

**6 · Sign-off (30s).** One more gate before it counts as a decision.

**7 · The decision log (60s).** Scroll it. Every dispatch, every response,
every escalation, every human keystroke — timestamped and sequenced. *"This is
what you hand your auditor."*

### If something goes wrong on stage

- An agent 500s → the run continues and the synthesis says that executive's
  domain is unassessed. Recovering gracefully is a better story than a demo
  that can't fail.
- Nothing gets escalated → lower `HITL_CONFIDENCE_FLOOR` and redeploy, or use
  an objective the document only partly answers (that reliably produces
  low-confidence findings).
- Cold start is slow → set `AGENT_MIN_INSTANCES=1` before the session.

---

## Configuration

Every knob is an environment variable; see `.env.example`. Nothing is
hard-coded — project, region, model, confidence floor, timeouts, retries,
active roles, upload limits and the decision-log backend are all injected.

**Secrets never enter this repository.** The Gemini API key lives in Secret
Manager and is resolved at runtime by each service. `.env` is gitignored;
`.env.example` contains names, never values.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Dashboard |
| `GET` | `/api/config` | Dashboard configuration (no secrets) |
| `GET` | `/api/agents/health` | Probe all five agents |
| `POST` | `/api/runs` | Start a run (`objective` + optional `file`) |
| `GET` | `/api/runs/{id}/events` | SSE stream of the live run |
| `GET` | `/api/runs/{id}/decisions` | Decision log |
| `GET` | `/api/runs/{id}/source` | Numbered source, for verifying citations |
| `POST` | `/api/runs/{id}/reviews/{rid}` | Approve / edit / reject |
| `POST` | `/invoke` | *(agent services)* Orchestrator entry point |

---

## Notes and limits

This is demo software, built to be read from the back of a room.

- Run state lives in the Orchestrator's memory, so it deploys with
  `--max-instances 1`. Multi-instance would need shared state (Firestore or
  Redis) behind the SSE stream.
- `DECISION_LOG_BACKEND=firestore` persists the audit trail; `memory` is the
  default because a live demo shouldn't depend on a second service.
- There is no authentication on the Orchestrator itself. Add IAP or Cloud Run
  IAM before this goes anywhere near real data.
