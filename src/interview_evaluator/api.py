import asyncio
import json
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import anthropic
import psycopg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.sse import EventSourceResponse, format_sse_event
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import evaluations, library, transcriptions
from .db import get_database_url, get_optional_db, migrate
from .embeddings import Embedder, get_optional_embedder, get_voyage_api_key
from .evaluations import (
    embed_or_none,
    embedding_text,
    llm_error,
    retrieve_calibration,
    run_evaluation,
    store,
)
from .evaluator import evaluate, get_client
from .library import resolve_rubric
from .models import (
    MAX_TRANSCRIPT,
    QA,
    EvaluateBody,
    Evaluation,
    EvaluationRequest,
    EvaluationResult,
    InterviewContext,
    SessionStart,
)
from .security import (
    get_service_api_key,
    rate_limit,
    require_api_key,
    require_api_key_header_or_query,
)
from .speech import TokenIssuer, Utterance, get_token_issuer, segment, single_speaker
from .sessions import (
    Event,
    InMemorySessionStore,
    Runner,
    Session,
    SessionStore,
    request_scoring,
    run_in_thread,
    wait_until_idle,
)

load_dotenv()
log = logging.getLogger("interview_evaluator")
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if get_service_api_key() is None:
        log.warning("SERVICE_API_KEY is not set: API-key auth is DISABLED (fine for local dev only)")
    if (url := get_database_url()) is None:
        log.warning("DATABASE_URL is not set: evaluations are not stored, rubric library is off")
    else:
        try:  # fail fast: a misconfigured deploy must not pass the health check
            applied = migrate(url)
        except psycopg.Error as e:
            raise RuntimeError(f"Database unreachable or migration failed at startup: {e}") from e
        log.info("Database ready; migrations applied: %s", applied or "none (up to date)")
        if get_voyage_api_key() is None:
            log.warning("VOYAGE_API_KEY is not set: rubric library and calibration retrieval are off")
    yield


app = FastAPI(title="Interview Evaluator", lifespan=lifespan)
# Everything except /health, / and the docs requires X-API-Key; included into `app` at the bottom.
api = APIRouter(dependencies=[Depends(require_api_key)])


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


limited = [Depends(rate_limit)]


@api.post("/evaluate", dependencies=limited)
def evaluate_candidate(
    body: EvaluateBody,
    client: anthropic.Anthropic = Depends(get_client),
    conn: psycopg.Connection | None = Depends(get_optional_db),
    embedder: Embedder | None = Depends(get_optional_embedder),
) -> EvaluationResult:
    """Scores the transcript and stores the result (when persistence is on). The scorecard is
    under `evaluation`. With a saved rubric, the nearest approved past evaluations for that rubric
    are used as calibration anchors."""
    rubric = resolve_rubric(body.rubric, body.rubric_id, conn)
    req = EvaluationRequest(job_description=body.job_description, rubric=rubric, transcript=body.transcript)
    embedding = embed_or_none(embedder, embedding_text(req)) if conn else None
    calibration = retrieve_calibration(conn, body.rubric_id, embedding)
    evaluation = run_evaluation(req, client, calibration)
    return store(conn, req, evaluation, body.rubric_id, embedding, calibration)


# ---- live sessions


@lru_cache
def get_session_store() -> SessionStore:
    return InMemorySessionStore()


def get_runner() -> Runner:
    return run_in_thread


def live_session(session_id: str, sessions: SessionStore = Depends(get_session_store)) -> Session:
    session = sessions.get(session_id)
    if session is None or session.ended:
        raise HTTPException(404, "Session not found or expired; start a new one")
    return session


class SessionCreated(BaseModel):
    session_id: str


class QAAccepted(BaseModel):
    qa_count: int


class SessionState(InterviewContext):
    rubric_id: UUID | None
    transcript: list[QA]
    latest_evaluation: Evaluation | None
    evaluated_qa_count: int
    scoring: bool


