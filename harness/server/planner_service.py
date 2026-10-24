"""Planner API service (M4.5): load a plan, decompose it in the background, edit the draft, approve and create.

GitHub is the state store; SQLite is a cache. Everything a client can see is derived from the proposal that
``harness.planner.proposal_store`` keeps as comments on the control issue (mirrored in the ``proposals`` table):

* **Identity.** A proposal id is ``<owner>~<name>~<token>``; its run id is ``<owner>/<name>#planner-<token>``, so
  ``harness sync`` restores it with the other proposals of the repo. A job id is ``<proposal id>.<kind>`` with
  ``kind`` ``decompose`` or ``create``.
* **Status** (stored with the proposal): ``drafting`` (the decomposer is running; the proposal is a placeholder
  until it answers), ``ready`` or ``invalid`` (a draft that passes / fails the checks), ``creating`` (approved,
  ``materialize`` is running) and ``created``. ``rev`` counts the changes of the draft; ``meta`` (also stored with
  the proposal) keeps the plan source, the control issue, the decomposition errors, the creation error and result.
* **Jobs** are views of that status: ``decompose`` is running while ``drafting``, failed when the decomposition
  left errors, done otherwise; ``create`` is running while ``creating``, done when ``created`` (with the result
  from ``meta``) and failed when a ``ready`` proposal carries the error of the last attempt. Asyncio tasks only
  do the work. After a restart the first read of a ``drafting`` or ``creating`` proposal starts the task again:
  the decomposer is asked again, ``materialize`` continues where it stopped (its run key, ``plan_run_key``, is
  derived from the frozen draft).
* **Writes.** Every change (an edit, a finished job, the approval) is first written as a *pending draft* to the
  ``meta`` table (key ``planner:pending:<run id>``) and then saved to GitHub by one background flusher per
  proposal. The flusher saves the newest pending draft only, so rapid edits coalesce into one write (and the
  outbox coalesces the comment edits queued under the same key). Reads answer from the pending draft while there
  is one (``saved`` is false, ``save_error`` says why a save failed), so a client always reads its own writes;
  ``rev`` only ever grows and ``If-Match`` is checked against the pending draft. Once GitHub has the draft and
  the mirror is promoted, the pending draft is dropped. A pending draft survives a restart in the local database
  and is saved again on the next read; until then it exists only there, which is the one place where the local
  database holds something GitHub does not.
* **Events.** Each change appends a ``planner_updated`` row to the event log; the SSE tailer publishes it as
  ``planner.updated``.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import Field, ValidationError

from harness.adapters.base import Capabilities
from harness.config import GlobalConfig, RepoConfig
from harness.github.client import GitHub, GitHubError
from harness.github.outbox import Outbox, OutboxOpFailed
from harness.llm.backend import ClaudeSdkBackend, ReviewBackend
from harness.llm.decomposer import DecompositionError, decompose
from harness.planner.context import build_repo_context
from harness.planner.materialize import (
    MaterializeContext,
    MaterializeError,
    MaterializeResult,
    materialize,
    plan_run_key,
)
from harness.planner.options import enabled_agents, repo_playbooks
from harness.planner.plan_doc import PlanDoc, PlanDocError, load_plan_doc
from harness.planner.plan_gate import PlanApproval, PlanApprovalStatus, check_plan_approval
from harness.planner.proposal import (
    DEFAULT_MAX_ISSUES,
    ProposalIssue,
    ProposedIssue,
    ProposedPlan,
    validate_proposal,
)
from harness.planner.proposal_store import StoredProposal, get_proposal, save_proposal
from harness.playbook.models import Playbook
from harness.secrets import redact
from harness.server.derive import repo_config_of
from harness.server.schemas import Schema
from harness.store.db import Store
from harness.store.repo import append_event, get_meta, get_repo, set_meta

log = logging.getLogger(__name__)

STATUS_DRAFTING = "drafting"
STATUS_READY = "ready"
STATUS_INVALID = "invalid"
STATUS_CREATING = "creating"
STATUS_CREATED = "created"
KIND_DECOMPOSE = "decompose"
KIND_CREATE = "create"
EVENT_KIND = "planner_updated"

MAX_GUIDANCE_CHARS = 4000
RETRY_FLUSH_AFTER_S = 10.0
PLAYBOOK_TTL_S = 30.0

# The capabilities of the agents the harness knows, for a server that has no adapter registry (the daemon owns
# that). They mirror ``harness.adapters.{jules,claude,codex,copilot}``; ``max_concurrency`` comes from the repo config.
# Like the daemon, the server offers an agent only when its key is in the environment.
KNOWN_AGENT_CAPS: dict[str, Capabilities] = {
    "jules": Capabilities(plan_approval=True, messaging=True, opens_pr=True, local_workspace=False),
    "claude": Capabilities(plan_approval=True, messaging=True, opens_pr=False, local_workspace=True),
    "codex": Capabilities(plan_approval=True, messaging=True, opens_pr=False, local_workspace=True),
    # Not in ``_AGENT_KEYS``: Copilot is opt-in through ``agents.copilot`` in a repo config, not an env key.
    "copilot": Capabilities(plan_approval=False, messaging=True, opens_pr=True, local_workspace=False),
}

_AGENT_KEYS = {"jules": "JULES_API_KEY", "claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY"}
_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_TOKEN = re.compile(r"^[0-9a-f]{12}$")

MaterializeFn = Callable[[ProposedPlan, MaterializeContext], Awaitable[MaterializeResult]]
ApprovalFn = Callable[..., Awaitable[PlanApproval]]


class PlannerError(Exception):
    """A refusal with an HTTP status; ``detail`` is a string or a JSON object."""

    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def refusal(status: int, reason: str, message: str, **extra: Any) -> PlannerError:
    return PlannerError(status, {"reason": reason, "message": message, **extra})


# ---- response models ------------------------------------------------------------------------------------


class ApprovalOut(Schema):
    status: str = Field(
        description="pending, approved, stale (the plan changed after the approval) or rejected."
    )
    via: str | None = Field(description="How it was approved: label, pr or cli; null when not approved.")
    approved_by: str | None = Field(description="Login of the approver; null when not approved.")
    note: str | None = Field(description="Why an approval signal was ignored, if one was.")


class PlanOut(Schema):
    repo: str = Field(description="Repository, owner/name.")
    path: str | None = Field(description="Plan file in the repo; null for an issue plan.")
    issue: int | None = Field(description="Plan issue number; null for a file plan.")
    ref: str | None = Field(description="Branch or commit the file was read at; null for the default branch.")
    sha: str = Field(
        description="Version of the plan text: the blob sha of a file, the text hash of an issue."
    )
    title: str = Field(description="The plan's first level-1 heading (or its first heading, or its name).")
    markdown: str = Field(description="The plan text.")
    headings: list[str] = Field(description="Every heading of the plan in order.")
    approval: ApprovalOut | None = Field(
        description="The plan approval; null when the repo has no control issue to read it from."
    )


class ProposalIssueOut(Schema):
    code: str = Field(description="Machine-readable problem code, for example unknown_dep.")
    key: str | None = Field(description="Key of the proposed issue concerned; null for plan-wide problems.")
    message: str = Field(description="What is wrong.")


class DecomposeAccepted(Schema):
    proposal_id: str = Field(description="Id of the new proposal; poll GET /api/planner/proposals/{id}.")
    job_id: str = Field(description="Id of the decompose job; poll GET /api/planner/jobs/{id}.")


class ProposalOut(Schema):
    id: str = Field(description="Proposal id.")
    repo: str = Field(description="Repository, owner/name.")
    status: str = Field(description="drafting, ready, invalid, creating or created.")
    plan_sha: str = Field(description="Version of the plan the proposal was made for.")
    plan: str = Field(description="The plan source, a file path or #issue.")
    rev: int = Field(description="Counts the changes of the draft; send it back as If-Match.")
    saved: bool = Field(
        description="False while the newest rev is only pending locally and not on GitHub yet."
    )
    save_error: str | None = Field(description="Why the last attempt to save the draft to GitHub failed.")
    proposal: dict[str, Any] | None = Field(
        description="The ProposedPlan (epic_title, epic_body, issues, notes); null while drafting or when "
        "the decomposition produced none."
    )
    issues_validation: list[ProposalIssueOut] = Field(
        description="Everything wrong with the draft; the decomposition's errors when it produced none."
    )


class ValidationOut(Schema):
    rev: int = Field(description="The rev that was validated.")
    ok: bool = Field(description="True when there is no issue.")
    issues: list[ProposalIssueOut] = Field(description="Everything wrong with the draft.")


class JobOut(Schema):
    job_id: str = Field(description="Job id, <proposal id>.decompose or <proposal id>.create.")
    kind: str = Field(description="decompose or create.")
    proposal_id: str = Field(description="The proposal the job works on.")
    status: Literal["running", "done", "failed"] = Field(description="Where the job is.")
    result: dict[str, Any] | None = Field(
        description="MaterializeResult (epic_number, issues, created, reused, links_created, labels_ensured) "
        "of a finished create job; null otherwise."
    )
    error: str | None = Field(description="Why the job failed.")
    issues: list[ProposalIssueOut] = Field(description="The problems found, for a decompose job that failed.")


class AgentOption(Schema):
    name: str = Field(description="Agent name.")
    enabled: bool = Field(description="False when the repo config switches the agent off.")
    caps: list[str] = Field(description="Capability names: plan, message, pr, local; empty when unknown.")


class OptionsOut(Schema):
    agents: list[AgentOption] = Field(description="Agents a proposed issue may name, with capabilities.")
    playbooks: list[str] = Field(description="Ids of the playbooks a proposed issue may name.")


# ---- internal state -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class View:
    """A proposal as a client sees it: the stored one with the pending draft on top."""

    id: str
    repo: str
    run_id: str
    control_issue: int
    plan_sha: str
    proposal: ProposedPlan
    status: str
    rev: int
    meta: dict[str, Any] = field(default_factory=dict)
    saved: bool = True
    save_error: str | None = None

    @property
    def placeholder(self) -> bool:
        return bool(self.meta.get("placeholder"))


def _placeholder() -> ProposedPlan:
    """What a proposal holds before the decomposer has answered (a proposal needs at least one issue)."""
    stub = ProposedIssue(
        key="pending", title="", body="", acceptance=[], blocked_by=[], agent="", playbook=""
    )
    return ProposedPlan(epic_title="Decomposition in progress", epic_body="", issues=[stub])


def default_agent_caps(environ: Mapping[str, str] | None = None) -> dict[str, Capabilities]:
    """The known agents whose API key is set, as ``harness daemon`` would register them."""
    env = os.environ if environ is None else environ
    return {name: KNOWN_AGENT_CAPS[name] for name, var in _AGENT_KEYS.items() if env.get(var)}


def make_id(repo: str, token: str) -> str:
    return f"{repo.replace('/', '~')}~{token}"


def parse_id(proposal_id: str) -> tuple[str, str]:
    """``(repo, run_id)`` of a proposal id; PlannerError 404 when it is not one."""
    parts = proposal_id.split("~")
    if len(parts) != 3 or not _NAME_PART.match(parts[0]) or not _NAME_PART.match(parts[1]):
        raise PlannerError(404, "unknown proposal")
    if not _TOKEN.match(parts[2]):
        raise PlannerError(404, "unknown proposal")
    repo = f"{parts[0]}/{parts[1]}"
    return repo, f"{repo}#planner-{parts[2]}"


def parse_job_id(job_id: str) -> tuple[str, str]:
    proposal_id, _, kind = job_id.rpartition(".")
    if kind not in (KIND_DECOMPOSE, KIND_CREATE):
        raise PlannerError(404, "unknown job")
    return proposal_id, kind


def parse_if_match(value: str | None) -> int | None:
    """The rev in an ``If-Match`` header (``3``, ``"3"`` or ``W/"3"``); None for no header or ``*``."""
    if value is None:
        return None
    text = value.strip()
    if text == "*":
        return None
    text = text.removeprefix("W/").strip('"')
    try:
        return int(text)
    except ValueError:
        raise PlannerError(400, "If-Match must carry the rev of the proposal") from None


def _error_text(exc: BaseException) -> str:
    return redact(" ".join(f"{type(exc).__name__}: {exc}".split()))[:2000]


def _issue_out(issue: ProposalIssue | Mapping[str, Any]) -> ProposalIssueOut:
    if isinstance(issue, ProposalIssue):
        return ProposalIssueOut(code=issue.code, key=issue.key, message=redact(issue.message))
    return ProposalIssueOut(
        code=str(issue.get("code") or "error"),
        key=issue.get("key"),
        message=redact(str(issue.get("message") or "")),
    )


def _problem(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "key": None, "message": redact(message)[:2000]}


class PlannerService:
    def __init__(
        self,
        *,
        store: Store,
        gh: GitHub,
        config: GlobalConfig,
        backend: ReviewBackend | None = None,
        agent_caps: Mapping[str, Capabilities] | None = None,
        materialize_fn: MaterializeFn = materialize,
        check_approval: ApprovalFn = check_plan_approval,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        playbook_ttl_s: float = PLAYBOOK_TTL_S,
    ) -> None:
        self.store = store
        self.gh = gh
        self.config = config
        self._backend = backend
        self.agent_caps: Mapping[str, Capabilities] = (
            agent_caps if agent_caps is not None else default_agent_caps()
        )
        self.materialize_fn = materialize_fn
        self.check_approval = check_approval
        self.outbox = Outbox(store, gh, sleep=sleep)
        self.playbook_ttl_s = playbook_ttl_s
        self._locks: dict[str, asyncio.Lock] = {}
        self._write_lock = asyncio.Lock()  # one user of the outbox at a time (drain_once is not re-entrant)
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._flushers: dict[str, asyncio.Task[None]] = {}
        self._flush_failed: dict[str, float] = {}
        self._playbook_cache: dict[str, tuple[float, dict[str, Playbook]]] = {}

    # ---- lifecycle ---------------------------------------------------------------------------------------

    async def idle(self) -> None:
        """Wait until no background job or flusher is running (tests, shutdown)."""
        while True:
            tasks = [t for t in [*self._jobs.values(), *self._flushers.values()] if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        tasks = [t for t in [*self._jobs.values(), *self._flushers.values()] if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---- config ------------------------------------------------------------------------------------------

    def _known_repo(self, repo: str) -> RepoConfig:
        if repo not in {entry.name for entry in self.config.repos}:
            raise PlannerError(404, "unknown repo")
        stored = get_repo(self.store, repo)
        return repo_config_of(stored["config"] if stored else None)

    def agents(self, repo: str) -> dict[str, Capabilities]:
        """The agents a proposal may name: the known ones that the repo config does not switch off."""
        cfg = self._known_repo(repo)
        caps = {
            name: (
                found.model_copy(update={"max_concurrency": cfg.agents[name].max_concurrency})
                if name in cfg.agents
                else found
            )
            for name, found in self.agent_caps.items()
        }
        return enabled_agents(caps, cfg.agents)

    async def _playbooks(self, repo: str) -> dict[str, Playbook]:
        cached = self._playbook_cache.get(repo)
        if cached is not None and time.monotonic() - cached[0] < self.playbook_ttl_s:
            return cached[1]
        found = await repo_playbooks(self.gh, repo)
        self._playbook_cache[repo] = (time.monotonic(), found)
        return found

    async def options(self, repo: str) -> OptionsOut:
        cfg = self._known_repo(repo)
        names = sorted({*self.agent_caps, *cfg.agents})
        agents = [
            AgentOption(
                name=name,
                enabled=name not in cfg.agents or cfg.agents[name].enabled,
                caps=sorted(self.agent_caps[name].names()) if name in self.agent_caps else [],
            )
            for name in names
        ]
        return OptionsOut(agents=agents, playbooks=sorted(await self._playbooks(repo)))

    # ---- the plan ----------------------------------------------------------------------------------------

    async def _load_plan(self, repo: str, *, path: str | None, issue: int | None, ref: str | None) -> PlanDoc:
        try:
            return await load_plan_doc(self.gh, repo, path=path, issue=issue, ref=ref)
        except PlanDocError as exc:
            raise PlannerError(404 if "not found" in str(exc) else 422, str(exc)) from exc

    async def _approval(self, repo: str, control_issue: int, plan: PlanDoc) -> PlanApproval:
        cfg = self._known_repo(repo)
        return await self.check_approval(
            self.gh,
            repo=repo,
            control_issue=control_issue,
            plan=plan,
            owner_allowlist=cfg.command_users,
            store=self.store,
        )

    async def get_plan(
        self,
        repo: str,
        *,
        path: str | None,
        issue: int | None,
        ref: str | None,
        control_issue: int | None,
    ) -> PlanOut:
        cfg = self._known_repo(repo)
        plan = await self._load_plan(repo, path=path, issue=issue, ref=ref)
        approval = None
        control = control_issue if control_issue is not None else cfg.control_issue
        if control is not None:
            found = await self._approval(repo, control, plan)
            approval = ApprovalOut(
                status=found.status.value, via=found.via, approved_by=found.approved_by, note=found.note
            )
        return PlanOut(
            repo=repo,
            path=plan.path,
            issue=plan.issue,
            ref=plan.ref,
            sha=plan.sha,
            title=plan.title,
            markdown=plan.text,
            headings=plan.headings,
            approval=approval,
        )

    # ---- pending drafts (the local side of a GitHub-first write) ----------------------------------------

    @staticmethod
    def _marker_key(run_id: str) -> str:
        return f"planner:pending:{run_id}"

    @staticmethod
    def _validated_key(run_id: str) -> str:
        return f"planner:validated:{run_id}"

    def _marker(self, run_id: str) -> dict[str, Any] | None:
        raw = get_meta(self.store, self._marker_key(run_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    def _set_validated(self, run_id: str, rev: int) -> None:
        set_meta(self.store, self._validated_key(run_id), str(rev))

    def _validated(self, run_id: str) -> int | None:
        raw = get_meta(self.store, self._validated_key(run_id))
        return int(raw) if raw and raw.isdigit() else None

    async def _load(self, repo: str, run_id: str) -> View | None:
        """The stored proposal (mirror, else GitHub) with the pending draft on top; None when there is none."""
        cfg = self._known_repo(repo)
        marker = self._marker(run_id)
        control = (marker or {}).get("control_issue") or cfg.control_issue
        stored: StoredProposal | None = None
        if control is not None or self._mirrored(run_id):
            try:
                stored = await get_proposal(
                    self.store, self.gh, repo=repo, control_issue=control or 0, run_id=run_id
                )
            except GitHubError as exc:
                raise PlannerError(502, f"github: {exc.status}") from exc
        if stored is None:
            return None
        meta = stored.meta
        view = View(
            id=make_id(repo, run_id.rsplit("-", 1)[1]),
            repo=repo,
            run_id=run_id,
            control_issue=int(meta.get("control_issue") or control or 0),
            plan_sha=stored.plan_sha,
            proposal=stored.proposal,
            status=stored.status,
            rev=stored.rev,
            meta=dict(meta),
        )
        if marker is None or marker.get("plan_sha") != stored.plan_sha:
            return view
        try:
            proposal = ProposedPlan.model_validate(marker["proposal"])
        except (KeyError, ValueError):
            return view
        return replace(
            view,
            proposal=proposal,
            status=str(marker["status"]),
            rev=int(marker["rev"]),
            meta=dict(marker.get("meta") or {}),
            saved=False,
            save_error=marker.get("error"),
        )

    def _mirrored(self, run_id: str) -> bool:
        return bool(self.store.query("SELECT 1 FROM proposals WHERE run_id = ?", (run_id,)))

    async def _need(self, proposal_id: str) -> View:
        repo, run_id = parse_id(proposal_id)
        view = await self._load(repo, run_id)
        if view is None:
            raise PlannerError(404, "unknown proposal")
        return view

    def _stage(
        self,
        view: View,
        *,
        what: str,
        proposal: ProposedPlan | None = None,
        status: str | None = None,
        meta: dict[str, Any] | None = None,
        bump: bool = False,
    ) -> View:
        """Write the new state as the pending draft, record the event and start saving it. Call under the lock."""
        new = replace(
            view,
            proposal=proposal if proposal is not None else view.proposal,
            status=status if status is not None else view.status,
            meta=dict(meta) if meta is not None else dict(view.meta),
            rev=view.rev + (1 if bump else 0),
            saved=False,
            save_error=None,
        )
        previous = self._marker(view.run_id)
        data = {
            "seq": (previous["seq"] if previous else 0) + 1,
            "plan_sha": new.plan_sha,
            "control_issue": new.control_issue,
            "proposal": new.proposal.model_dump(mode="json"),
            "status": new.status,
            "rev": new.rev,
            "meta": new.meta,
            "error": None,
        }
        with self.store.tx():
            set_meta(self.store, self._marker_key(view.run_id), json.dumps(data, sort_keys=True))
            self._event(new, what)
        self._flush_failed.pop(view.run_id, None)
        self._kick_flush(view.repo, view.run_id)
        return new

    def _event(self, view: View, what: str) -> None:
        append_event(
            self.store,
            kind=EVENT_KIND,
            repo=view.repo,
            data={"proposal_id": view.id, "status": view.status, "rev": view.rev, "what": what},
        )

    def _kick_flush(self, repo: str, run_id: str) -> asyncio.Task[None]:
        task = self._flushers.get(run_id)
        if task is None or task.done():
            task = asyncio.create_task(self._flush_loop(repo, run_id))
            self._flushers[run_id] = task
        return task

    async def _flush(self, repo: str, run_id: str) -> None:
        """Wait until the pending draft has been saved (or a save failed)."""
        if self._marker(run_id) is None:
            return
        await self._kick_flush(repo, run_id)

    async def _flush_loop(self, repo: str, run_id: str) -> None:
        """Save the newest pending draft until none is left; a draft staged meanwhile is saved by the next round.

        The draft is read only once the outbox is free, so edits made while another save is running coalesce.
        """
        while True:
            seq: int | None = None
            try:
                async with self._write_lock:
                    marker = self._marker(run_id)
                    if marker is None:
                        return
                    seq = int(marker["seq"])
                    await save_proposal(
                        self.store,
                        self.outbox,
                        repo=repo,
                        control_issue=int(marker["control_issue"]),
                        run_id=run_id,
                        plan_sha=marker["plan_sha"],
                        proposal=ProposedPlan.model_validate(marker["proposal"]),
                        status=marker["status"],
                        rev=int(marker["rev"]),
                        meta=marker.get("meta") or {},
                    )
            except (GitHubError, OutboxOpFailed, ValueError, KeyError) as exc:
                self._flush_failed[run_id] = time.monotonic()
                if seq is not None:
                    self._mark_failed(run_id, seq, _error_text(exc))
                return
            self._promote(run_id, seq)

    def _mark_failed(self, run_id: str, seq: int, error: str) -> None:
        with self.store.tx():
            marker = self._marker(run_id)
            if marker is not None and marker["seq"] == seq:
                marker["error"] = error
                set_meta(self.store, self._marker_key(run_id), json.dumps(marker, sort_keys=True))

    def _promote(self, run_id: str, seq: int) -> None:
        """The mirror has the draft: drop the pending one, unless a newer one was staged while it was saved."""
        with self.store.tx():
            marker = self._marker(run_id)
            if marker is not None and marker["seq"] == seq:
                self.store.execute("DELETE FROM meta WHERE key = ?", (self._marker_key(run_id),))

    def _resume(self, view: View) -> None:
        """Start the work a restart interrupted: a failed save, a decomposition, a creation."""
        run_id = view.run_id
        if not view.saved:
            task = self._flushers.get(run_id)
            failed = self._flush_failed.get(run_id)
            retry = failed is None or time.monotonic() - failed >= RETRY_FLUSH_AFTER_S
            if (task is None or task.done()) and retry:
                self._kick_flush(view.repo, run_id)
        if view.status == STATUS_DRAFTING:
            self._spawn_decompose(view.repo, run_id)
        if view.status == STATUS_CREATING and not self._alive(run_id, KIND_CREATE):
            self._spawn(view.repo, run_id, KIND_CREATE, lambda: self._create_job(view.repo, run_id))

    # ---- background jobs ---------------------------------------------------------------------------------

    def _alive(self, run_id: str, kind: str) -> bool:
        task = self._jobs.get(f"{run_id}:{kind}")
        return task is not None and not task.done()

    def _spawn(self, repo: str, run_id: str, kind: str, work: Callable[[], Awaitable[None]]) -> None:
        key = f"{run_id}:{kind}"
        task = asyncio.create_task(self._guarded(repo, run_id, kind, work))
        self._jobs[key] = task

    async def _guarded(self, repo: str, run_id: str, kind: str, work: Callable[[], Awaitable[None]]) -> None:
        """The one top-level handler of a job: whatever escapes is recorded in the proposal, never lost."""
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("planner %s job of %s crashed: %s", kind, run_id, _error_text(exc))
            await self._record_crash(repo, run_id, kind, _error_text(exc))

    async def _record_crash(self, repo: str, run_id: str, kind: str, message: str) -> None:
        try:
            if kind == KIND_DECOMPOSE:
                await self._commit(
                    repo,
                    run_id,
                    what="decompose_failed",
                    status=STATUS_INVALID,
                    meta_set={"decompose_errors": [_problem("internal_error", message)]},
                )
            else:
                await self._commit(
                    repo,
                    run_id,
                    what="create_failed",
                    status=STATUS_READY,
                    meta_set={"create_error": message},
                )
        except (PlannerError, GitHubError, OutboxOpFailed, ValueError) as exc:
            log.warning("could not record the failure of %s: %s", run_id, _error_text(exc))

    async def _commit(
        self,
        repo: str,
        run_id: str,
        *,
        what: str,
        status: str,
        proposal: ProposedPlan | None = None,
        meta_set: dict[str, Any] | None = None,
        meta_drop: tuple[str, ...] = (),
        bump: bool = False,
        validated: bool = False,
    ) -> None:
        """Stage a change made by a job and wait until it is saved."""
        async with self._lock(run_id):
            view = await self._load(repo, run_id)
            if view is None:
                return
            meta = {k: v for k, v in view.meta.items() if k not in meta_drop}
            meta.update(meta_set or {})
            new = self._stage(view, what=what, proposal=proposal, status=status, meta=meta, bump=bump)
            if validated:
                self._set_validated(run_id, new.rev)
        await self._flush(repo, run_id)

    async def start_decompose(
        self,
        repo: str,
        *,
        path: str | None,
        issue: int | None,
        ref: str | None,
        guidance: str | None,
        control_issue: int | None,
    ) -> DecomposeAccepted:
        cfg = self._known_repo(repo)
        control = control_issue if control_issue is not None else cfg.control_issue
        if control is None:
            raise refusal(
                422, "no_control_issue", "the repo has no control_issue configured: pass control_issue"
            )
        async with self._lock(f"repo:{repo}"):
            running = self._drafting(repo)
            if running is not None:
                self._spawn_decompose(repo, running)
                raise refusal(
                    409,
                    "decompose_running",
                    "a decomposition is already running for this repo",
                    proposal_id=make_id(repo, running.rsplit("-", 1)[1]),
                )
            plan = await self._load_plan(repo, path=path, issue=issue, ref=ref)
            token = secrets.token_hex(6)
            run_id = f"{repo}#planner-{token}"
            meta: dict[str, Any] = {
                "placeholder": True,
                "control_issue": control,
                "source": {"path": path, "issue": issue, "ref": ref},
                "plan": plan.source,
            }
            if guidance:
                meta["guidance"] = guidance
            try:
                async with self._write_lock:
                    await save_proposal(
                        self.store,
                        self.outbox,
                        repo=repo,
                        control_issue=control,
                        run_id=run_id,
                        plan_sha=plan.sha,
                        proposal=_placeholder(),
                        status=STATUS_DRAFTING,
                        meta=meta,
                    )
            except (GitHubError, OutboxOpFailed) as exc:
                raise PlannerError(
                    502, "the draft could not be saved to GitHub: " + _error_text(exc)
                ) from exc
            proposal_id = make_id(repo, token)
            append_event(
                self.store,
                kind=EVENT_KIND,
                repo=repo,
                data={"proposal_id": proposal_id, "status": STATUS_DRAFTING, "rev": 1, "what": "drafting"},
            )
            self._spawn(repo, run_id, KIND_DECOMPOSE, lambda: self._decompose_job(repo, run_id, plan))
        return DecomposeAccepted(proposal_id=proposal_id, job_id=f"{proposal_id}.{KIND_DECOMPOSE}")

    def _drafting(self, repo: str) -> str | None:
        """The run id of a decomposition that is not finished, if the repo has one."""
        prefix = f"{repo}#planner-"
        rows = self.store.query(
            "SELECT run_id, status FROM proposals WHERE substr(run_id, 1, ?) = ? ORDER BY id",
            (len(prefix), prefix),
        )
        for row in rows:
            marker = self._marker(row["run_id"])
            status = marker["status"] if marker else row["status"]
            if status == STATUS_DRAFTING:
                return row["run_id"]
        return None

    def _spawn_decompose(self, repo: str, run_id: str) -> None:
        """Run the decomposition of a ``drafting`` proposal again (after a restart), unless it is running."""
        if not self._alive(run_id, KIND_DECOMPOSE):
            self._spawn(repo, run_id, KIND_DECOMPOSE, lambda: self._decompose_job(repo, run_id, None))

    async def _decompose_job(self, repo: str, run_id: str, plan: PlanDoc | None) -> None:
        view = await self._load(repo, run_id)
        if view is None or view.status != STATUS_DRAFTING:
            return
        source = view.meta.get("source") or {}
        try:
            if plan is None or plan.sha != view.plan_sha:
                plan = await load_plan_doc(
                    self.gh, repo, path=source.get("path"), issue=source.get("issue"), ref=source.get("ref")
                )
            if plan.sha != view.plan_sha:
                raise PlanDocError("the plan changed after the decomposition was started; start it again")
            cfg = self._known_repo(repo)
            playbooks = await self._playbooks(repo)
            agents = self.agents(repo)
            context = await build_repo_context(self.gh, repo, cfg, plan.ref)
            proposal = await decompose(
                backend=self._backend_or_default(),
                plan_doc=plan,
                repo_context=context,
                agents=agents,
                playbooks=playbooks,
                guidance=view.meta.get("guidance"),
                max_issues=DEFAULT_MAX_ISSUES,
            )
        except DecompositionError as exc:
            errors = [{"code": p.code, "key": p.key, "message": redact(p.message)[:2000]} for p in exc.issues]
            await self._commit(
                repo,
                run_id,
                what="decompose_failed",
                status=STATUS_INVALID,
                meta_set={"decompose_errors": errors},
            )
            return
        except (PlanDocError, GitHubError, OutboxOpFailed) as exc:
            code = "plan_unreadable" if isinstance(exc, PlanDocError) else "github_error"
            await self._commit(
                repo,
                run_id,
                what="decompose_failed",
                status=STATUS_INVALID,
                meta_set={"decompose_errors": [_problem(code, _error_text(exc))]},
            )
            return
        problems = validate_proposal(proposal, agents=agents, playbooks=playbooks)
        await self._commit(
            repo,
            run_id,
            what="drafted",
            status=STATUS_INVALID if problems else STATUS_READY,
            proposal=proposal,
            meta_set={"placeholder": False},
            meta_drop=("decompose_errors",),
            bump=True,
            validated=True,
        )

    def _backend_or_default(self) -> ReviewBackend:
        if self._backend is None:
            self._backend = ClaudeSdkBackend()
        return self._backend

    # ---- proposals ---------------------------------------------------------------------------------------

    async def _issues(self, view: View) -> list[ProposalIssueOut]:
        if view.status == STATUS_DRAFTING:
            return []
        if view.placeholder:
            return [_issue_out(e) for e in view.meta.get("decompose_errors") or []]
        try:
            playbooks = await self._playbooks(view.repo)
        except GitHubError as exc:
            raise PlannerError(502, f"github: {exc.status}") from exc
        found = validate_proposal(view.proposal, agents=self.agents(view.repo), playbooks=playbooks)
        return [_issue_out(issue) for issue in found]

    async def _out(self, view: View) -> ProposalOut:
        issues = await self._issues(view)
        return ProposalOut(
            id=view.id,
            repo=view.repo,
            status=view.status,
            plan_sha=view.plan_sha,
            plan=str(view.meta.get("plan") or ""),
            rev=view.rev,
            saved=view.saved,
            save_error=view.save_error,
            proposal=None if view.placeholder else view.proposal.model_dump(mode="json"),
            issues_validation=issues,
        )

    async def read_proposal(self, proposal_id: str) -> ProposalOut:
        view = await self._need(proposal_id)
        self._resume(view)
        return await self._out(view)

    async def edit(self, proposal_id: str, body: Any, if_match: int | None) -> ProposalOut:
        """Replace the draft. Structurally broken input is a 422; a draft that fails the checks is stored."""
        try:
            proposal = ProposedPlan.model_validate(body)
        except ValidationError as exc:
            raise PlannerError(422, json.loads(exc.json(include_url=False, include_context=False))) from exc
        repo, run_id = parse_id(proposal_id)
        async with self._lock(run_id):
            view = await self._load(repo, run_id)
            if view is None:
                raise PlannerError(404, "unknown proposal")
            if view.status not in (STATUS_READY, STATUS_INVALID):
                raise refusal(
                    409, f"proposal_{view.status}", f"the proposal is {view.status}: it cannot be edited"
                )
            if if_match is not None and if_match != view.rev:
                raise PlannerError(412, f"the proposal is at rev {view.rev}, not {if_match}")
            if proposal == view.proposal and not view.placeholder:
                self._resume(view)
                return await self._out(view)
            issues = validate_proposal(
                proposal, agents=self.agents(repo), playbooks=await self._playbooks_or_502(repo)
            )
            meta = {k: v for k, v in view.meta.items() if k not in ("decompose_errors", "create_error")}
            meta["placeholder"] = False
            new = self._stage(
                view,
                what="edited",
                proposal=proposal,
                status=STATUS_INVALID if issues else STATUS_READY,
                meta=meta,
                bump=True,
            )
            self._set_validated(run_id, new.rev)
        return await self._out(new)

    async def _playbooks_or_502(self, repo: str) -> dict[str, Playbook]:
        try:
            return await self._playbooks(repo)
        except GitHubError as exc:
            raise PlannerError(502, f"github: {exc.status}") from exc

    async def validate(self, proposal_id: str) -> ValidationOut:
        """Validation issues of the current draft; nothing is written to the draft or to GitHub."""
        view = await self._need(proposal_id)
        self._resume(view)
        if view.status == STATUS_DRAFTING:
            raise refusal(409, "proposal_drafting", "the decomposition is still running")
        issues = await self._issues(view)
        self._set_validated(view.run_id, view.rev)  # a local note for approve, not part of the draft
        return ValidationOut(rev=view.rev, ok=not issues, issues=issues)

    # ---- approve and create ------------------------------------------------------------------------------

    async def approve(self, proposal_id: str, *, control_issue: int | None, rev: int | None) -> JobOut:
        repo, run_id = parse_id(proposal_id)
        async with self._lock(run_id):
            view = await self._load(repo, run_id)
            if view is None:
                raise PlannerError(404, "unknown proposal")
            if view.status in (STATUS_CREATING, STATUS_CREATED):
                self._resume(view)
                return self._job(view, KIND_CREATE)
            if view.status == STATUS_DRAFTING:
                raise refusal(409, "proposal_drafting", "the decomposition is still running")
            if view.placeholder:
                raise refusal(
                    409, "no_proposal", "the decomposition produced no proposal: edit one or decompose again"
                )
            issues = await self._issues(view)
            if issues:
                raise refusal(
                    409,
                    "validation",
                    f"the proposal has {len(issues)} problem(s)",
                    issues=[i.model_dump() for i in issues],
                )
            if rev is not None and rev != view.rev:
                raise refusal(
                    409,
                    "rev_changed",
                    f"the proposal changed since it was validated: rev {view.rev}, not {rev}",
                )
            if self._validated(run_id) != view.rev:
                raise refusal(
                    409, "not_validated", f"rev {view.rev} has not been validated: validate it, then approve"
                )
            control = control_issue or view.control_issue or None
            if control is None:
                raise refusal(422, "no_control_issue", "no control issue: pass control_issue")
            plan = await self._plan_of(view)
            if plan.sha != view.plan_sha:
                raise refusal(
                    409, "plan_changed", "the plan changed since the proposal was made: decompose it again"
                )
            approval = await self._approval(repo, control, plan)
            if approval.status is not PlanApprovalStatus.APPROVED:
                raise refusal(
                    409,
                    f"plan_{approval.status.value}",
                    f"the plan is not approved ({approval.status.value})",
                    note=approval.note,
                )
            meta = {k: v for k, v in view.meta.items() if k not in ("create_error", "result")}
            meta["materialize_control_issue"] = control
            new = self._stage(view, what="creating", status=STATUS_CREATING, meta=meta)
            self._spawn(repo, run_id, KIND_CREATE, lambda: self._create_job(repo, run_id))
        return self._job(new, KIND_CREATE)

    async def _plan_of(self, view: View) -> PlanDoc:
        source = view.meta.get("source") or {}
        return await self._load_plan(
            view.repo, path=source.get("path"), issue=source.get("issue"), ref=source.get("ref")
        )

    async def _create_job(self, repo: str, run_id: str) -> None:
        await self._flush(repo, run_id)  # the `creating` status is on GitHub before anything is created
        view = await self._load(repo, run_id)
        if view is None or view.status != STATUS_CREATING:
            return
        cfg = self._known_repo(repo)
        control = int(
            view.meta.get("materialize_control_issue") or view.control_issue or cfg.control_issue or 0
        )
        try:
            playbooks = await self._playbooks(repo)
            ctx = MaterializeContext(
                repo=repo,
                gh=self.gh,
                outbox=self.outbox,
                store=self.store,
                control_issue=control or None,
                run_key=plan_run_key(view.proposal),
                agents=sorted(self.agents(repo)),
                playbooks=sorted(playbooks),
            )
            async with self._write_lock:
                result = await self.materialize_fn(view.proposal, ctx)
        except MaterializeError as exc:
            extra: dict[str, Any] = {"create_error": _error_text(exc)}
            if exc.problems:
                extra["create_problems"] = [
                    {"code": p.code, "key": p.key, "message": redact(p.message)[:2000]} for p in exc.problems
                ]
            await self._commit(repo, run_id, what="create_failed", status=STATUS_READY, meta_set=extra)
            return
        except (GitHubError, OutboxOpFailed, ValueError) as exc:
            await self._commit(
                repo,
                run_id,
                what="create_failed",
                status=STATUS_READY,
                meta_set={"create_error": _error_text(exc)},
            )
            return
        await self._commit(
            repo,
            run_id,
            what="created",
            status=STATUS_CREATED,
            meta_set={"result": result.model_dump(mode="json")},
            meta_drop=("create_error", "create_problems"),
        )

    # ---- jobs --------------------------------------------------------------------------------------------

    def _job(self, view: View, kind: str) -> JobOut:
        job_id = f"{view.id}.{kind}"
        base = {
            "job_id": job_id,
            "kind": kind,
            "proposal_id": view.id,
            "result": None,
            "error": None,
            "issues": [],
        }
        meta = view.meta
        if kind == KIND_DECOMPOSE:
            errors = meta.get("decompose_errors") or []
            if view.status == STATUS_DRAFTING:
                return JobOut(**{**base, "status": "running"})
            if errors:
                issues = [_issue_out(e) for e in errors]
                error = "; ".join(i.message for i in issues[:3])
                return JobOut(**{**base, "status": "failed", "error": error, "issues": issues})
            return JobOut(**{**base, "status": "done"})
        if view.status == STATUS_CREATING:
            return JobOut(**{**base, "status": "running"})
        if view.status == STATUS_CREATED:
            return JobOut(**{**base, "status": "done", "result": meta.get("result")})
        if meta.get("create_error"):
            issues = [_issue_out(p) for p in meta.get("create_problems") or []]
            return JobOut(**{**base, "status": "failed", "error": meta["create_error"], "issues": issues})
        raise PlannerError(404, "unknown job")

    async def get_job(self, job_id: str) -> JobOut:
        proposal_id, kind = parse_job_id(job_id)
        view = await self._need(proposal_id)
        self._resume(view)
        return self._job(view, kind)
