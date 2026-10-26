"""Evaluation service: run the LLM, persist the result, and the stored-evaluation endpoints."""

import logging
from uuid import UUID

import anthropic
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pgvector import Vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .db import get_db
from .embeddings import Embedder
from .evaluator import EvaluationRefused, evaluate, match_competency
from .models import (
    ApproveRequest,
    CalibrationExample,
    Evaluation,
    EvaluationRequest,
    EvaluationResult,
    StoredEvaluation,
)

log = logging.getLogger("interview_evaluator")
MAX_CALIBRATION_EXAMPLES = 3


# ---- LLM error mapping (shared by /evaluate, session end and background scoring)


def llm_error(e: Exception) -> tuple[int, str] | None:
    """Maps an evaluation failure to (HTTP status, detail); None for unexpected errors."""
    match e:  # most specific first: RateLimitError and AuthenticationError are APIStatusErrors
        case EvaluationRefused():
            return 422, f"Model declined to evaluate: {e}"
        case anthropic.RateLimitError():
            return 429, "LLM rate limit hit, retry shortly"
        case anthropic.AuthenticationError():
            return 500, "LLM credentials are missing or invalid"
        case anthropic.APIStatusError():
            return 502, f"LLM API error ({e.status_code})"
        case anthropic.APIConnectionError():
            return 503, "Could not reach the LLM API"
    return None


def run_evaluation(
    req: EvaluationRequest,
    client: anthropic.Anthropic,
    calibration: list[CalibrationExample] = (),
) -> Evaluation:
    try:
        return evaluate(req, client, calibration)
    except Exception as e:
        if mapped := llm_error(e):
            raise HTTPException(*mapped) from e
        raise


# ---- repository


