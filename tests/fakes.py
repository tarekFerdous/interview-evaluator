"""Fake Anthropic client shared by the tests, so they run without an API key or network."""

from types import SimpleNamespace

from interview_evaluator.models import CompetencyScore, Evaluation

CANNED = Evaluation(
    scores=[CompetencyScore(competency="Python", score=4, evidence="FastAPI service", reasoning="ok")],
    strengths=["Shipped end to end"],
    concerns=["Passive on disagreement"],
    red_flags=[],
    follow_up_questions=["Tell me about a time you pushed back."],
    recommendation="yes",
    summary="Solid builder.",
)


class FakeMessages:
    """Stands in for client.beta.messages. `parsed` is one output for every call, or a list
    consumed one per call; an Exception in that list is raised instead of returned."""

    def __init__(self, stop_reason="end_turn", parsed=CANNED):
        self.stop_reason, self.parsed, self.calls = stop_reason, parsed, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = self.parsed.pop(0) if isinstance(self.parsed, list) else self.parsed
        if isinstance(parsed, Exception):
            raise parsed
        return SimpleNamespace(stop_reason=self.stop_reason, stop_details=None, parsed_output=parsed)


class FakeEmbedder:
    """Deterministic embeddings: a text gets the axis of the first `keywords` entry it contains
    (a one-hot 1024-d vector), else a fixed fallback axis. Records every call."""

    def __init__(self, keywords: list[str] | None = None, vectors: dict[str, list[float]] | None = None):
        """vectors: marker -> explicit (short) vector, zero-padded to 1024; checked first."""
        self.keywords, self.vectors, self.calls = keywords or [], vectors or {}, []

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.calls.append((texts, input_type))
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        for marker, short in self.vectors.items():
            if marker in text:
                return short + [0.0] * (1024 - len(short))
        axis = next((i for i, k in enumerate(self.keywords) if k.lower() in text.lower()), 1023)
        v = [0.0] * 1024
        v[axis] = 1.0
        return v


class FakeTranscriber:
    """Returns a fixed diarized transcript, or raises `error`. Records each call."""

    def __init__(self, utterances=None, duration: float = 120.0, error: Exception | None = None):
        from interview_evaluator.speech import Utterance

        self.utterances = utterances or [
            Utterance(speaker="Speaker 1", text="Hi, thanks for joining.", start=0, end=2),
            Utterance(speaker="Speaker 2", text="Happy to be here.", start=2, end=3),
            Utterance(speaker="Speaker 1", text="Tell me about something you shipped.", start=3, end=6),
            Utterance(speaker="Speaker 2", text="I shipped a FastAPI service to production.", start=6, end=10),
        ]
        self.duration, self.error, self.calls = duration, error, []

    def transcribe(self, audio: bytes, content_type: str, filename: str):
        from interview_evaluator.speech import DiarizedTranscript

        self.calls.append((len(audio), content_type, filename))
        if self.error:
            raise self.error
        return DiarizedTranscript(utterances=self.utterances, duration_seconds=self.duration)


class FakeTokenIssuer:
    region = "westeurope"

    def issue(self) -> str:
        return "fake-short-lived-token"


def segmentation(pairs: int = 1, interviewer: str = "Speaker 1", unanswered_from: int | None = None):
    from interview_evaluator.models import QA
    from interview_evaluator.speech import Segmentation

    return Segmentation(
        interviewer_speaker=interviewer,
        qa=[QA(question=f"Question {i}?", answer=f"Answer {i}.") for i in range(1, pairs + 1)],
        dropped_smalltalk=1,
        unanswered_from=unanswered_from,
    )


def fake_client(messages: FakeMessages) -> SimpleNamespace:
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def evaluation(scores: dict[str, int], recommendation: str = "yes") -> Evaluation:
    return CANNED.model_copy(
        update={
            "scores": [
                CompetencyScore(competency=c, score=s, evidence="e", reasoning="r")
                for c, s in scores.items()
            ],
            "recommendation": recommendation,
        }
    )
