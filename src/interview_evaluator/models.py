"""Request and response schemas. The LLM output schema doubles as the API response."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

Recommendation = Literal["strong_yes", "yes", "lean_no", "no"]


class QA(BaseModel):
    question: str
    answer: str


MAX_TRANSCRIPT = 30


class InterviewContext(BaseModel):
    job_description: str = Field(min_length=20, max_length=20_000)
    rubric: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Competencies to score, e.g. 'Python proficiency', 'Ownership'",
    )


class EvaluationRequest(InterviewContext):
    """A fully resolved request: what the evaluator (and the eval set) consumes."""

    transcript: list[QA] = Field(min_length=1, max_length=MAX_TRANSCRIPT)


# ---- client-facing request bodies: the rubric is given inline or by saved-rubric id


class SessionStart(BaseModel):
    job_description: str = Field(min_length=20, max_length=20_000)
    rubric: list[str] | None = Field(
        None,
        min_length=1,
        max_length=10,
        description="Competencies to score; send this or rubric_id, not both",
    )
    rubric_id: UUID | None = Field(None, description="A saved rubric's id; send this or rubric")

    @model_validator(mode="after")
    def exactly_one_rubric_source(self) -> Self:
        if (self.rubric is None) == (self.rubric_id is None):
            raise ValueError("Send exactly one of `rubric` or `rubric_id`")
        return self


class EvaluateBody(SessionStart):
    transcript: list[QA] = Field(min_length=1, max_length=MAX_TRANSCRIPT)


# ---- rubric library


class RubricIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role_description: str = Field(min_length=1, max_length=20_000)
    competencies: list[str] = Field(min_length=1, max_length=10)


class Rubric(RubricIn):
    id: UUID
    created_at: datetime
    updated_at: datetime


class SuggestRequest(BaseModel):
    job_description: str = Field(min_length=20, max_length=20_000)
    limit: int = Field(3, ge=1, le=10)


class RubricSuggestion(BaseModel):
    rubric: Rubric
    similarity: float = Field(description="Cosine similarity to the job description, -1 to 1")


class CompetencyScore(BaseModel):
    competency: str
    score: int = Field(description="1 (no evidence) to 5 (strong, specific evidence)")
    evidence: str = Field(description="Short quote or paraphrase from the candidate's answers")
    reasoning: str


class Evaluation(BaseModel):
    scores: list[CompetencyScore]
    strengths: list[str]
    concerns: list[str]
    red_flags: list[str]
    follow_up_questions: list[str]
    recommendation: Recommendation
    summary: str


# ---- stored evaluations


class EvaluationResult(BaseModel):
    """Response of POST /evaluate and POST /sessions/{id}/end."""

    id: UUID | None = Field(description="Stored evaluation id; null when persistence is off")
    evaluation: Evaluation
    calibration_example_ids: list[UUID] = Field(
        default_factory=list, description="Approved past evaluations used as scoring anchors"
    )


class CalibrationExample(BaseModel):
    """An approved past evaluation as the prompt sees it: transcript and human judgment only."""

    id: UUID
    transcript: list[QA]
    scores: dict[str, int]
    recommendation: Recommendation


class ApproveRequest(BaseModel):
    """Human approval, optionally correcting the model. Omitted competencies keep the model's
    score; an omitted recommendation keeps the model's."""

    scores: dict[str, Annotated[int, Field(ge=1, le=5)]] | None = None
    recommendation: Recommendation | None = None


class StoredEvaluation(BaseModel):
    id: UUID
    rubric_id: UUID | None
    request: EvaluationRequest
    evaluation: Evaluation
    calibration_example_ids: list[UUID]
    approved: bool
    approved_scores: dict[str, int] | None
    approved_recommendation: Recommendation | None
    created_at: datetime
    approved_at: datetime | None
