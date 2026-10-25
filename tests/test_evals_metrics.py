from fakes import evaluation

from evals.dataset import Expected
from evals.metrics import agreement_report

EXPECTED = Expected(scores={"Python": 4, "Communication": 2}, recommendation="yes")


def report_for(scores: dict[str, int], recommendation: str = "yes"):
    return agreement_report([("case-1", EXPECTED, evaluation(scores, recommendation))])


def test_perfect_agreement():
    r = report_for({"Python": 4, "Communication": 2})
    assert (r.exact_pct, r.within_one_pct, r.mae, r.recommendation_pct) == (100.0, 100.0, 0.0, 100.0)
    assert r.per_case[0].disagreements == []
    assert r.passed(85)


def test_off_by_one_is_within_but_not_exact():
    r = report_for({"Python": 5, "Communication": 1})
    assert (r.exact_pct, r.within_one_pct, r.mae) == (0.0, 100.0, 1.0)
    assert r.per_case[0].disagreements == []


def test_off_by_two_is_a_disagreement():
    r = report_for({"Python": 4, "Communication": 4})
    assert (r.exact_pct, r.within_one_pct, r.mae) == (50.0, 50.0, 1.0)
    [d] = r.per_case[0].disagreements
    assert (d.competency, d.expected, d.actual) == ("Communication", 2, 4)
    assert not r.passed(85)


def test_missing_competency_counts_as_mismatch():
    # "communication" differs in case only: exact-name matching treats it as missing.
    r = report_for({"Python": 4, "communication": 2})
    assert r.scores == 2
    assert (r.exact_pct, r.within_one_pct, r.mae) == (50.0, 50.0, 0.0)
    [d] = r.per_case[0].disagreements
    assert (d.competency, d.actual) == ("Communication", None)


def test_recommendation_mismatch():
    r = report_for({"Python": 4, "Communication": 2}, recommendation="no")
    assert r.recommendation_pct == 0.0
    assert r.per_case[0].actual_recommendation == "no"
    assert r.passed(85)  # PASS/FAIL is on score agreement only
