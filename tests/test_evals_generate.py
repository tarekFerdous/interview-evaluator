from fakes import FakeMessages, fake_client

from evals import generate
from evals.dataset import load_cases
from interview_evaluator.models import QA

DRAFT = generate.GeneratedCase(
    job_description="Backend engineer building Python services on Postgres.",
    rubric=["Python", "SQL"],
    transcript=[QA(question="What did you build?", answer="A FastAPI service with Postgres.")],
    expected_scores=[
        generate.LabelledScore(competency="Python", score=4),
        generate.LabelledScore(competency="SQL", score=2),
    ],
    expected_recommendation="lean_no",
    notes="Good Python, thin SQL.",
)


def test_generator_writes_unreviewed_valid_cases_with_sonnet(tmp_path):
    messages = FakeMessages(parsed=DRAFT)
    code = generate.main(
        ["--role", "backend", "--quality", "weak", "--out-dir", str(tmp_path)],
        client=fake_client(messages),
    )
    assert code == 0
    assert messages.calls[0]["model"] == "claude-sonnet-5-5"

    [case], broken = load_cases(tmp_path)
    assert broken == []
    assert case.id == "backend-weak" and case.reviewed is False
    assert case.expected.scores == {"Python": 4, "SQL": 2}

    # Existing files are not overwritten without --force.
    generate.main(["--role", "backend", "--quality", "weak", "--out-dir", str(tmp_path)],
                  client=fake_client(messages))
    assert len(messages.calls) == 1
