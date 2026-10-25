"""Runs the real evaluator over the labelled cases and reports score agreement.

    uv run python -m evals.run [--threshold 85] [--include-unreviewed] [--filter 'seed-*']

Each run is saved as timestamped JSON under evals/results/ (git-ignored).
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from interview_evaluator.evaluator import MODEL, SYSTEM_PROMPT, EvaluationRefused, evaluate

from .dataset import CASES_DIR, BrokenCase, load_cases
from .metrics import AgreementReport, agreement_report

DEFAULT_THRESHOLD = 85.0
RESULTS_DIR = Path(__file__).parent / "results"


def prompt_hash() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]


def matches(name: str, patterns: list[str] | None) -> bool:
    return not patterns or any(fnmatch(name, p) for p in patterns)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.splitlines()[0])
    p.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="PASS when within-±1 agreement %% is at least this (default %(default)s)",
    )
    p.add_argument(
        "--include-unreviewed", action="store_true", help="Also run cases with reviewed: false"
    )
    p.add_argument(
        "--filter",
        action="append",
        metavar="GLOB",
        help="Only run cases whose id matches this glob (repeatable)",
    )
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return p.parse_args(argv)


def format_report(
    report: AgreementReport,
    threshold: float,
    broken: list[BrokenCase] = (),
    errors: dict[str, str] | None = None,
) -> str:
    mae = "n/a" if report.mae is None else f"{report.mae:.2f}"
    lines = [f"BROKEN {b.path}: {b.error}" for b in broken]
    lines += [f"ERROR {case_id}: {error}" for case_id, error in (errors or {}).items()]
    lines += [
        f"Cases: {report.cases}   Competency scores: {report.scores}",
        f"  Exact match:          {report.exact_pct:.1f}%",
        f"  Within ±1:            {report.within_one_pct:.1f}%",
        f"  Mean absolute error:  {mae}",
        f"  Recommendation match: {report.recommendation_pct:.1f}%",
    ]
    misses = [c for c in report.per_case if c.disagreements]
    if misses:
        lines.append("Disagreements (>1 or missing):")
        for c in misses:
            for d in c.disagreements:
                got = "missing" if d.actual is None else d.actual
                lines.append(f"  {c.case_id}: {d.competency!r} expected {d.expected}, got {got}")
    verdict = "PASS" if report.passed(threshold) else "FAIL"
    lines.append(f"{verdict}: within ±1 {report.within_one_pct:.1f}% (threshold {threshold:.1f}%)")
    return "\n".join(lines)


def main(argv: list[str] | None = None, client: anthropic.Anthropic | None = None) -> int:
    args = parse_args(argv)
    cases, broken = load_cases(args.cases_dir)
    cases = [c for c in cases if matches(c.id, args.filter)]
    broken = [b for b in broken if matches(Path(b.path).stem, args.filter)]

    unreviewed = [c for c in cases if not c.reviewed]
    if unreviewed and not args.include_unreviewed:
        ids = ", ".join(c.id for c in unreviewed)
        print(f"WARNING: skipping {len(unreviewed)} unreviewed case(s): {ids}", file=sys.stderr)
        cases = [c for c in cases if c.reviewed]
    if not cases:
        print("No cases to run.", file=sys.stderr)
        return 1

    client = client or anthropic.Anthropic()
    pairs, errors = [], {}
    for case in cases:
        print(f"Evaluating {case.id} ...", file=sys.stderr)
        try:
            pairs.append((case.id, case.expected, evaluate(case.request, client)))
        except (anthropic.APIError, EvaluationRefused, RuntimeError) as e:
            errors[case.id] = f"{type(e).__name__}: {e}"

    report = agreement_report(pairs)
    passed = report.passed(args.threshold)
    print(format_report(report, args.threshold, broken, errors))
    saved = save_results(args.results_dir, report, args.threshold, passed, broken, errors)
    print(f"Results saved to {saved}", file=sys.stderr)
    return 0 if passed else 1


def save_results(
    directory: Path,
    report: AgreementReport,
    threshold: float,
    passed: bool,
    broken: list[BrokenCase],
    errors: dict[str, str],
) -> Path:
    now = datetime.now(UTC)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now:%Y%m%dT%H%M%S%fZ}.json"
    result = {
        "timestamp": now.isoformat(),
        "model": MODEL,
        "prompt_hash": prompt_hash(),
        "threshold": threshold,
        "passed": passed,
        "metrics": report.model_dump(),
        "broken": [b.model_dump() for b in broken],
        "errors": errors,
    }
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")  # the report prints ±
    sys.exit(main())
