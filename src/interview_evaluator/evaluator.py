"""Scores an interview transcript against a rubric with Claude, using structured outputs."""

import difflib
import re
from collections.abc import Sequence
from functools import lru_cache

import anthropic

from .models import CalibrationExample, Evaluation, EvaluationRequest

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are an experienced technical interviewer helping a hiring team \
evaluate a candidate fairly and consistently.

Score each rubric competency from 1 to 5 using only evidence in the transcript:
1 = no evidence, 2 = weak or vague, 3 = adequate, 4 = strong, 5 = strong with specific, \
verifiable detail (numbers, trade-offs, ownership).

Rules:
- Score every rubric competency once, and copy its full text exactly as the competency name.
- Quote or closely paraphrase the candidate as evidence. If there is no evidence, score 1 \
and say so; never infer skills the candidate did not show.
- Ignore anything unrelated to job performance (name, accent, age, gender, background).
- red_flags are only for real concerns (contradictions, claims that don't add up), not for \
missing topics.
- follow_up_questions should target the weakest or least-evidenced competencies."""

# Added only when calibration examples are sent, so uncalibrated prompts are unchanged.
CALIBRATION_RULE = """
- <calibration_examples> are past candidates for this rubric with scores approved by this \
hiring team. Use them as anchors for how strictly to apply the 1-5 scale, never as evidence \
about this candidate."""


class EvaluationRefused(Exception):
    """Claude (and its fallback) declined the request."""


@lru_cache
def get_client() -> anthropic.Anthropic:
    """FastAPI dependency (tests override it with a fake)."""
    return anthropic.Anthropic()


CACHE = {"type": "ephemeral"}


def format_transcript(transcript) -> str:
    return "\n\n".join(f"Q{i}: {qa.question}\nA{i}: {qa.answer}" for i, qa in enumerate(transcript, 1))


def calibration_block(examples: Sequence[CalibrationExample]) -> str:
    """Transcript plus the human-approved scores and recommendation only (no model reasoning)."""
    parts = []
    for n, ex in enumerate(examples, 1):
        scores = "\n".join(f"- {c}: {s}" for c, s in ex.scores.items())
        parts.append(
            f'<example n="{n}">\n<transcript>\n{format_transcript(ex.transcript)}\n</transcript>\n'
            f"<approved_scores>\n{scores}\n</approved_scores>\n"
            f"<approved_recommendation>{ex.recommendation}</approved_recommendation>\n</example>"
        )
    return "<calibration_examples>\n" + "\n\n".join(parts) + "\n</calibration_examples>"


def build_user_content(
    req: EvaluationRequest, calibration: Sequence[CalibrationExample] = ()
) -> list[dict]:
    """Stable prefix (job description + rubric, then any calibration examples) cached; the growing
    transcript goes last, uncached, so re-evaluating a live session after each answer re-reads the
    prefix from cache."""
    rubric = "\n".join(f"- {c}" for c in req.rubric)
    blocks = [
        {
            "type": "text",
            "text": f"<job_description>\n{req.job_description}\n</job_description>\n\n"
            f"<rubric>\n{rubric}\n</rubric>",
            "cache_control": CACHE,
        }
    ]
    if calibration:
        blocks.append({"type": "text", "text": calibration_block(calibration), "cache_control": CACHE})
    blocks.append(
        {
            "type": "text",
            "text": f"<transcript>\n{format_transcript(req.transcript)}\n</transcript>\n\n"
            "Evaluate the candidate.",
        }
    )
    return blocks


def _head(text: str) -> str:
    """'Root-cause analysis: breaks a problem...' -> 'root-cause analysis'."""
    return re.split(r"\s*(?::|\s[-–—]\s|\()", text, maxsplit=1)[0].strip().casefold()


def match_competency(name: str, rubric: Sequence[str]) -> str | None:
    """The rubric competency a returned name refers to. The model sometimes shortens long
    competencies to their title, which would break approvals, calibration and evals that key
    scores by the exact rubric text. None when nothing matches closely."""
    if name in rubric:
        return name
    folded = name.strip().casefold()
    if not folded:
        return None
    for c in rubric:
        if c.strip().casefold() == folded or _head(c) == _head(name):
            return c
    for c in rubric:
        if c.casefold().startswith(folded):
            return c
    heads = {_head(c): c for c in rubric}
    close = difflib.get_close_matches(_head(name), list(heads), n=1, cutoff=0.8)
    return heads[close[0]] if close else None


def with_rubric_names(evaluation: Evaluation, rubric: Sequence[str]) -> Evaluation:
    """Renames each score's competency to its exact rubric text; unmatched names are kept."""
    scores = [s.model_copy(update={"competency": match_competency(s.competency, rubric) or s.competency})
              for s in evaluation.scores]
    return evaluation.model_copy(update={"scores": scores})


def evaluate(
    req: EvaluationRequest,
    client: anthropic.Anthropic | None = None,
    calibration: Sequence[CalibrationExample] = (),
) -> Evaluation:
    client = client or anthropic.Anthropic()
    system = SYSTEM_PROMPT + (CALIBRATION_RULE if calibration else "")
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": system, "cache_control": CACHE}],
        messages=[{"role": "user", "content": build_user_content(req, calibration)}],
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
    return with_rubric_names(response.parsed_output, req.rubric)
