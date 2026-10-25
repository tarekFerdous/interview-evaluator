# Interview Evaluator

An LLM-powered service that scores a candidate's interview answers against a hiring rubric.
You send a job description, rubric competencies and a Q&A transcript; it returns a
validated, structured evaluation with a 1–5 score per competency (with quoted evidence),
strengths, concerns, red flags, follow-up questions and a hiring recommendation.

It also scores interviews **live** (re-scoring after every answer over Server-Sent Events),
takes **recordings and live microphone audio** (Azure AI Speech + Claude segmentation), keeps a
**rubric library** and **calibrates** scoring on evaluations your team has approved (Postgres +
pgvector + Voyage embeddings).

**Stack:** Python 3.13, FastAPI, Pydantic, Anthropic Claude API (structured outputs, prompt
caching), Postgres + pgvector, Voyage AI, Azure AI Speech, plain HTML/JS UI, pytest, uv, Docker,
Azure Container Apps (Bicep), GitHub Actions.

## Run it

```bash
uv sync
cp .env.example .env          # add your ANTHROPIC_API_KEY (everything else is optional)
uv run interview-evaluator    # http://127.0.0.1:8000 -> the web UI; API docs at /docs
```

Optional features switch on with configuration (see `.env.example`):

| Feature | Needs |
|---|---|
| API-key auth | `SERVICE_API_KEY` (unset = auth off, with a startup warning) |
| Stored evaluations, approve/calibrate | `DATABASE_URL` (Postgres with pgvector) |
| Rubric library, suggestions, calibration retrieval | `DATABASE_URL` + `VOYAGE_API_KEY` |
| Recording upload, live microphone | `AZURE_SPEECH_KEY` + `AZURE_SPEECH_REGION` |

### With Docker

```bash
docker compose up --build     # app on http://localhost:8000 + Postgres/pgvector (migrations run at startup)
PORT=8123 docker compose up   # different port
```

The image is a multi-stage uv build (installs from `uv.lock`), runs as a non-root user and
binds `0.0.0.0:$PORT` (default 8000). The app fails fast at startup if `DATABASE_URL` is set but
the database is unreachable.

## API

```bash
curl -X POST http://127.0.0.1:8000/evaluate \
  -H "Content-Type: application/json" -H "X-API-Key: $SERVICE_API_KEY" \
  -d @examples/sample_request.json
```

> **Breaking change (rubric library release):** `POST /evaluate` now returns
> `{"id", "evaluation", "calibration_example_ids"}`. The scorecard that used to be the whole
> response is now under `evaluation` (its schema is unchanged). `id` is `null` when persistence
> is off. `POST /sessions/{id}/end` returns the same shape.

