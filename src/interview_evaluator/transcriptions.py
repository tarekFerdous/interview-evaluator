"""Recording upload -> diarized transcription -> Q&A segmentation, as background jobs.

Audio is never stored: it lives in memory (and the upload's spooled temp file, deleted on close)
only until its job finishes. Jobs (diarized text and Q&A) live in memory for JOB_TTL_SECONDS,
which is another reason for the single replica.
"""

import secrets
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import PurePath
from typing import Literal

import anthropic
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from .evaluations import llm_error
from .evaluator import get_client
from .models import MAX_TRANSCRIPT, QA
from .speech import Transcriber, TranscriptionError, Utterance, get_transcriber, segment

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_DURATION_SECONDS = 60 * 60
JOB_TTL_SECONDS = 2 * 60 * 60

# content type -> canonical type sent to Azure; extensions cover browsers that send a generic type
CONTENT_TYPES = {
    "audio/wav": "audio/wav", "audio/x-wav": "audio/wav", "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav", "audio/mpeg": "audio/mpeg", "audio/mp3": "audio/mpeg",
    "audio/mp4": "audio/mp4", "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4",
}
EXTENSIONS = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}

Status = Literal["pending", "transcribing", "segmenting", "done", "error"]


class TranscriptionJob(BaseModel):
    id: str
    status: Status = "pending"
    diarized: list[Utterance] | None = None
    qa: list[QA] | None = None
    interviewer_speaker: str | None = None
    dropped_smalltalk: int | None = None
    warning: str | None = None
    error: str | None = None


class JobStore:
    def __init__(self, ttl: float = JOB_TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._jobs: dict[str, tuple[float, TranscriptionJob]] = {}
        self._lock = threading.Lock()

    def create(self) -> TranscriptionJob:
        job = TranscriptionJob(id=secrets.token_urlsafe(16))
        now = self.clock()
        with self._lock:
            self._jobs = {k: v for k, v in self._jobs.items() if now - v[0] <= self.ttl}
            self._jobs[job.id] = (now, job)
        return job

    def get(self, job_id: str) -> TranscriptionJob | None:
        with self._lock:
            entry = self._jobs.get(job_id)
        if entry is None or self.clock() - entry[0] > self.ttl:
            return None
        return entry[1]


@lru_cache
def get_job_store() -> JobStore:
    return JobStore()


def accepted_content_type(upload: UploadFile) -> str:
    if (ct := CONTENT_TYPES.get((upload.content_type or "").lower())) is not None:
        return ct
    if (ct := EXTENSIONS.get(PurePath(upload.filename or "").suffix.lower())) is not None:
        return ct
    raise HTTPException(415, "Unsupported audio format: upload a wav, mp3 or m4a file")


def process(job: TranscriptionJob, audio: bytes, content_type: str, filename: str,
            transcriber: Transcriber, client: anthropic.Anthropic) -> None:
    """Background task: transcribe, then segment. Every failure ends in status `error`."""
    try:
        job.status = "transcribing"
        transcript = transcriber.transcribe(audio, content_type, filename)
        if transcript.duration_seconds > MAX_DURATION_SECONDS:
            raise TranscriptionError("The recording is longer than 60 minutes; trim it and upload again")
        if not transcript.utterances:
            raise TranscriptionError("No speech was recognised in the recording")
        job.diarized = transcript.utterances
        job.status = "segmenting"
        seg = segment(transcript.utterances, client)
        job.qa, job.interviewer_speaker = seg.qa, seg.interviewer_speaker
        job.dropped_smalltalk = seg.dropped_smalltalk
        if len(seg.qa) > MAX_TRANSCRIPT:
            job.warning = (
                f"{len(seg.qa)} Q&A pairs found; evaluation accepts at most {MAX_TRANSCRIPT}. "
                "Remove the least relevant pairs before evaluating."
            )
        job.status = "done"
    except TranscriptionError as e:
        job.error, job.status = str(e), "error"
    except Exception as e:
        mapped = llm_error(e)
        job.error = mapped[1] if mapped else f"Processing failed: {type(e).__name__}"
        job.status = "error"


router = APIRouter(prefix="/transcriptions", tags=["transcriptions"])


class JobAccepted(BaseModel):
    id: str


@router.post("", status_code=202)
async def create_transcription(
    file: UploadFile,
    background: BackgroundTasks,
    jobs: JobStore = Depends(get_job_store),
    transcriber: Transcriber = Depends(get_transcriber),
    client: anthropic.Anthropic = Depends(get_client),
) -> JobAccepted:
    """Upload a recording (wav/mp3/m4a, at most 100 MB and 60 minutes). Poll GET /transcriptions/{id}."""
    content_type = accepted_content_type(file)
    audio = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(audio) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The recording is larger than 100 MB")
    if not audio:
        raise HTTPException(422, "The uploaded file is empty")
    job = jobs.create()
    background.add_task(process, job, audio, content_type, file.filename or "audio", transcriber, client)
    return JobAccepted(id=job.id)


@router.get("/{job_id}")
def get_transcription(job_id: str, jobs: JobStore = Depends(get_job_store)) -> TranscriptionJob:
    if (job := jobs.get(job_id)) is None:
        raise HTTPException(404, "Transcription not found or expired")
    return job
