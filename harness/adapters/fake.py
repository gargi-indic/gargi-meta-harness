"""A scripted :class:`AgentAdapter` for tests and the editor's dry run.

A script maps an idempotency key (or ``"*"`` as the default for any key) to a list of *phases*; each phase
is a list of snapshots. ``poll`` walks the current phase and keeps returning its last snapshot once it is
exhausted. ``approve_plan`` and ``send`` advance to the next phase (staying on the last one if there is no
next). Every call is recorded in :attr:`FakeAdapter.calls` for assertions.
"""

from dataclasses import dataclass
from typing import Any

from harness.adapters.base import (
    AgentSnapshot,
    AgentState,
    Capabilities,
    PrRef,
    SessionRef,
    StartRequest,
)

_DEFAULT_SCRIPT: list[list[AgentSnapshot]] = [[AgentSnapshot(state=AgentState.STARTING)]]


@dataclass
class _Session:
    ref: SessionRef
    phases: list[list[AgentSnapshot]]
    phase: int = 0
    index: int = 0
    cancelled: bool = False


def _pr(number: int, head_sha: str) -> PrRef:
    return PrRef(number=number, url=f"https://github.com/example/repo/pull/{number}", head_sha=head_sha)


class FakeAdapter:
    def __init__(
        self,
        name: str = "fake",
        script: dict[str, list[list[AgentSnapshot]]] | None = None,
        caps: Capabilities | None = None,
    ) -> None:
        self.name = name
        self.caps = caps or Capabilities(
            plan_approval=True, messaging=True, opens_pr=True, local_workspace=True, max_concurrency=10
        )
        self.script = script or {}
        for key, phases in self.script.items():
            if not phases or any(not phase for phase in phases):
                raise ValueError(f"script for {key!r} needs at least one phase, each with a snapshot")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._sessions: dict[str, _Session] = {}
        self._by_key: dict[str, str] = {}

    def _session(self, ref: SessionRef) -> _Session:
        try:
            return self._sessions[ref.id]
        except KeyError:
            raise KeyError(f"unknown fake session {ref.id!r}") from None

    def _advance(self, session: _Session) -> None:
        if session.phase < len(session.phases) - 1:
            session.phase += 1
            session.index = 0

    async def start(self, req: StartRequest) -> SessionRef:
        existing = self._by_key.get(req.idempotency_key)
        if existing is not None:
            return self._sessions[existing].ref
        self.calls.append(("start", {"idempotency_key": req.idempotency_key, "issue": req.issue}))
        session_id = f"fake-{len(self._sessions) + 1}"
        ref = SessionRef(
            agent=self.name,
            id=session_id,
            url=f"https://fake.invalid/sessions/{session_id}",
            data={"idempotency_key": req.idempotency_key},
        )
        phases = self.script.get(req.idempotency_key) or self.script.get("*") or _DEFAULT_SCRIPT
        self._sessions[session_id] = _Session(ref=ref, phases=phases)
        self._by_key[req.idempotency_key] = session_id
        return ref

    async def find(self, idempotency_key: str) -> SessionRef | None:
        self.calls.append(("find", {"idempotency_key": idempotency_key}))
        session_id = self._by_key.get(idempotency_key)
        return self._sessions[session_id].ref if session_id is not None else None

    async def poll(self, ref: SessionRef) -> AgentSnapshot:
        self.calls.append(("poll", {"session": ref.id}))
        session = self._session(ref)
        if session.cancelled:
            return AgentSnapshot(state=AgentState.FAILED, error="cancelled")
        phase = session.phases[session.phase]
        snapshot = phase[min(session.index, len(phase) - 1)]
        if session.index < len(phase) - 1:
            session.index += 1
        return snapshot.model_copy(deep=True)

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None:
        self.calls.append(("approve_plan", {"session": ref.id, "note": note}))
        self._advance(self._session(ref))

    async def send(self, ref: SessionRef, text: str) -> None:
        self.calls.append(("send", {"session": ref.id, "text": text}))
        self._advance(self._session(ref))

    async def cancel(self, ref: SessionRef) -> None:
        self.calls.append(("cancel", {"session": ref.id}))
        self._session(ref).cancelled = True

    # Builders. Each returns an adapter whose "*" script applies to every idempotency key.

    @classmethod
    def happy_path(cls) -> "FakeAdapter":
        """PLANNING -> PLAN_READY | approve_plan | WORKING -> PR_READY (#101)."""
        return cls(
            script={
                "*": [
                    [
                        AgentSnapshot(state=AgentState.PLANNING),
                        AgentSnapshot(state=AgentState.PLAN_READY, plan="1. Do the thing."),
                    ],
                    [
                        AgentSnapshot(state=AgentState.WORKING),
                        AgentSnapshot(state=AgentState.PR_READY, pr=_pr(101, "a1")),
                    ],
                ]
            }
        )

    @classmethod
    def plan_rejected_once(cls) -> "FakeAdapter":
        """PLAN_READY (v1) | send | PLAN_READY (v2) | approve_plan | PR_READY."""
        return cls(
            script={
                "*": [
                    [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v1")],
                    [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v2")],
                    [AgentSnapshot(state=AgentState.PR_READY, pr=_pr(101, "a1"))],
                ]
            }
        )

    @classmethod
    def pr_fix_round(cls) -> "FakeAdapter":
        """PLAN_READY | approve_plan | PR_READY (a1) | send | WORKING -> PR_READY (b2)."""
        return cls(
            script={
                "*": [
                    [AgentSnapshot(state=AgentState.PLAN_READY, plan="the plan")],
                    [AgentSnapshot(state=AgentState.PR_READY, pr=_pr(101, "a1"))],
                    [
                        AgentSnapshot(state=AgentState.WORKING),
                        AgentSnapshot(state=AgentState.PR_READY, pr=_pr(101, "b2")),
                    ],
                ]
            }
        )

    @classmethod
    def fails(cls, error: str = "agent failed") -> "FakeAdapter":
        """PLANNING -> FAILED."""
        return cls(
            script={
                "*": [
                    [
                        AgentSnapshot(state=AgentState.PLANNING),
                        AgentSnapshot(state=AgentState.FAILED, error=error),
                    ]
                ]
            }
        )

    @classmethod
    def asks_question(cls, question: str = "Which approach should I take?") -> "FakeAdapter":
        """PLANNING -> NEEDS_INPUT | send | PLAN_READY | approve_plan | PR_READY."""
        return cls(
            script={
                "*": [
                    [
                        AgentSnapshot(state=AgentState.PLANNING),
                        AgentSnapshot(state=AgentState.NEEDS_INPUT, question=question),
                    ],
                    [AgentSnapshot(state=AgentState.PLAN_READY, plan="the plan")],
                    [AgentSnapshot(state=AgentState.PR_READY, pr=_pr(101, "a1"))],
                ]
            }
        )
