import json
from pathlib import Path

import anthropic
import httpx
import pytest
from fakes import FakeMessages, evaluation, fake_client

from evals import run
from evals.dataset import CASES_DIR, load_cases

RUBRIC = ["Python", "Communication"]


def write_case(directory: Path, case_id: str, reviewed: bool = True, **overrides) -> None:
    case = {
        "id": case_id,
        "reviewed": reviewed,
        "notes": "",
        "request": {
            "job_description": "Backend engineer building Python services.",
            "rubric": RUBRIC,
            "transcript": [{"question": "What did you build?", "answer": "A FastAPI service."}],
        },
        "expected": {"scores": {"Python": 4, "Communication": 3}, "recommendation": "yes"},
        **overrides,
    }
    (directory / f"{case_id}.json").write_text(json.dumps(case), encoding="utf-8")


def run_with(directory: Path, outputs: list, *flags: str) -> tuple[int, FakeMessages]:
    messages = FakeMessages(parsed=outputs)
    argv = ["--cases-dir", str(directory), "--results-dir", str(directory / "results"), *flags]
    return run.main(argv, client=fake_client(messages)), messages


def saved_results(directory: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((directory / "results").glob("*.json"))]


def test_committed_seed_cases_validate():
    cases, broken = load_cases(CASES_DIR)
    assert broken == []
    assert len(cases) >= 2


def test_pass_report_and_exit_code(tmp_path, capsys):
    write_case(tmp_path, "a")
    write_case(tmp_path, "b")
    code, messages = run_with(
        tmp_path,
        [evaluation({"Python": 4, "Communication": 3}), evaluation({"Python": 5, "Communication": 3})],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert len(messages.calls) == 2
    assert "Exact match:          75.0%" in out
    assert "Within ±1:            100.0%" in out
    assert "Recommendation match: 100.0%" in out
    assert "PASS" in out


def test_fail_lists_disagreements_and_exits_nonzero(tmp_path, capsys):
    write_case(tmp_path, "a")
    code, _ = run_with(tmp_path, [evaluation({"Python": 1, "Communication": 3}, "no")])
    out = capsys.readouterr().out
    assert code == 1
    assert "a: 'Python' expected 4, got 1" in out
    assert "FAIL" in out


def test_threshold_flag(tmp_path, capsys):
    write_case(tmp_path, "a")
    outputs = lambda: [evaluation({"Python": 1, "Communication": 3})]  # 50% within ±1
    assert run_with(tmp_path, outputs())[0] == 1
    assert run_with(tmp_path, outputs(), "--threshold", "50")[0] == 0


def test_unreviewed_cases_skipped_unless_flagged(tmp_path, capsys):
    write_case(tmp_path, "done")
    write_case(tmp_path, "draft", reviewed=False)
    perfect = lambda: evaluation({"Python": 4, "Communication": 3})

    _, messages = run_with(tmp_path, [perfect()])
    assert len(messages.calls) == 1
    assert "skipping 1 unreviewed case(s): draft" in capsys.readouterr().err

    _, messages = run_with(tmp_path, [perfect(), perfect()], "--include-unreviewed")
    assert len(messages.calls) == 2


@pytest.mark.parametrize("bad", [{"Python": 4}, {"Python": 4, "Communication": 6}])
def test_case_labels_must_cover_rubric_with_valid_scores(tmp_path, bad):
    write_case(tmp_path, "bad", expected={"scores": bad, "recommendation": "yes"})
    cases, broken = load_cases(tmp_path)
    assert cases == [] and broken[0].path == "bad.json"


def test_filter_limits_run_to_matching_ids(tmp_path, capsys):
    for case_id in ("seed-a", "seed-b", "other"):
        write_case(tmp_path, case_id)
    perfect = lambda: evaluation({"Python": 4, "Communication": 3})

    _, messages = run_with(tmp_path, [perfect(), perfect()], "--filter", "seed-*")
    assert len(messages.calls) == 2

    _, messages = run_with(tmp_path, [perfect()], "--filter", "other")
    assert len(messages.calls) == 1


def test_run_saves_results_json(tmp_path, capsys):
    write_case(tmp_path, "a")
    run_with(tmp_path, [evaluation({"Python": 4, "Communication": 3})])
    [result] = saved_results(tmp_path)
    assert result["model"] == "claude-opus-5-5"
    assert len(result["prompt_hash"]) == 12
    assert result["timestamp"] and result["passed"] is True
    assert result["metrics"]["within_one_pct"] == 100.0
    assert result["metrics"]["per_case"][0]["case_id"] == "a"


def test_broken_case_is_reported_and_others_still_run(tmp_path, capsys):
    write_case(tmp_path, "good")
    (tmp_path / "broken.json").write_text('{"id": "broken"', encoding="utf-8")
    code, messages = run_with(tmp_path, [evaluation({"Python": 4, "Communication": 3})])
    assert code == 0 and len(messages.calls) == 1
    assert "BROKEN broken.json" in capsys.readouterr().out
    assert saved_results(tmp_path)[0]["broken"][0]["path"] == "broken.json"


def test_llm_error_is_recorded_and_skipped(tmp_path, capsys):
    write_case(tmp_path, "a")
    write_case(tmp_path, "b")
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    code, messages = run_with(tmp_path, [error, evaluation({"Python": 4, "Communication": 3})])
    assert code == 0 and len(messages.calls) == 2
    assert "ERROR a: APIConnectionError" in capsys.readouterr().out
    [result] = saved_results(tmp_path)
    assert result["metrics"]["cases"] == 1
    assert "a" in result["errors"]
