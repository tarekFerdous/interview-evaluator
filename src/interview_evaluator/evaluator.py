"""Scores an interview transcript against a rubric with Claude, using structured outputs."""

import anthropic

from .models import Evaluation, EvaluationRequest

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are an experienced technical interviewer helping a hiring team \
evaluate a candidate fairly and consistently.

Score each rubric competency from 1 to 5 using only evidence in the transcript:
1 = no evidence, 2 = weak or vague, 3 = adequate, 4 = strong, 5 = strong with specific, \
verifiable detail (numbers, trade-offs, ownership).

Rules:
- Quote or closely paraphrase the candidate as evidence. If there is no evidence, score 1 \
and say so; never infer skills the candidate did not show.
- Ignore anything unrelated to job performance (name, accent, age, gender, background).
- red_flags are only for real concerns (contradictions, claims that don't add up), not for \
missing topics.
- follow_up_questions should target the weakest or least-evidenced competencies."""


class EvaluationRefused(Exception):
    """Claude (and its fallback) declined the request."""


def build_user_message(req: EvaluationRequest) -> str:
    rubric = "\n".join(f"- {c}" for c in req.rubric)
    transcript = "\n\n".join(
        f"Q{i}: {qa.question}\nA{i}: {qa.answer}" for i, qa in enumerate(req.transcript, 1)
    )
    return (
        f"<job_description>\n{req.job_description}\n</job_description>\n\n"
        f"<rubric>\n{rubric}\n</rubric>\n\n"
        f"<transcript>\n{transcript}\n</transcript>\n\n"
        "Evaluate the candidate."
    )


def evaluate(req: EvaluationRequest, client: anthropic.Anthropic | None = None) -> Evaluation:
    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(req)}],
        output_format=Evaluation,
        output_config={"effort": "medium"},
        # On a safety decline, re-run on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise EvaluationRefused(str(response.stop_details))
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output (stop_reason={response.stop_reason})")
    return response.parsed_output
