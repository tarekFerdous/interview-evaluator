<div align="center">

# Interview Evaluator

*A second opinion for every interview, with the evidence quoted.*

**An LLM-powered service that scores job interviews against a hiring rubric with Claude.**
Paste a transcript, upload a call recording or score a live interview as it happens. You get a 1–5 score per competency backed by the candidate's own words, plus strengths, concerns, red flags, follow-up questions and a hiring recommendation.

![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-API%20%2B%20UI-009688?logo=fastapi&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-Opus%205.5%20%C2%B7%20structured%20outputs-D97757)
![Postgres + pgvector](https://img.shields.io/badge/Postgres-pgvector-4169E1?logo=postgresql&logoColor=white)
![Azure AI Speech](https://img.shields.io/badge/Azure%20AI%20Speech-live%20%2B%20recordings-0078D4?logo=microsoftazure&logoColor=white)
![Azure Container Apps](https://img.shields.io/badge/deploys%20on-Azure%20Container%20Apps-0078D4?logo=microsoftazure&logoColor=white)
![Near-zero cost](https://img.shields.io/badge/hosting-near--zero%20cost-2ea44f)
![Tests](https://img.shields.io/badge/tests-99-brightgreen)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![CI/CD](https://github.com/tarekFerdous/interview-evaluator/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/tarekFerdous/interview-evaluator/actions/workflows/ci-cd.yml)

<img src="docs/demo.gif" alt="Animated: filling in a Logistics Data Analyst job description and a seven-competency rubric, pasting a seven-question transcript and parsing it into Q&A pairs, clicking Evaluate, then scrolling a 'Strong yes' scorecard with a 1–5 bar, quoted evidence and reasoning per competency, strengths, concerns and follow-up questions" width="820">

<sub>A real run against Claude, recorded with Playwright. The 15-second wait for the scorecard is cut. The sample role and transcript are in <a href="examples/logistics_analyst.txt"><code>examples/logistics_analyst.txt</code></a>.</sub>

</div>

---

## Features

- **Evidence-grounded scorecards.** Every competency gets a 1–5 score with a quote or close paraphrase from the candidate. No evidence means a 1, and the scorecard says so. Claude never infers skills the candidate didn't show.
- **Always a valid result.** The Pydantic `Evaluation` model is Claude's output schema (structured outputs), so a scorecard can't come back malformed.
- **Three ways in.** Paste a transcript, upload a recording (wav, mp3 or m4a, up to 60 minutes), or score a live interview from your microphone.
- **Live scoring.** In the Live interview tab the scorecard re-scores after every answer and streams to the browser over Server-Sent Events. Score changes are highlighted, and competencies still without evidence are flagged so you know what to ask next.
- **Speech to Q&A.** Azure AI Speech transcribes with speaker diarization, then a second Claude call turns the utterances into question/answer pairs. It merges split turns, folds in follow-ups and drops small talk.
- **Two live audio setups.** *Remote call:* your mic is the interviewer and the call's tab audio is the candidate. *In the room:* one mic, with voices told apart by diarization.
- **Rubric library.** Save rubrics, and get the closest saved rubric suggested for a new job description (Voyage embeddings + pgvector).
- **Calibration (RAG).** Approve a scorecard, correcting scores if needed, and it becomes an anchor. Future evaluations with that rubric retrieve the three nearest approved examples, so Claude scores the way your team does.
- **Fairness guardrail.** The prompt tells Claude to ignore name, accent, age, gender and background.
- **Privacy.** Audio is never stored, and `DELETE /evaluations/{id}` removes a transcript for data-deletion requests.
- **Measured, not guessed.** An eval harness checks agreement with human labels (exact match, within ±1, mean error, verdict) before a prompt or model change ships.
- **Near-zero hosting cost.** It scales to zero on Azure Container Apps and uses free tiers for Speech, Postgres (Neon) and the image registry (ghcr.io).

## How it looks

<table>
  <tr>
    <td align="center" valign="top" width="50%"><img src="docs/role.png" alt="Role card with a Logistics Data Analyst job description and a rubric of competencies, one per line" width="400"><br><sub><b>Role</b>: a job description and one competency per line</sub></td>
    <td align="center" valign="top" width="50%"><img src="docs/scorecard.png" alt="Scorecard headed 'Strong yes' with a summary, then each competency with a 1–5 bar, quoted evidence and reasoning, followed by strengths, concerns, red flags and follow-up questions" width="400"><br><sub><b>Scorecard</b>: score, evidence and reasoning per competency</sub></td>
  </tr>
</table>

## How it works

```mermaid
flowchart LR
    U["Browser UI<br/>plain HTML/JS"] -- "JSON · X-API-Key" --> G["FastAPI<br/>auth + rate limit"]
    U -. "live audio, 10-min token" .-> SP["Azure AI Speech"]
    G --> E["Scoring engine<br/>Claude structured output"]
    G --> L["Live sessions<br/>re-score per answer"]
    G --> T["Transcriptions<br/>background jobs"]
    T --> SP
    T --> SEG["Segmenter<br/>utterances → Q&A"]
    L --> SEG
    L --> E
    L -- "SSE scorecards" --> U
    E --> C["Anthropic API<br/>claude-opus-5-5"]
    SEG --> C
    E <-- "nearest approved<br/>evaluations" --> DB[("Postgres + pgvector")]
    G --> V["Voyage AI<br/>embeddings"]
    V --> DB
```

Every input ends up as the same thing: a job description, a rubric and a list of Q&A pairs. The scorer sees only that.

The prompt is built so that re-scoring a live interview stays cheap:

| Layer | Contents | Cached |
|---|---|---|
| System | What the 1–5 scale means, evidence-only rules, the fairness guardrail | ✅ |
| Role | `<job_description>` and `<rubric>` | ✅ |
| Calibration | Up to 3 approved past candidates: transcript plus human scores only, never the model's reasoning | ✅ |
| Transcript | Q1/A1 … Qn/An, then "Evaluate the candidate." | — |

In a live session, at most one Claude call is in flight. Answers that arrive while it runs are picked up by its next pass, so scorecards always arrive in order. `/end` reuses the latest scorecard if it already covers every answer.

## Getting started

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). Only an Anthropic API key is required; every other feature switches on when you configure it.

```bash
git clone https://github.com/tarekFerdous/interview-evaluator.git
cd interview-evaluator
uv sync
cp .env.example .env          # add your ANTHROPIC_API_KEY
uv run interview-evaluator    # http://127.0.0.1:8000 → the web UI; API docs at /docs
```

Click **Load example** in the UI, or paste the role and transcripts from [`examples/logistics_analyst.txt`](examples/logistics_analyst.txt). It has one transcript written to score well (it scored `strong_yes` in testing) and one written to score poorly.

### With Docker

```bash
docker compose up --build     # app on http://localhost:8000 + Postgres/pgvector (migrations run at startup)
PORT=8123 docker compose up   # different port
```

The image is a multi-stage uv build (installs from `uv.lock`), runs as a non-root user and binds `0.0.0.0:$PORT` (default 8000). The app fails fast at startup if `DATABASE_URL` is set but the database is unreachable.

### Configuration

| Setting | Turns on | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Scoring and segmentation | **Required** |
| `SERVICE_API_KEY` | API-key auth (`X-API-Key`) | Auth is off, with a warning at startup (local only) |
| `DATABASE_URL` | Stored evaluations, approve and calibrate, saved rubrics | Evaluations work but aren't stored |
| `VOYAGE_API_KEY` | Rubric library, suggestions, calibration retrieval | Those endpoints answer 503 |
| `AZURE_SPEECH_KEY` + `AZURE_SPEECH_REGION` | Recording upload and the live microphone | Typed and pasted transcripts still work |
| `AZURE_SPEECH_ENDPOINT` | A custom Speech endpoint | Derived from the region |
| `RATE_LIMIT_PER_MIN` | Requests per minute per client IP | Defaults to 10 |

## Live microphone: two audio setups

- **Remote call** (default, Chrome/Edge only). Your microphone is the *Interviewer* and the call's audio is the *Candidate*. When asked, pick the call's browser tab (Teams, Meet or Zoom web) and tick **Share tab audio**. For a desktop meeting app on Windows, share your entire screen with system audio. Each side is transcribed separately, so nobody's voice has to be told apart. **Use headphones**, or your mic picks up the candidate too. This runs two Azure Speech streams at once, so it uses audio hours twice as fast: the F0 free tier's ~5 h/month is ~2.5 h of interview. Stopping the share from the browser's bar pauses the microphone.
- **In the room.** One microphone hears everyone, and Azure diarization labels the voices.

An exchange (question + answer) is sent for segmentation when the interviewer speaks again after the candidate. **Send exchange now** sends what's buffered at any time, and **End interview** always sends it. If only one speaker label was detected, Claude splits questions from answers by their content. A question with no answer yet stays buffered for the next send.

## API

```bash
curl -X POST http://127.0.0.1:8000/evaluate \
  -H "Content-Type: application/json" -H "X-API-Key: $SERVICE_API_KEY" \
  -d @examples/sample_request.json
```

`POST /evaluate` returns `{"id", "evaluation", "calibration_example_ids"}`. The scorecard is under `evaluation`, and `id` is `null` when persistence is off. `POST /sessions/{id}/end` returns the same shape.

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

Everything except `/health`, `/`, `/ui` and the docs needs `X-API-Key` (when configured) and is rate limited per client IP (`RATE_LIMIT_PER_MIN`, default 10; 429 with `Retry-After`).

## Project layout

```
src/interview_evaluator/
├── api.py             # FastAPI app: /evaluate, live sessions, SSE, speech token, lifespan
├── evaluator.py       # Claude scoring: prompt layers, caching, structured output
├── evaluations.py     # Stored evaluations, approval, calibration retrieval, LLM error mapping
├── sessions.py        # In-memory live sessions, coalescing re-scorer, event fan-out
├── speech.py          # Azure transcription + tokens, Claude segmenter (utterances → Q&A)
├── transcriptions.py  # Recording upload as background jobs
├── library.py         # Rubric library endpoints
├── repos.py           # Rubric repository (plain SQL)
├── embeddings.py      # Voyage AI embeddings
├── security.py        # API-key auth, per-IP token-bucket rate limiter
├── db.py              # Connections + startup migrations (advisory lock)
├── models.py          # Pydantic request/response/LLM-output schemas
├── migrations/        # Numbered .sql files
└── static/            # Web UI: index.html, app.js, app.css (no build step)
evals/                 # Labelled cases, generator, agreement metrics, runner
tests/                 # pytest suite with fakes for Claude, Voyage and Speech
infra/main.bicep       # Azure resources
.github/workflows/     # CI/CD
examples/              # Sample requests and paste-ready role + transcripts
```

## Development

### Tests

```bash
uv run pytest
```

The tests use fakes for Claude, Voyage and Azure Speech, so they need no API keys or network. Database-backed tests run against a real Postgres and are skipped (with the reason shown in `pytest -rs`) unless `TEST_DATABASE_URL` is set. **That database is wiped by the tests.**

```bash
docker compose up -d db
TEST_DATABASE_URL=postgresql://evaluator:evaluator@localhost:5433/evaluator_test uv run pytest
```

CI runs the full suite, DB tests included, against a Postgres service container.

### Eval set: measuring score agreement

`evals/` holds labelled interview cases (an `EvaluationRequest` plus human-owned expected scores and recommendation) and a runner that measures how closely the evaluator agrees.

1. **Generate** draft cases with a different model from the grader (`claude-sonnet-5-5`):
   ```bash
   uv run python -m evals.generate                      # 5 roles × strong/mixed/weak, skips existing files
   uv run python -m evals.generate --role backend --quality weak --force
   ```
   Cases are written to `evals/cases/` with `reviewed: false`.
2. **Review** each case: read the transcript, correct `expected.scores` / `expected.recommendation`, then set `"reviewed": true`. Only reviewed cases count.
3. **Run** the real evaluator over them:
   ```bash
   uv run python -m evals.run                           # reviewed cases only
   uv run python -m evals.run --filter 'backend-*'      # subset by id glob (repeatable)
   uv run python -m evals.run --include-unreviewed --threshold 85
   ```
   It prints exact-match %, within-±1 %, mean absolute error, recommendation match %, every disagreement larger than 1, and PASS/FAIL (within-±1 ≥ threshold; exits non-zero on FAIL). Broken case files and per-case LLM errors are reported without stopping the run. Each run is saved to `evals/results/<timestamp>.json` (git-ignored) with the model and a system-prompt hash.

**Baseline (provisional):** over the 2 seed cases plus the 15 generated drafts (labels not yet human-reviewed), `claude-opus-5-5` at medium effort scored 75.3% exact, 100% within ±1, MAE 0.25, 94.1% recommendation agreement: PASS. The run after adding prompt caching gave identical numbers. Re-baseline once the drafts are reviewed.

## Deploying to Azure Container Apps

The deployment is sized for **near-zero monthly cost**. `infra/main.bicep` creates only Log Analytics (30-day retention), a Container Apps environment, the Container App (external ingress, `/health` probes, **scales to zero** with at most 1 replica) and an Azure AI Speech resource (free F0 tier by default, or none if you reuse an existing one, see below). The image comes from a **public ghcr.io package** (anonymous pull, no registry to pay for) and the database is external: a free [Neon](https://neon.tech) Postgres works (it supports pgvector). Secrets (`ANTHROPIC_API_KEY`, `SERVICE_API_KEY`, `DATABASE_URL`, the Speech key, optional `VOYAGE_API_KEY`) are Container App secrets, never image or repo literals; `DATABASE_URL` and `VOYAGE_API_KEY` are optional (the matching features stay off without them).

Scaling to zero means the first request after an idle period waits for a cold start, and the in-memory rate limits, live sessions and transcription jobs reset when the app scales down. That is acceptable for a demo; move them to Postgres/Redis for real use.

`.github/workflows/ci-cd.yml` runs the tests on every PR and push; on `main` it signs in to Azure with **OIDC** (no stored Azure secret), pushes `ghcr.io/<owner>/interview-evaluator:<commit sha>` with `GITHUB_TOKEN`, checks the image can be pulled anonymously, deploys the Bicep once with that image and fails unless `/health` answers (20 tries, 15 s apart, to allow for a cold start).

| Resource | Where | Expected cost |
|---|---|---|
| Container App (0–1 replica) + environment | Azure | Covered by the monthly free grant at demo traffic |
| Speech F0 | Azure | Free, ~5 audio hours a month (one F0 per subscription) |
| Log Analytics | Azure | First 5 GB a month free |
| Postgres + pgvector | Neon | Free tier |
| Container image | ghcr.io | Free for public packages |
| Claude, Voyage | Per call | The only real variable cost (Voyage has a 200M-token free allowance) |

### One-time bootstrap

```bash
RG=interview-evaluator; LOCATION=westeurope; REPO=<owner>/<repo>
az group create -n $RG -l $LOCATION

# App registration + service principal that GitHub Actions signs in as
APP_ID=$(az ad app create --display-name interview-evaluator-ci --query appId -o tsv)
az ad sp create --id $APP_ID
az role assignment create --assignee $APP_ID --role Contributor \
  --scope $(az group show -n $RG --query id -o tsv)   # no role assignments are created, so Contributor is enough

# Federated credential for the `production` environment of this repo
az ad app federated-credential create --id $APP_ID --parameters '{
  "name": "github-production",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:'$REPO':environment:production",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

In GitHub, create the `production` environment and set:

- **Variables:** `AZURE_CLIENT_ID` (= `$APP_ID`), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP` (= `$RG`)
- **Secrets:** `ANTHROPIC_API_KEY`, `SERVICE_API_KEY`, `DATABASE_URL` (your Neon connection string, e.g. `postgresql://…/neondb?sslmode=require`), and optionally `VOYAGE_API_KEY`
- **Speech (optional):** by default the Bicep creates a free **F0** Speech resource. Azure allows only **one F0 Speech resource per subscription**, so if you already have one (e.g. for local development), reuse it instead: set the secret `AZURE_SPEECH_KEY` and the variable `AZURE_SPEECH_REGION`, and no Speech resource is created. F0 may not support **fast transcription** (recording uploads); if uploads fail, set the variable `AZURE_SPEECH_SKU=S0` (passed as `speechSku`; pay per audio hour) or point `AZURE_SPEECH_KEY` at an S0 resource.

Push to `main` to deploy. **The first run stops at "Check the image is publicly pullable"**: a new ghcr package is private. Open the package (repo page → Packages → `interview-evaluator` → Package settings), change its visibility to **Public**, then re-run the job. The app URL is printed by the "Deploy Container App" step.

To be told before you spend anything, add a Cost Management **budget** (e.g. $5 a month with email alerts) on the subscription or resource group.

## Design decisions

- **Structured outputs:** the Pydantic `Evaluation` model is the LLM's output schema, so scorecards are always schema-valid. Segmentation of transcripts uses the same approach into the existing `QA` model.
- **Evidence-grounded scoring:** the system prompt requires quoted evidence per score and a score of 1 when there is no evidence.
- **Fairness guardrail:** the prompt tells the model to ignore attributes unrelated to job performance.
- **Prompt caching:** the system prompt, the job description + rubric prefix and any calibration examples are cached; the transcript goes last. Live sessions re-score the whole transcript each turn but re-read the prefix from cache. Calibration examples for a session are retrieved once at start so the prefix stays stable.
- **Calibration RAG:** with a saved rubric, up to 3 approved evaluations of that rubric, nearest by cosine similarity, are added as scoring anchors: transcript plus the human-approved scores only, never the model's reasoning. Without examples the prompt is identical to the uncalibrated one.
- **Failure handling:** refusals → 422 (after Anthropic's server-side fallback model); rate limits, auth, API and connection errors map to distinct statuses, also as SSE `error` events.
- **At most one replica, scale to zero:** rate limits, live sessions and transcription jobs are in memory, so the app runs on at most one replica, and they reset when it scales to zero (fine for a demo). Move them to Postgres/Redis before scaling out or keeping state.
- **Privacy:** audio is never stored; transcripts are stored only with evaluations (when persistence is on) and can be deleted via `DELETE /evaluations/{id}`.

## Roadmap

- **AI interviewer.** Give the app a voice (Azure neural TTS) and a stylized face lip-synced to TTS visemes, and let Claude pick each next question from the scorecard's weakest competencies. Most of the pipeline (speech in, Q&A segmentation, live re-scoring, `follow_up_questions`) already exists.
- **Shared state** in Postgres or Redis, so sessions survive a scale-to-zero and the app can run more than one replica.
- **Human-reviewed eval labels**, then a firm baseline in place of the provisional one.

## Known limitations

- The scorecard is advisory. A person should make the hiring decision, and automated interview scoring is regulated in some places (for example New York City Local Law 144 and the EU AI Act).
- Live sessions, transcription jobs and rate limits are lost when the container restarts or scales to zero.
- Remote-call capture needs a Chromium browser (Chrome or Edge) for tab audio.
- Speech F0 may not support fast transcription, so recording uploads can need the S0 tier.
- Transcription is English (`en-US`) only.

## License

[MIT](LICENSE)
