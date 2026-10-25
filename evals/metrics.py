"""Pure agreement metrics between expected labels and model evaluations. No I/O."""

from pydantic import BaseModel

from interview_evaluator.models import Evaluation

from .dataset import Expected


class Disagreement(BaseModel):
    competency: str
    expected: int
    actual: int | None  # None = the model returned no score for this exact competency name


class CaseResult(BaseModel):
    case_id: str
    expected_recommendation: str
    actual_recommendation: str
    disagreements: list[Disagreement]  # misses by more than 1, plus missing competencies


class AgreementReport(BaseModel):
    cases: int
    scores: int
    exact_pct: float
    within_one_pct: float
    mae: float | None  # over competencies the model actually scored
    recommendation_pct: float
    per_case: list[CaseResult]

    def passed(self, threshold: float) -> bool:
        return self.scores > 0 and self.within_one_pct >= threshold


def _pct(n: int, total: int) -> float:
    return round(100 * n / total, 1) if total else 0.0


def agreement_report(pairs: list[tuple[str, Expected, Evaluation]]) -> AgreementReport:
    """pairs: (case_id, expected labels, model evaluation). Competencies match by exact name."""
    total = exact = within = rec_match = 0
    abs_errors: list[int] = []
    per_case = []
    for case_id, expected, actual in pairs:
        actual_scores = {s.competency: s.score for s in actual.scores}
        disagreements = []
        for competency, want in expected.scores.items():
            total += 1
            got = actual_scores.get(competency)
            if got is None:
                disagreements.append(Disagreement(competency=competency, expected=want, actual=None))
                continue
            diff = abs(got - want)
            abs_errors.append(diff)
            exact += diff == 0
            within += diff <= 1
            if diff > 1:
                disagreements.append(Disagreement(competency=competency, expected=want, actual=got))
        rec_match += expected.recommendation == actual.recommendation
        per_case.append(
            CaseResult(
                case_id=case_id,
                expected_recommendation=expected.recommendation,
                actual_recommendation=actual.recommendation,
                disagreements=disagreements,
            )
        )
    return AgreementReport(
        cases=len(pairs),
        scores=total,
        exact_pct=_pct(exact, total),
        within_one_pct=_pct(within, total),
        mae=round(sum(abs_errors) / len(abs_errors), 2) if abs_errors else None,
        recommendation_pct=_pct(rec_match, len(pairs)),
        per_case=per_case,
    )
