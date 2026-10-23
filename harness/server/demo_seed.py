"""A small seeded campaign for the server tests (M2.2; M2.6-M2.10 reuse it).

Repo ``acme/widgets`` with control issue #1 and issues #2-#7:

====  ======================  ==========  ==========================================================
#     labels / state          derived     what it has
====  ======================  ==========  ==========================================================
2     closed, harness:done    done
3     open, blocked_by [2]    ready       blocker is closed
4     open, blocked_by [3]    blocked     open blocker
5     harness:running         running     run A (plan-build-review), plan_gate waiting with a gate row
6     harness:needs-you       needs-you   run B, escalate waiting after an agent asked for input, PR 42
7     agent:fake              ready       no run
====  ======================  ==========  ==========================================================

Run A also has five ``agent_activity`` events. Written through the store repository functions only (no engine),
with a fake clock, so the content is deterministic.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from harness.config import GlobalConfig, RepoConfig, RepoEntry
from harness.playbook.load import builtin_playbooks
from harness.store.db import Store
from harness.store.repo import (
    GateRow,
    IssueRow,
    RunRow,
    StepRow,
    append_event,
    insert_gate,
    insert_run,
    insert_step,
    update_step,
    upsert_issue,
    upsert_repo,
)

REPO = "acme/widgets"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Seed:
    repo: str
    control_issue: int
    run_a: str
    run_b: str
    plan_gate_step: str
    plan_gate_id: str
    escalate_step: str
    activity_ids: tuple[int, ...]


def seed_config() -> GlobalConfig:
    return GlobalConfig(repos=[RepoEntry(name=REPO)], env_files=[])


def ts(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(number: int, title: str, **fields: Any) -> IssueRow:
    values: dict[str, Any] = {
        "repo": REPO,
        "number": number,
        "id": 1000 + number,
        "title": title,
        "body": None,
        "state": "open",
        "labels": [],
        "parent": None,
        "blocked_by": [],
        "updated_at": ts(number),
    }
    values.update(fields)
    return IssueRow(**values)


def _step(run_id: str, node: str, visit: int, status: str, minute: int, **fields: Any) -> StepRow:
    done = status == "done"
    values: dict[str, Any] = {
        "id": f"{run_id}/{node}/{visit}",
        "run_id": run_id,
        "node_id": node,
        "visit": visit,
        "status": status,
        "outcome": None,
        "ref": None,
        "output": None,
        "poll_after": None,
        "started_at": ts(minute),
        "ended_at": ts(minute) if done else None,
    }
    values.update(fields)
    return StepRow(**values)


def _run(run_id: str, issue: int, status: str, minute: int, context: dict[str, Any]) -> RunRow:
    return RunRow(
        id=run_id,
        repo=REPO,
        issue=issue,
        playbook_id="plan-build-review",
        playbook_sha=None,
        status=status,
        context=context,
        ledger_comment_id=None,
        created_at=ts(minute),
        updated_at=ts(minute + 9),
    )


def seed_store(store: Store) -> Seed:
    real_clock = store.clock
    ticks = iter(range(10_000))
    store.clock = lambda: BASE + timedelta(hours=1, seconds=next(ticks))
    try:
        return _seed(store)
    finally:
        store.clock = real_clock


def _seed(store: Store) -> Seed:
    upsert_repo(store, REPO, "main", RepoConfig(control_issue=1).model_dump(), synced_at=ts(0))
    for issue in (
        _issue(1, "Campaign control"),
        _issue(2, "Add the widget model", state="closed", labels=["harness:done"]),
        _issue(3, "Widget CRUD endpoints", blocked_by=[2]),
        _issue(4, "Widget search", blocked_by=[3]),
        _issue(5, "Widget export", labels=["harness:running"]),
        _issue(6, "Widget import", labels=["harness:needs-you"]),
        _issue(7, "Widget docs", labels=["agent:fake"]),
    ):
        upsert_issue(store, issue)

    playbook = builtin_playbooks()["plan-build-review"].model_dump(mode="json")

    run_a = f"{REPO}#5@1"
    insert_run(
        store,
        _run(
            run_a, 5, "needs-you", 10, {"agent": "jules", "playbook": playbook, "visits": {"plan_review": 2}}
        ),
    )
    gate_step = f"{run_a}/plan_gate/1"
    gate_id = f"{gate_step}/gate"
    for step in (
        _step(run_a, "plan", 1, "done", 11, outcome="plan_ready"),
        _step(run_a, "plan_review", 1, "done", 12, outcome="changes"),
        _step(run_a, "revise_plan", 1, "done", 13, outcome="plan_ready"),
        _step(run_a, "plan_review", 2, "done", 14, outcome="approve"),
        _step(run_a, "plan_gate", 1, "pending", 15),
    ):
        insert_step(store, step)
    update_step(
        store,
        gate_step,
        status="waiting",
        ref={"gate_id": gate_id},
        output={"needs_you": True, "reason": "Approve the plan for #5", "gate_id": gate_id},
    )
    insert_gate(
        store,
        GateRow(
            id=gate_id,
            run_id=run_a,
            step_id=gate_step,
            name="plan",
            status="pending",
            note=None,
            decided_by=None,
            created_at=ts(15),
            decided_at=None,
        ),
    )

    run_b = f"{REPO}#6@1"
    insert_run(
        store,
        _run(
            run_b,
            6,
            "needs-you",
            20,
            {"agent": "jules", "playbook": playbook, "visits": {}, "pr": {"number": 42}},
        ),
    )
    findings = [
        {
            "severity": "major",
            "file": "widgets/importer.py",
            "problem": "No size limit",
            "required_change": "Cap it",
        },
        {"severity": "minor", "file": None, "problem": "Missing docstring", "required_change": "Add one"},
    ]
    failed_checks = [
        {"name": "build", "conclusion": "failure", "url": "https://ci.example/build"},
        {"name": "lint", "conclusion": "cancelled", "url": "https://ci.example/lint"},
    ]
    for step in (
        _step(run_b, "implement", 1, "done", 21, outcome="pr_ready"),
        _step(
            run_b,
            "ci",
            1,
            "done",
            22,
            outcome="fail",
            output={"failed": failed_checks, "correction": "fix CI"},
        ),
        _step(run_b, "pr_review", 1, "done", 23, outcome="changes", output={"findings": findings}),
        _step(run_b, "fix", 1, "done", 24, outcome="needs_input"),
        _step(
            run_b,
            "escalate",
            1,
            "waiting",
            25,
            output={"needs_you": True, "reason": "Escalated after fix: which format?"},
        ),
    ):
        insert_step(store, step)

    activity = [
        ("progress", "Planning started"),
        ("message", "Read the importer module"),
        ("command", "pytest -q"),
        ("plan", "Plan: add a size limit"),
        ("surprise", "Unclassified note"),
    ]
    activity_ids = tuple(
        append_event(
            store,
            kind="agent_activity",
            data={"step": f"{run_a}/plan/1", "ts": ts(30 + i), "kind": kind, "text": text},
            repo=REPO,
            issue=5,
            run_id=run_a,
        )
        for i, (kind, text) in enumerate(activity)
    )

    return Seed(
        repo=REPO,
        control_issue=1,
        run_a=run_a,
        run_b=run_b,
        plan_gate_step=gate_step,
        plan_gate_id=gate_id,
        escalate_step=f"{run_b}/escalate/1",
        activity_ids=activity_ids,
    )