| Endpoint | Purpose |
|---|---|
| `POST /evaluate` | One-shot evaluation. Send `rubric` (list) **or** `rubric_id` (saved rubric), not both. |
| `POST /sessions`, `POST /sessions/{id}/qa`, `GET /sessions/{id}`, `POST /sessions/{id}/end` | Live session: add Q&As one at a time; each triggers a full re-score (one in flight per session, extra Q&As coalesced). Sessions expire after 2 h idle. |
| `GET /sessions/{id}/events` | SSE stream: `scoring`, `scorecard`, `error`, `ended`. The only endpoint that also accepts the key as `?key=` (EventSource can't send headers). |
| `PUT /sessions/{id}/qa/last` | Fix the most recent Q&A and re-score. |
| `POST /sessions/{id}/segment` | Live mic: segment diarized utterances into Q&As and append them. |
| `POST /speech/token` | Short-lived Azure Speech token + region for the browser; the key never leaves the server. |
| `POST /transcriptions`, `GET /transcriptions/{id}` | Upload wav/mp3/m4a (≤100 MB, ≤60 min); poll until `done` with diarized utterances and Q&A pairs. Audio is never stored. |
| `POST/GET /rubrics`, `GET/PUT/DELETE /rubrics/{id}`, `POST /rubrics/suggest` | Rubric library; suggestions ranked by embedding similarity to a job description. |
| `GET/DELETE /evaluations/{id}`, `POST /evaluations/{id}/approve`, `POST /evaluations/{id}/unapprove` | Stored evaluations; approval (optionally with corrected scores/recommendation) makes them calibration examples. DELETE removes the transcript for data-deletion requests. |

Everything except `/health`, `/`, `/ui` and the docs needs `X-API-Key` (when configured) and is
rate limited per client IP (`RATE_LIMIT_PER_MIN`, default 10; 429 with `Retry-After`).

## Web UI

`/ui` (and `/`): paste a transcript (`Q:`/`A:` lines) or edit Q&A pairs, upload a call
recording, pick or save a rubric, evaluate, and approve the scorecard with edits. The **Live
interview** tab scores as you go, highlights score changes and flags competencies still without
evidence, and can transcribe the call from your microphone with live captions. Plain static
files, no build step.

### Live microphone: two audio setups

- **Remote call** (default, Chrome/Edge only): your microphone is the *Interviewer* and the call's
  audio is the *Candidate*. When asked, pick the call's browser tab (Teams/Meet/Zoom web) and tick
  **Share tab audio**; for a desktop meeting app on Windows, share your entire screen with system
  audio. Each side is transcribed separately, so nobody's voice has to be told apart. **Use
  headphones**, or your mic picks up the candidate too. This runs two Azure Speech streams at once,
  so it uses audio hours twice as fast (the F0 free tier's ~5 h/month is ~2.5 h of interview).
  Stopping the share from the browser's bar pauses the microphone.
- **In the room**: one microphone hears everyone and Azure diarization labels the voices.

An exchange (question + answer) is sent for segmentation when the interviewer speaks again after
the candidate. **Send exchange now** sends what's buffered at any time, and **End interview**
always sends it. If only one speaker label was detected, Claude splits questions from answers by
their content. A question that has no answer yet stays buffered for the next send.

## Tests

```bash
uv run pytest
```

The tests use fakes for Claude, Voyage and Azure Speech, so they need no API keys or network.
Database-backed tests run against a real Postgres and are skipped (with the reason shown in
`pytest -rs`) unless `TEST_DATABASE_URL` is set. **That database is wiped by the tests.**

```bash
docker compose up -d db
TEST_DATABASE_URL=postgresql://evaluator:evaluator@localhost:5433/evaluator_test uv run pytest
```

CI runs the full suite, DB tests included, against a Postgres service container.

## Eval set: measuring score agreement

`evals/` holds labelled interview cases (an `EvaluationRequest` plus human-owned expected
scores and recommendation) and a runner that measures how closely the evaluator agrees.

1. **Generate** draft cases with a different model from the grader (`claude-sonnet-5-5`):
   ```bash
   uv run python -m evals.generate                      # 5 roles × strong/mixed/weak, skips existing files
   uv run python -m evals.generate --role backend --quality weak --force
   ```
   Cases are written to `evals/cases/` with `reviewed: false`.
2. **Review** each case: read the transcript, correct `expected.scores` /
   `expected.recommendation`, then set `"reviewed": true`. Only reviewed cases count.
3. **Run** the real evaluator over them:
   ```bash
   uv run python -m evals.run                           # reviewed cases only
   uv run python -m evals.run --filter 'backend-*'      # subset by id glob (repeatable)
   uv run python -m evals.run --include-unreviewed --threshold 85
   ```
   It prints exact-match %, within-±1 %, mean absolute error, recommendation match %, every
   disagreement larger than 1, and PASS/FAIL (within-±1 ≥ threshold; exits non-zero on FAIL).
   Broken case files and per-case LLM errors are reported without stopping the run. Each run is
   saved to `evals/results/<timestamp>.json` (git-ignored) with the model and a system-prompt hash.

**Baseline (provisional):** over the 2 seed cases plus the 15 generated drafts (labels not yet
human-reviewed), `claude-opus-5-5` at medium effort scored 75.3% exact, 100% within ±1,
MAE 0.25, 94.1% recommendation agreement: PASS. The run after adding prompt caching gave
identical numbers. Re-baseline once the drafts are reviewed.

## Deploying to Azure Container Apps

`infra/main.bicep` creates: Container Registry (no admin user), Log Analytics, a Container Apps
environment, the Container App (external ingress, exactly 1 replica, `/health` probes, pulls
with a user-assigned managed identity holding `AcrPull`), Azure Database for PostgreSQL Flexible
Server (Burstable B1ms, `vector` allow-listed, Azure-services firewall rule, TLS required), and an
Azure AI Speech (S0) resource. Secrets (`ANTHROPIC_API_KEY`, `SERVICE_API_KEY`, `DATABASE_URL`,
`AZURE_SPEECH_KEY`, optional `VOYAGE_API_KEY`) are Container App secrets, never image or repo
literals.

`.github/workflows/ci-cd.yml` runs the tests on every PR and push; on `main` it signs in to Azure
with **OIDC** (no stored Azure secret), deploys the shared infra, builds and pushes the image
tagged with the commit SHA, deploys the app on that image and fails unless `/health` answers.

### One-time bootstrap

```bash
RG=interview-evaluator; LOCATION=westeurope; REPO=<owner>/<repo>
az group create -n $RG -l $LOCATION

# App registration + service principal that GitHub Actions signs in as
APP_ID=$(az ad app create --display-name interview-evaluator-ci --query appId -o tsv)
az ad sp create --id $APP_ID
az role assignment create --assignee $APP_ID --role Owner \
  --scope $(az group show -n $RG --query id -o tsv)   # Owner: the Bicep creates role assignments

# Federated credential for the `production` environment of this repo
az ad app federated-credential create --id $APP_ID --parameters '{
  "name": "github-production",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:'$REPO':environment:production",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

In GitHub, create the `production` environment and set:

- **Variables:** `AZURE_CLIENT_ID` (= `$APP_ID`), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
  `AZURE_RESOURCE_GROUP` (= `$RG`)
- **Secrets:** `ANTHROPIC_API_KEY`, `SERVICE_API_KEY`, `POSTGRES_ADMIN_PASSWORD`, and optionally
  `VOYAGE_API_KEY`

Push to `main` to deploy. The app URL is printed by the "Deploy Container App" step.

## Design decisions

- **Structured outputs:** the Pydantic `Evaluation` model is the LLM's output schema, so
  scorecards are always schema-valid. Segmentation of transcripts uses the same approach into
  the existing `QA` model.
- **Evidence-grounded scoring:** the system prompt requires quoted evidence per score and a
  score of 1 when there is no evidence.
- **Fairness guardrail:** the prompt tells the model to ignore attributes unrelated to job
  performance.
- **Prompt caching:** the system prompt, the job description + rubric prefix and any calibration
  examples are cached; the transcript goes last. Live sessions re-score the whole transcript
  each turn but re-read the prefix from cache. Calibration examples for a session are retrieved
  once at start so the prefix stays stable.
- **Calibration RAG:** with a saved rubric, up to 3 approved evaluations of that rubric, nearest
  by cosine similarity, are added as scoring anchors: transcript plus the human-approved scores
  only, never the model's reasoning. Without examples the prompt is identical to the uncalibrated
  one.
- **Failure handling:** refusals → 422 (after Anthropic's server-side fallback model); rate
  limits, auth, API and connection errors map to distinct statuses, also as SSE `error` events.
- **Single replica:** rate limits, live sessions and transcription jobs are in memory, so the
  app is pinned to one replica. Move them to Postgres/Redis before scaling out.
- **Privacy:** audio is never stored; transcripts are stored only with evaluations (when
  persistence is on) and can be deleted via `DELETE /evaluations/{id}`.