@api.post("/sessions", status_code=201, dependencies=limited)
def create_session(
    body: SessionStart,
    sessions: SessionStore = Depends(get_session_store),
    conn: psycopg.Connection | None = Depends(get_optional_db),
    embedder: Embedder | None = Depends(get_optional_embedder),
) -> SessionCreated:
    rubric = resolve_rubric(body.rubric, body.rubric_id, conn)
    context = InterviewContext(job_description=body.job_description, rubric=rubric)
    session = sessions.create(context, rubric_id=body.rubric_id)
    if body.rubric_id is not None:
        # Retrieved once, by job description, so the cached prompt prefix stays the same for
        # every re-score in this session.
        embedding = embed_or_none(embedder, body.job_description, input_type="query")
        session.calibration = retrieve_calibration(conn, body.rubric_id, embedding)
    return SessionCreated(session_id=session.id)


@api.post("/sessions/{session_id}/qa", status_code=202, dependencies=limited)
def add_qa(
    qa: QA,
    session: Session = Depends(live_session),
    client: anthropic.Anthropic = Depends(get_client),
    run: Runner = Depends(get_runner),
) -> QAAccepted:
    with session.lock:
        if len(session.transcript) >= MAX_TRANSCRIPT:
            raise HTTPException(422, f"A session holds at most {MAX_TRANSCRIPT} Q&As")
        session.transcript.append(qa)
        qa_count = len(session.transcript)
    request_scoring(session, lambda req: evaluate(req, client, session.calibration), llm_error, run)
    return QAAccepted(qa_count=qa_count)


@api.put("/sessions/{session_id}/qa/last", dependencies=limited)
def replace_last_qa(
    qa: QA,
    session: Session = Depends(live_session),
    client: anthropic.Anthropic = Depends(get_client),
    run: Runner = Depends(get_runner),
) -> QAAccepted:
    """Fixes the most recent Q&A (e.g. a mis-split live exchange) and re-scores."""
    with session.lock:
        if not session.transcript:
            raise HTTPException(422, "There is no Q&A to replace yet")
        session.transcript[-1] = qa
        qa_count = len(session.transcript)
        # The last scoring pass (if it covered this Q&A) is stale: let the scorer run again.
        session.attempted_qa_count = min(session.attempted_qa_count, qa_count - 1)
        session.evaluated_qa_count = min(session.evaluated_qa_count, qa_count - 1)
    request_scoring(session, lambda req: evaluate(req, client, session.calibration), llm_error, run)
    return QAAccepted(qa_count=qa_count)


class SegmentRequest(BaseModel):
    utterances: list[Utterance] = Field(min_length=1, max_length=500)
    interviewer_speaker: str | None = Field(
        None, max_length=50, description="Label the client already knows is the interviewer"
    )


class SegmentResult(BaseModel):
    qa: list[QA]
    qa_count: int
    interviewer_speaker: str | None
    unanswered_from: int | None = Field(
        description="Index of the first sent utterance not used yet (a trailing, unanswered "
        "exchange); the client keeps these for its next send"
    )


@api.post("/sessions/{session_id}/segment", dependencies=limited)
def segment_into_session(
    body: SegmentRequest,
    session: Session = Depends(live_session),
    client: anthropic.Anthropic = Depends(get_client),
    run: Runner = Depends(get_runner),
) -> SegmentResult:
    """Live mic: turns a chunk of diarized utterances (one or more finished exchanges) into Q&A
    pairs, appends them to the session and triggers re-scoring."""
    try:
        # The client's label wins: remote mode knows its labels even if room mode was used first.
        seg = segment(body.utterances, client, body.interviewer_speaker or session.interviewer_speaker)
    except Exception as e:
        if mapped := llm_error(e):
            raise HTTPException(*mapped) from e
        raise
    unanswered = seg.unanswered_from
    if unanswered is not None and not 0 <= unanswered < len(body.utterances):
        unanswered = None
    with session.lock:
        if session.interviewer_speaker is None:
            # A single-label exchange says nothing reliable about who is asking.
            if body.interviewer_speaker or not single_speaker(body.utterances):
                session.interviewer_speaker = body.interviewer_speaker or seg.interviewer_speaker
        room = MAX_TRANSCRIPT - len(session.transcript)
        if len(seg.qa) > room:
            raise HTTPException(422, f"A session holds at most {MAX_TRANSCRIPT} Q&As")
        session.transcript.extend(seg.qa)
        qa_count = len(session.transcript)
        interviewer = session.interviewer_speaker
    if seg.qa:
        request_scoring(session, lambda req: evaluate(req, client, session.calibration), llm_error, run)
    return SegmentResult(qa=seg.qa, qa_count=qa_count, interviewer_speaker=interviewer,
                         unanswered_from=unanswered)


