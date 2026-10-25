"""Labelled case format: an API request plus the human-owned expected scores."""

from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from interview_evaluator.models import EvaluationRequest, Recommendation

CASES_DIR = Path(__file__).parent / "cases"


class Expected(BaseModel):
    scores: dict[str, Annotated[int, Field(ge=1, le=5)]] = Field(
        description="Competency name (exact rubric text) -> 1-5"
    )
    recommendation: Recommendation


class Case(BaseModel):
    id: str
    reviewed: bool = False
    notes: str = ""
    request: EvaluationRequest
    expected: Expected

    @model_validator(mode="after")
    def labels_match_rubric(self) -> Self:
        if set(self.expected.scores) != set(self.request.rubric):
            raise ValueError("expected.scores keys must equal request.rubric exactly")
        return self


class BrokenCase(BaseModel):
    path: str
    error: str


def load_case(path: Path) -> Case:
    return Case.model_validate_json(path.read_text(encoding="utf-8"))


def load_cases(directory: Path = CASES_DIR) -> tuple[list[Case], list[BrokenCase]]:
    """Loads every *.json case in a directory; invalid files are returned as broken, not raised."""
    cases, broken = [], []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(load_case(path))
        except ValidationError as e:  # also raised for malformed JSON
            error = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            broken.append(BrokenCase(path=path.name, error=error))
    return cases, broken
