"""Live interview sessions: in-memory store, coalescing background re-scoring, and event fan-out.

In-memory state is why the deployment is pinned to one replica.
"""

import asyncio
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from .models import QA, CalibrationExample, Evaluation, EvaluationRequest, InterviewContext

SESSION_TTL_SECONDS = 2 * 60 * 60
EVENT_HISTORY = 100  # events replayed to a (re)connecting subscriber


@dataclass
class Event:
    id: int
    type: str  # scoring | scorecard | error | ended
    data: dict[str, Any]


@dataclass
class Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue


@dataclass(eq=False)
class Session:
    id: str
    context: InterviewContext
    last_active: float
    rubric_id: UUID | None = None  # set when started from a saved rubric (used for calibration)
    calibration: list[CalibrationExample] = field(default_factory=list)  # fixed per session
    interviewer_speaker: str | None = None  # live mic: fixed after the first segmentation
    transcript: list[QA] = field(default_factory=list)
    latest_evaluation: Evaluation | None = None
    evaluated_qa_count: int = 0
    attempted_qa_count: int = 0  # covers failed runs too, so a failure isn't retried in a loop
    scoring: bool = False
    ended: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    idle: threading.Condition = None  # set in __post_init__ (needs the lock)
    events: list[Event] = field(default_factory=list)
    subscribers: list[Subscriber] = field(default_factory=list)
    _next_event_id: int = 1

    def __post_init__(self):
        self.idle = threading.Condition(self.lock)

    def request(self, upto: int | None = None) -> EvaluationRequest:
        return EvaluationRequest(**self.context.model_dump(), transcript=self.transcript[:upto])

    def publish(self, type: str, data: dict[str, Any]) -> None:
        with self.lock:
            event = Event(self._next_event_id, type, data)
            self._next_event_id += 1
            self.events = [*self.events[-(EVENT_HISTORY - 1):], event]
            for sub in list(self.subscribers):
                try:
                    sub.loop.call_soon_threadsafe(sub.queue.put_nowait, event)
                except RuntimeError:  # the subscriber's event loop is gone
                    self.subscribers.remove(sub)

    def subscribe(self) -> tuple[Subscriber, list[Event]]:
        """Registers a subscriber on the running loop; returns it with the history to replay."""
        sub = Subscriber(asyncio.get_running_loop(), asyncio.Queue())
        with self.lock:
            self.subscribers.append(sub)
            return sub, list(self.events)

    def unsubscribe(self, sub: Subscriber) -> None:
        with self.lock:
            if sub in self.subscribers:
                self.subscribers.remove(sub)


class SessionStore(Protocol):
    def create(self, context: InterviewContext, rubric_id: UUID | None = None) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    def __init__(self, ttl: float = SESSION_TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, context: InterviewContext, rubric_id: UUID | None = None) -> Session:
        now = self.clock()
        session = Session(
            id=secrets.token_urlsafe(16), context=context, last_active=now, rubric_id=rubric_id
        )
        with self._lock:
            self._sessions = {k: s for k, s in self._sessions.items() if not self._expired(s, now)}
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        """Returns a live session and refreshes its inactivity timer; None if unknown or expired."""
        now = self.clock()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._expired(session, now):
                del self._sessions[session_id]
                return None
            session.last_active = now
            return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _expired(self, session: Session, now: float) -> bool:
        return now - session.last_active > self.ttl


Evaluate = Callable[[EvaluationRequest], Evaluation]
ErrorMapper = Callable[[Exception], tuple[int, str] | None]
Runner = Callable[[Callable[[], None]], None]


def run_in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def request_scoring(session: Session, evaluate: Evaluate, map_error: ErrorMapper, run: Runner) -> None:
    """Starts the scorer unless one is already running for this session. A running scorer picks
    up Q&As added meanwhile on its next pass, so at most one evaluation is in flight and
    scorecards are emitted in order."""
    with session.lock:
        if session.scoring or session.ended:
            return
        session.scoring = True
    run(lambda: _score_until_caught_up(session, evaluate, map_error))


def _score_until_caught_up(session: Session, evaluate: Evaluate, map_error: ErrorMapper) -> None:
    while True:
        with session.lock:
            qa_count = len(session.transcript)
            if session.ended or qa_count <= session.attempted_qa_count:
                session.scoring = False
                session.idle.notify_all()
                return
            session.attempted_qa_count = qa_count
            req = session.request(qa_count)
        session.publish("scoring", {"qa_count": qa_count})
        try:
            evaluation = evaluate(req)
        except Exception as e:
            status, detail = map_error(e) or (500, f"Evaluation failed: {type(e).__name__}")
            session.publish("error", {"qa_count": qa_count, "status": status, "detail": detail})
            continue
        with session.lock:
            session.latest_evaluation = evaluation
            session.evaluated_qa_count = qa_count
        session.publish("scorecard", {"qa_count": qa_count, "evaluation": evaluation.model_dump()})


def wait_until_idle(session: Session, timeout: float | None = None) -> bool:
    with session.lock:
        return session.idle.wait_for(lambda: not session.scoring, timeout)