class EvaluationRepo:
    COLUMNS = (
        "id, rubric_id, request, result AS evaluation, calibration_example_ids, approved,"
        " approved_scores, approved_recommendation, created_at, approved_at"
    )

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def create(
        self,
        req: EvaluationRequest,
        evaluation: Evaluation,
        rubric_id: UUID | None,
        embedding: list[float] | None,
        calibration_example_ids: list[UUID],
    ) -> UUID:
        row = self.conn.execute(
            "INSERT INTO evaluations (rubric_id, request, result, calibration_example_ids, embedding)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                rubric_id,
                Jsonb(req.model_dump(mode="json")),
                Jsonb(evaluation.model_dump(mode="json")),
                Jsonb([str(i) for i in calibration_example_ids]),
                Vector(embedding) if embedding is not None else None,
            ),
        ).fetchone()
        return row[0]

    def get(self, evaluation_id: UUID) -> StoredEvaluation | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                f"SELECT {self.COLUMNS} FROM evaluations WHERE id = %s", (evaluation_id,)
            ).fetchone()
        return StoredEvaluation(**row) if row else None

    def delete(self, evaluation_id: UUID) -> bool:
        return self.conn.execute("DELETE FROM evaluations WHERE id = %s", (evaluation_id,)).rowcount > 0

    def approve(self, evaluation_id: UUID, scores: dict[str, int], recommendation: str) -> None:
        self.conn.execute(
            "UPDATE evaluations SET approved = true, approved_scores = %s,"
            " approved_recommendation = %s, approved_at = now() WHERE id = %s",
            (Jsonb(scores), recommendation, evaluation_id),
        )

    def nearest_approved(
        self, rubric_id: UUID, embedding: list[float], limit: int = MAX_CALIBRATION_EXAMPLES
    ) -> list[CalibrationExample]:
        """Approved evaluations of the same rubric, nearest by cosine similarity first."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT id, request -> 'transcript' AS transcript, approved_scores AS scores,"
                " approved_recommendation AS recommendation FROM evaluations"
                " WHERE approved AND rubric_id = %s AND embedding IS NOT NULL"
                " ORDER BY embedding <=> %s LIMIT %s",
                (rubric_id, Vector(embedding), limit),
            ).fetchall()
        return [CalibrationExample(**r) for r in rows]

    def unapprove(self, evaluation_id: UUID) -> None:
        self.conn.execute(
            "UPDATE evaluations SET approved = false, approved_scores = NULL,"
            " approved_recommendation = NULL, approved_at = NULL WHERE id = %s",
            (evaluation_id,),
        )


def embedding_text(req: EvaluationRequest) -> str:
    transcript = "\n".join(f"Q: {qa.question}\nA: {qa.answer}" for qa in req.transcript)
    return f"{req.job_description}\n\n{transcript}"


def embed_or_none(embedder: Embedder | None, text: str, input_type: str = "document") -> list[float] | None:
    """Embeddings are best-effort for storage: an outage must not fail the evaluation."""
    if embedder is None:
        return None
    try:
        return embedder.embed([text], input_type=input_type)[0]
    except Exception as e:  # the Voyage client raises several unrelated error types
        log.warning("Embedding failed, storing without it: %s", e)
        return None


def retrieve_calibration(
    conn: psycopg.Connection | None, rubric_id: UUID | None, embedding: list[float] | None
) -> list[CalibrationExample]:
    """Up to 3 approved same-rubric examples; none without a saved rubric, a database or an
    embedding, in which case scoring works exactly as without calibration."""
    if conn is None or rubric_id is None or embedding is None:
        return []
    return EvaluationRepo(conn).nearest_approved(rubric_id, embedding)


def store(
    conn: psycopg.Connection | None,
    req: EvaluationRequest,
    evaluation: Evaluation,
    rubric_id: UUID | None,
    embedding: list[float] | None,
    calibration: list[CalibrationExample] = (),
) -> EvaluationResult:
    """Persists the evaluation when persistence is on; returns the API response either way."""
    ids = [ex.id for ex in calibration]
    if conn is None:
        return EvaluationResult(id=None, evaluation=evaluation, calibration_example_ids=ids)
    new_id = EvaluationRepo(conn).create(req, evaluation, rubric_id, embedding, ids)
    return EvaluationResult(id=new_id, evaluation=evaluation, calibration_example_ids=ids)


# ---- endpoints

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def get_evaluation_repo(conn: psycopg.Connection = Depends(get_db)) -> EvaluationRepo:
    return EvaluationRepo(conn)


def _not_found() -> HTTPException:
    return HTTPException(404, "Evaluation not found")


@router.get("/{evaluation_id}")
def get_evaluation(evaluation_id: UUID, repo: EvaluationRepo = Depends(get_evaluation_repo)) -> StoredEvaluation:
    if (stored := repo.get(evaluation_id)) is None:
        raise _not_found()
    return stored


@router.delete("/{evaluation_id}", status_code=204)
def delete_evaluation(evaluation_id: UUID, repo: EvaluationRepo = Depends(get_evaluation_repo)) -> Response:
    """Removes the evaluation and its transcript (data-deletion requests); it stops being a
    calibration example immediately."""
    if not repo.delete(evaluation_id):
        raise _not_found()
    return Response(status_code=204)


@router.post("/{evaluation_id}/approve")
def approve_evaluation(
    evaluation_id: UUID,
    body: ApproveRequest | None = None,
    repo: EvaluationRepo = Depends(get_evaluation_repo),
) -> StoredEvaluation:
    """Marks the evaluation as a calibration example. Approved scores start from the model's and
    take any corrections; the model's own result is never modified."""
    body = body or ApproveRequest()
    if (stored := repo.get(evaluation_id)) is None:
        raise _not_found()
    rubric = stored.request.rubric
    # Keyed by exact rubric text; older evaluations may carry the model's shortened names.
    named = {name: match_competency(name, rubric) for name in body.scores or {}}
    if unknown := sorted(name for name, c in named.items() if c is None):
        raise HTTPException(422, f"Not competencies of this evaluation's rubric: {unknown}")
    corrections = {named[name]: score for name, score in (body.scores or {}).items()}
    model_scores = {match_competency(s.competency, rubric) or s.competency: s.score
                    for s in stored.evaluation.scores}
    scores = model_scores | corrections
    recommendation = body.recommendation or stored.evaluation.recommendation
    repo.approve(evaluation_id, scores, recommendation)
    return repo.get(evaluation_id)


@router.post("/{evaluation_id}/unapprove")
def unapprove_evaluation(
    evaluation_id: UUID, repo: EvaluationRepo = Depends(get_evaluation_repo)
) -> StoredEvaluation:
    if repo.get(evaluation_id) is None:
        raise _not_found()
    repo.unapprove(evaluation_id)
    return repo.get(evaluation_id)
