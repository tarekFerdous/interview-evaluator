# Interview Evaluator

An LLM-powered service that scores a candidate's interview answers against a hiring rubric.
You send a job description, rubric competencies and a Q&A transcript; it returns a
validated, structured evaluation with a 1–5 score per competency (with quoted evidence),
strengths, concerns, red flags, follow-up questions and a hiring recommendation.

**Stack:** Python 3.13, FastAPI, Pydantic, Anthropic Claude API (structured outputs), pytest, uv.

## Run it

```bash
uv sync
cp .env.example .env          # add your ANTHROPIC_API_KEY
uv run interview-evaluator    # http://127.0.0.1:8000/docs
```

```bash
curl -X POST http://127.0.0.1:8000/evaluate \
  -H "Content-Type: application/json" \
  -d @examples/sample_request.json
```

## Tests

```bash
uv run pytest
```

The tests use a fake LLM client, so they need no API key or network.

## Design decisions

- **Structured outputs:** the Pydantic `Evaluation` model is the LLM's output schema and the
  API response model, so responses are always schema-valid. No regex parsing of model text.
- **Evidence-grounded scoring:** the system prompt requires quoted evidence per score and a
  score of 1 when there is no evidence, which reduces hallucinated strengths.
- **Fairness guardrail:** the prompt tells the model to ignore attributes unrelated to job
  performance.
- **Failure handling:** model refusals return 422 (with a server-side fallback model enabled first).
  Rate limits, auth, API and connection errors each map to a distinct HTTP status.
- **Input limits:** request size is bounded (rubric ≤ 10 items, transcript ≤ 30 Q&As) to cap cost.

## Roadmap

- [ ] Eval set: 10–20 hand-labelled transcripts and a script that measures score agreement
- [ ] Simple web UI (paste a transcript, see the scorecard)
- [ ] Streaming: score the interview live, question by question
- [ ] RAG: retrieve the rubric and similar past evaluations from pgvector
- [ ] Voice: speech-to-text in, so it can evaluate a live screening call
- [ ] Docker and deployment (Azure Container Apps)
