"""Speech in: Azure AI Speech transcription with diarization, browser tokens, and the Claude
segmenter that turns a diarized transcript into interviewer/candidate Q&A pairs."""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import anthropic
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

from .evaluator import MODEL, EvaluationRefused
from .models import QA

FAST_TRANSCRIPTION_API_VERSION = "2025-10-15"


class Utterance(BaseModel):
    speaker: str = Field(max_length=50)
    text: str = Field(max_length=20_000)
    start: float = Field(0.0, description="Seconds from the start of the audio")
    end: float = Field(0.0, description="Seconds from the start of the audio")


class DiarizedTranscript(BaseModel):
    utterances: list[Utterance]
    duration_seconds: float


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, content_type: str, filename: str) -> DiarizedTranscript: ...


class TokenIssuer(Protocol):
    region: str

    def issue(self) -> str: ...


@dataclass(frozen=True)
class SpeechConfig:
    key: str
    region: str
    endpoint: str  # e.g. https://<region>.api.cognitive.microsoft.com or the resource's own endpoint


def get_speech_config() -> SpeechConfig | None:
    key, region = os.environ.get("AZURE_SPEECH_KEY"), os.environ.get("AZURE_SPEECH_REGION")
    if not key or not region:
        return None
    endpoint = os.environ.get("AZURE_SPEECH_ENDPOINT") or f"https://{region}.api.cognitive.microsoft.com"
    return SpeechConfig(key, region, endpoint.rstrip("/"))


def _require_config() -> SpeechConfig:
    if (config := get_speech_config()) is None:
        raise HTTPException(503, "Speech is not configured (AZURE_SPEECH_KEY / AZURE_SPEECH_REGION)")
    return config


class AzureTranscriber:
    """Azure AI Speech fast transcription (synchronous REST) with speaker diarization."""

    def __init__(self, config: SpeechConfig, max_speakers: int = 2):
        self.config, self.max_speakers = config, max_speakers

    def transcribe(self, audio: bytes, content_type: str, filename: str) -> DiarizedTranscript:
        definition = {
            "locales": ["en-US"],
            "diarization": {"enabled": True, "maxSpeakers": self.max_speakers},
        }
        res = httpx.post(
            f"{self.config.endpoint}/speechtotext/transcriptions:transcribe",
            params={"api-version": FAST_TRANSCRIPTION_API_VERSION},
            headers={"Ocp-Apim-Subscription-Key": self.config.key},
            files={"audio": (filename, audio, content_type)},
            data={"definition": json.dumps(definition)},
            timeout=600,
        )
        if res.status_code != 200:
            raise TranscriptionError(f"Azure Speech returned {res.status_code}: {res.text[:300]}")
        body = res.json()
        return DiarizedTranscript(
            duration_seconds=body.get("durationMilliseconds", 0) / 1000,
            utterances=[
                Utterance(
                    speaker=f"Speaker {p.get('speaker', '?')}",
                    text=p["text"],
                    start=p.get("offsetMilliseconds", 0) / 1000,
                    end=(p.get("offsetMilliseconds", 0) + p.get("durationMilliseconds", 0)) / 1000,
                )
                for p in body.get("phrases", [])
                if p.get("text")
            ],
        )


class AzureTokenIssuer:
    """Short-lived (10 min) token for the browser Speech SDK; the key stays on the server."""

    def __init__(self, config: SpeechConfig):
        self.config, self.region = config, config.region

    def issue(self) -> str:
        res = httpx.post(
            f"{self.config.endpoint}/sts/v1.0/issueToken",
            headers={"Ocp-Apim-Subscription-Key": self.config.key},
            timeout=15,
        )
        if res.status_code != 200:
            raise HTTPException(502, f"Azure Speech token request failed ({res.status_code})")
        return res.text


class TranscriptionError(Exception):
    """The speech provider could not transcribe the audio."""


def get_transcriber() -> Transcriber:
    return AzureTranscriber(_require_config())


def get_token_issuer() -> TokenIssuer:
    return AzureTokenIssuer(_require_config())


# ---- segmentation (Claude, structured output into the existing QA model)

SEGMENT_PROMPT = """You turn a diarized transcript of a job interview into question/answer \
pairs for an evaluation tool.

- Work out which speaker is the interviewer (the one asking the questions) unless it is given.
- Each pair is one interviewer question and the candidate's full answer to it. Merge a \
question split across several utterances, and the candidate's consecutive utterances, into one \
pair. Fold short interviewer follow-ups ("can you say more?") into the same pair.
- Leave out small talk and logistics (greetings, scheduling, audio checks, "any questions for \
us?"); count each one you drop in dropped_smalltalk.
- Keep the candidate's words; fix only obvious transcription glitches. Never invent content.
- If the transcript ends mid-answer or has a question with no answer yet, leave it out and set \
unanswered_from to the number of the first utterance you left out for that reason; otherwise \
set it to null."""

SINGLE_SPEAKER_NOTE = (
    "Only one speaker label appears, so the labels are unreliable: tell the interviewer's "
    "questions from the candidate's answers by their content.\n\n"
)


class Segmentation(BaseModel):
    interviewer_speaker: str = Field(description="Speaker label of the interviewer, as given")
    qa: list[QA]
    dropped_smalltalk: int = Field(description="Number of small-talk/logistics exchanges left out")
    unanswered_from: int | None = Field(
        description="Number of the first utterance of a trailing, not yet complete exchange; "
        "null when every utterance was used or dropped as small talk"
    )


def single_speaker(utterances: Sequence[Utterance]) -> bool:
    """True when the utterances carry at most one real speaker label (diarization gave up)."""
    return len({u.speaker for u in utterances} - {"Unknown"}) <= 1


def format_utterances(utterances: Sequence[Utterance]) -> str:
    return "\n".join(f"{i}. [{u.speaker}] {u.text}" for i, u in enumerate(utterances))


def segment(
    utterances: Sequence[Utterance],
    client: anthropic.Anthropic,
    interviewer_speaker: str | None = None,
) -> Segmentation:
    known = (
        f"The interviewer is {interviewer_speaker}.\n\n" if interviewer_speaker else ""
    ) + (SINGLE_SPEAKER_NOTE if single_speaker(utterances) else "")
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SEGMENT_PROMPT,
        messages=[{
            "role": "user",
            "content": f"{known}<diarized_transcript>\n{format_utterances(utterances)}\n"
            "</diarized_transcript>\n\nSegment it into Q&A pairs.",
        }],
        output_format=Segmentation,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise EvaluationRefused(str(response.stop_details))
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output (stop_reason={response.stop_reason})")
    return response.parsed_output