class SpeechToken(BaseModel):
    token: str
    region: str
    expires_in_seconds: int = 540  # Azure tokens last 10 minutes; refresh a little early


@api.post("/speech/token", dependencies=limited)
def speech_token(issuer: TokenIssuer = Depends(get_token_issuer)) -> SpeechToken:
    """Short-lived token for the browser Speech SDK. The Azure key never leaves the server."""
    return SpeechToken(token=issuer.issue(), region=issuer.region)


@api.get("/sessions/{session_id}", dependencies=limited)
def get_session(session: Session = Depends(live_session)) -> SessionState:
    with session.lock:
        return SessionState(
            **session.context.model_dump(),
            rubric_id=session.rubric_id,
            transcript=list(session.transcript),
            latest_evaluation=session.latest_evaluation,
            evaluated_qa_count=session.evaluated_qa_count,
            scoring=session.scoring,
        )


@api.post("/sessions/{session_id}/end", dependencies=limited)
def end_session(
    session: Session = Depends(live_session),
    sessions: SessionStore = Depends(get_session_store),
    client: anthropic.Anthropic = Depends(get_client),
    conn: psycopg.Connection | None = Depends(get_optional_db),
    embedder: Embedder | None = Depends(get_optional_embedder),
) -> EvaluationResult:
    """Final evaluation of the whole transcript; stored like a one-shot evaluation."""
    with session.lock:
        if not session.transcript:
            raise HTTPException(422, "Add at least one Q&A before ending the session")
        session.ended = True  # blocks new Q&As; the in-flight scorer stops after its current run
    wait_until_idle(session, timeout=300)
    with session.lock:
        up_to_date = session.evaluated_qa_count == len(session.transcript)
        final = session.latest_evaluation if up_to_date else None
    if final is None:
        try:
            final = run_evaluation(session.request(), client, session.calibration)
        except Exception:
            with session.lock:
                session.ended = False  # keep the session usable so the client can retry
            raise
    req = session.request()
    embedding = embed_or_none(embedder, embedding_text(req)) if conn else None
    result = store(conn, req, final, session.rubric_id, embedding, session.calibration)
    session.publish("ended", {"qa_count": len(session.transcript), **result.model_dump(mode="json")})
    sessions.delete(session.id)
    return result


SSE_KEEPALIVE_SECONDS = 15


# On `app`, not `api`: it authenticates with header OR ?key= (EventSource can't set headers).
@app.get(
    "/sessions/{session_id}/events",
    dependencies=[Depends(require_api_key_header_or_query), *limited],
)
async def session_events(
    session: Session = Depends(live_session),
    last_event_id: int | None = Header(None),
) -> EventSourceResponse:
    """Server-Sent Events: scoring {qa_count}, scorecard {qa_count, evaluation},
    error {qa_count, status, detail}, ended {qa_count, evaluation}. Recent events are replayed on
    connect (after Last-Event-ID when the browser reconnects)."""
    sub, history = session.subscribe()  # before the response starts, so no event is missed

    def encode(event: Event) -> bytes:
        return format_sse_event(data_str=json.dumps(event.data), event=event.type, id=str(event.id))

    async def stream():
        try:
            for event in history:
                if last_event_id is None or event.id > last_event_id:
                    yield encode(event)
                    if event.type == "ended":
                        return
            while True:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), SSE_KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield format_sse_event(comment="keepalive")
                    continue
                yield encode(event)
                if event.type == "ended":
                    return
        finally:
            session.unsubscribe(sub)

    return EventSourceResponse(stream(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


api.include_router(library.router, dependencies=limited)
api.include_router(evaluations.router, dependencies=limited)
api.include_router(transcriptions.router, dependencies=limited)
app.include_router(api)
# The UI is public; only its API calls need the key.
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
