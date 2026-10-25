"""Rubric library endpoints (needs DATABASE_URL and VOYAGE_API_KEY; 503 otherwise)."""

from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from .db import get_db
from .embeddings import Embedder, get_embedder
from .models import Rubric, RubricIn, RubricSuggestion, SuggestRequest
from .repos import RubricRepo, rubric_embedding_text

router = APIRouter(prefix="/rubrics", tags=["rubrics"])


def get_rubric_repo(conn: psycopg.Connection = Depends(get_db)) -> RubricRepo:
    return RubricRepo(conn)


def _duplicate(rubric: RubricIn) -> HTTPException:
    return HTTPException(409, f"A rubric named {rubric.name!r} already exists")


@router.post("", status_code=201)
def create_rubric(
    rubric: RubricIn,
    repo: RubricRepo = Depends(get_rubric_repo),
    embedder: Embedder = Depends(get_embedder),
) -> Rubric:
    [embedding] = embedder.embed([rubric_embedding_text(rubric)])
    try:
        return repo.create(rubric, embedding)
    except psycopg.errors.UniqueViolation:
        raise _duplicate(rubric) from None


@router.get("")
def list_rubrics(repo: RubricRepo = Depends(get_rubric_repo)) -> list[Rubric]:
    return repo.all()


@router.post("/suggest")
def suggest_rubrics(
    body: SuggestRequest,
    repo: RubricRepo = Depends(get_rubric_repo),
    embedder: Embedder = Depends(get_embedder),
) -> list[RubricSuggestion]:
    [embedding] = embedder.embed([body.job_description], input_type="query")
    return [RubricSuggestion(rubric=r, similarity=s) for r, s in repo.nearest(embedding, body.limit)]


@router.get("/{rubric_id}")
def get_rubric(rubric_id: UUID, repo: RubricRepo = Depends(get_rubric_repo)) -> Rubric:
    if (rubric := repo.get(rubric_id)) is None:
        raise HTTPException(404, "Rubric not found")
    return rubric


@router.put("/{rubric_id}")
def update_rubric(
    rubric_id: UUID,
    rubric: RubricIn,
    repo: RubricRepo = Depends(get_rubric_repo),
    embedder: Embedder = Depends(get_embedder),
) -> Rubric:
    [embedding] = embedder.embed([rubric_embedding_text(rubric)])
    try:
        updated = repo.update(rubric_id, rubric, embedding)
    except psycopg.errors.UniqueViolation:
        raise _duplicate(rubric) from None
    if updated is None:
        raise HTTPException(404, "Rubric not found")
    return updated


@router.delete("/{rubric_id}", status_code=204)
def delete_rubric(rubric_id: UUID, repo: RubricRepo = Depends(get_rubric_repo)) -> Response:
    if not repo.delete(rubric_id):
        raise HTTPException(404, "Rubric not found")
    return Response(status_code=204)


def resolve_rubric(rubric: list[str] | None, rubric_id: UUID | None, conn: psycopg.Connection | None) -> list[str]:
    """Competencies for a request that sent either an inline rubric or a saved rubric's id."""
    if rubric is not None:
        return rubric
    if conn is None:
        raise HTTPException(503, "rubric_id needs persistence, which is not configured (DATABASE_URL)")
    saved = RubricRepo(conn).get(rubric_id)
    if saved is None:
        raise HTTPException(404, f"Rubric {rubric_id} not found")
    return saved.competencies
