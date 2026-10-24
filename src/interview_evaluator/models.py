"""Request and response schemas. The LLM output schema doubles as the API response."""

from typing import Literal

from pydantic import BaseModel, Field


class QA(BaseModel):
    question: str
    answer: str


class EvaluationRequest(BaseModel):
    job_description: str = Field(min_length=20, max_length=20_000)
    rubric: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Competencies to score, e.g. 'Python proficiency', 'Ownership'",
    )
    transcript: list[QA] = Field(min_length=1, max_length=30)


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
    recommendation: Literal["strong_yes", "yes", "lean_no", "no"]
    summary: str
