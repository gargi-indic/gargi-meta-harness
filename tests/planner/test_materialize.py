"""materialize: request shapes, idempotency, crash resume, ordering, throttling, control table, dry run (respx only)."""

import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from harness.engine.dispatcher import parse_path_hints
from harness.github.client import GitHub
from harness.github.control_table import parse_table
from harness.github.ledger import render_event
from harness.github.outbox import Outbox, OutboxOpFailed, tick_boxes
from harness.github.planned import EPIC_KEY, index_planned, marker_of, neutralise, planned_marker
from harness.planner.materialize import (
    MaterializeContext,
    MaterializeError,
    materialize,
    plan_run_key,
    render_issue_body,
    topological_order,
)
from harness.planner.proposal import ProposedIssue, ProposedPlan
from harness.store.db import Store
from tests.planner.gh_fake import CONTROL, CONTROL_BODY, REPO, Crash, PlannerWorld

RUN = "run1"


class Clock:
    """A fake monotonic clock; ``sleep`` moves it, so waiting is instant and measurable."""

    def __init__(self) -> None:
        self.time = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.time += seconds


def issue(key: str, *deps: str, **over: Any) -> ProposedIssue:
    data: dict[str, Any] = {
        "key": key,
        "title": f"Do {key}",
        "body": f"## Context\nWhy {key}.\n\n## Scope\nWhat {key}.",
        "acceptance": [f"{key} works", f"{key} is tested"],
        "blocked_by": list(deps),
        "agent": "jules",
        "playbook": "plan-build-review",
    }
    data.update(over)
    return ProposedIssue(**data)


def six() -> ProposedPlan:
    return ProposedPlan(
        epic_title="The epic",
        epic_body="Epic body.",
        issues=[
            issue("schema", paths=["db/", "db/schema.sql"], labels=["area:db"]),
            issue("api", "schema"),
            issue("ui", "api"),
            issue("docs"),
            issue("tests", "api", "ui"),
            issue("release", "tests", "docs", agent="codex", playbook="implement-only"),
        ],
    )


class Rig:
    """One 'process': a GitHub client, an outbox and a context over a shared store and fake GitHub."""

    def __init__(self, world: PlannerWorld, store: Store, clock: Clock, **ctx: Any) -> None:
        self.world, self.store, self.clock = world, store, clock
        self.gh = GitHub("t", write_spacing=0.0, sleep=clock.sleep, clock=clock)
        self.outbox = Outbox(store, self.gh, clock=clock, sleep=clock.sleep)
        self.ctx = MaterializeContext(
            **{
                "repo": REPO,
                "gh": self.gh,
                "outbox": self.outbox,
                "store": store,
                "control_issue": None,
                "run_key": RUN,
                **ctx,
            }
        )

    async def run(self, plan: ProposedPlan, **kw: Any):
        return await materialize(plan, self.ctx, **kw)

    async def close(self) -> None:
        await self.gh.aclose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def world(respx_mock, clock: Clock) -> PlannerWorld:
    w = PlannerWorld(respx_mock, now=clock).install()
    return w


@pytest.fixture
def store() -> Store:
    s = Store(":memory:", clock=lambda: datetime(2026, 3, 1, 9, 30, tzinfo=UTC))
    s.migrate()
    return s


@pytest.fixture
async def rig(world: PlannerWorld, store: Store, clock: Clock):
    r = Rig(world, store, clock)
    yield r
    await r.close()


def creation_titles(world: PlannerWorld) -> list[str]:
    return [w.body["title"] for w in world.writes_to("POST", "/issues")]


# ---- request shapes ---------------------------------------------------------------------------------------


async def test_six_issue_plan_request_shapes_and_call_counts(rig: Rig, world: PlannerWorld) -> None:
    result = await rig.run(six())

    assert len(world.writes_to("POST", "/labels")) == 9
    assert len(world.writes_to("POST", "/issues")) == 7  # the epic and six issues
    assert len(world.writes_to("POST", "/sub_issues")) == 6
    assert len(world.writes_to("POST", "/dependencies/blocked_by")) == 6  # api 1, ui 1, tests 2, release 2
    assert len(world.writes) == 9 + 7 + 6 + 6  # nothing else is written

    labels = {w.body["name"]: w.body for w in world.writes_to("POST", "/labels")}
    assert set(labels) == {
        "epic",
        "harness:planned",
        "harness:ready",
        "harness:blocked",
        "agent:jules",
        "agent:codex",
        "playbook:plan-build-review",
        "playbook:implement-only",
        "area:db",  # the proposal's own label is ensured too
    }
    assert labels["harness:ready"]["color"] == "0e8a16" and labels["epic"]["color"] == "3e4b9e"

    epic = world.by_title("The epic")
    assert [lab["name"] for lab in epic["labels"]] == ["epic", "harness:planned"]
    assert epic["body"] == f"Epic body.\n\n{planned_marker(RUN, EPIC_KEY)}"

    schema = world.by_title("Do schema")
    assert schema["body"] == (
        "## Context\nWhy schema.\n\n## Scope\nWhat schema.\n\n"
        "## Acceptance criteria\n\n- [ ] schema works\n- [ ] schema is tested\n\n"
        "paths: db/, db/schema.sql\n\n"
        f"{planned_marker(RUN, 'schema')}"
    )
    assert [lab["name"] for lab in schema["labels"]] == [
        "harness:planned",
        "harness:ready",
        "agent:jules",
        "playbook:plan-build-review",
        "area:db",
    ]
    assert [lab["name"] for lab in world.by_title("Do release")["labels"]] == [
        "harness:planned",
        "harness:blocked",
        "agent:codex",
        "playbook:implement-only",
    ]

    attached = [w.body for w in world.writes_to("POST", "/sub_issues")]
    assert all(set(b) == {"sub_issue_id"} for b in attached)
    epic_number = result.epic_number
    assert world.sub_issues[epic_number] == [world.by_title(f"Do {k}")["id"] for k in result.issues]
    api, tests = world.by_title("Do api"), world.by_title("Do tests")
    assert world.writes_to("POST", "/dependencies/blocked_by")[0].body == {
        "issue_id": world.by_title("Do schema")["id"]
    }
    assert world.blocked_by[api["number"]] == [world.by_title("Do schema")["id"]]
    assert world.blocked_by[tests["number"]] == [api["id"], world.by_title("Do ui")["id"]]

    assert result.issues == {k: world.by_title(f"Do {k}")["number"] for k in result.issues}
    assert set(result.issues) == {"schema", "api", "ui", "docs", "tests", "release"}
    assert sorted(result.created) == sorted(result.issues) and result.reused == []
    assert result.links_created == 12
    assert len(result.labels_ensured) == 9


async def test_existing_labels_are_not_created_again(rig: Rig, world: PlannerWorld) -> None:
    for name in ("epic", "harness:planned", "agent:jules", "Harness:Ready"):
        world.labels[name] = {"name": name}
    result = await rig.run(six())
    created = {w.body["name"] for w in world.writes_to("POST", "/labels")}
    assert "epic" not in created and "harness:ready" not in created  # label names are case-insensitive
    assert set(result.labels_ensured) == created


# ---- ordering and labels ----------------------------------------------------------------------------------


def test_topological_order_puts_blockers_first_and_keeps_plan_order() -> None:
    plan = ProposedPlan(
        epic_title="E",
        epic_body="b",
        issues=[issue("c", "b"), issue("b", "a"), issue("d"), issue("a"), issue("e", "c", "d")],
    )
    assert topological_order(plan) == ["d", "a", "b", "c", "e"]


async def test_issues_are_created_blockers_first_with_ready_or_blocked_labels(
    rig: Rig, world: PlannerWorld
) -> None:
    plan = ProposedPlan(
        epic_title="E",
        epic_body="b",
        issues=[issue("c", "b"), issue("b", "a"), issue("a"), issue("d")],
    )
    await rig.run(plan)
    assert creation_titles(world) == ["E", "Do a", "Do d", "Do b", "Do c"]
    for key, expected in {"a": "ready", "d": "ready", "b": "blocked", "c": "blocked"}.items():
        names = [lab["name"] for lab in world.by_title(f"Do {key}")["labels"]]
        assert f"harness:{expected}" in names
        assert f"harness:{'blocked' if expected == 'ready' else 'ready'}" not in names


async def test_label_prefix_is_used(world: PlannerWorld, store: Store, clock: Clock) -> None:
    rig = Rig(world, store, clock, label_prefix="gm")
    await rig.run(ProposedPlan(epic_title="E", epic_body="b", issues=[issue("a")]))
    await rig.close()
    names = [lab["name"] for lab in world.by_title("Do a")["labels"]]
    assert names[:2] == ["gm:planned", "gm:ready"]
    # the marker search used the prefixed label: a second run finds everything
    again = Rig(world, store, clock, label_prefix="gm")
    n = len(world.writes)
    await again.run(ProposedPlan(epic_title="E", epic_body="b", issues=[issue("a")]))
    await again.close()
    assert len(world.writes) == n


# ---- idempotency ------------------------------------------------------------------------------------------


async def test_a_complete_rerun_makes_no_write_call(rig: Rig, world: PlannerWorld, store: Store) -> None:
    first = await rig.run(six())
    writes, rows = len(world.writes), store.query("SELECT COUNT(*) AS n FROM outbox")[0]["n"]

    again = Rig(world, store, rig.clock)  # a new process, same GitHub
    second = await again.run(six())
    await again.close()

    assert len(world.writes) == writes
    assert store.query("SELECT COUNT(*) AS n FROM outbox")[0]["n"] == rows  # nothing was even queued
    assert second.epic_number == first.epic_number and second.issues == first.issues
    assert second.created == [] and sorted(second.reused) == sorted(first.issues)
    assert second.links_created == 0 and second.labels_ensured == []


async def test_a_rerun_needs_no_local_state(world: PlannerWorld, clock: Clock, store: Store) -> None:
    first_rig = Rig(world, store, clock)
    first = await first_rig.run(six())
    await first_rig.close()
    writes = len(world.writes)

    fresh = Store(":memory:")
    fresh.migrate()
    second_rig = Rig(world, fresh, clock)
    second = await second_rig.run(six())
    await second_rig.close()
    assert len(world.writes) == writes and second.issues == first.issues


async def test_another_run_key_is_a_different_materialization(
    world: PlannerWorld, store: Store, clock: Clock
) -> None:
    one, two = Rig(world, store, clock, run_key="r1"), Rig(world, store, clock, run_key="r2")
    plan = ProposedPlan(epic_title="E", epic_body="b", issues=[issue("a")])
    first, second = await one.run(plan), await two.run(plan)
    await one.close()
    await two.close()
    assert first.issues["a"] != second.issues["a"] and first.epic_number != second.epic_number


async def test_resume_after_a_crash_following_the_third_issue(
    rig: Rig, world: PlannerWorld, store: Store, clock: Clock
) -> None:
    world.crash_after("POST", "/issues", 3)  # the epic, schema, then docs exist; the answer for docs is lost
    with pytest.raises(Crash):
        await rig.run(six())
    assert len(world.created()) == 3
    assert world.sub_issues == {} and world.blocked_by == {}

    world.crash_at = None
    again = Rig(world, store, clock)  # same database: the unsent rows of the dead process are still queued
    result = await again.run(six())
    await again.close()

    titles = [i["title"] for i in world.created()]
    assert sorted(titles) == sorted(["The epic", *(f"Do {k}" for k in result.issues)])  # no duplicates
    assert len(titles) == 7
    for key in ("schema", "docs"):
        marker = planned_marker(RUN, key)
        assert sum(1 for i in world.issues.values() if i["body"].rstrip().endswith(marker)) == 1
    assert set(result.reused) == {"schema", "docs"} and len(result.created) == 4
    assert len(world.writes_to("POST", "/issues")) == 7
    assert (
        len(world.writes_to("POST", "/sub_issues")) == 6
        and len(world.writes_to("POST", "/dependencies/blocked_by")) == 6
    )
    assert sorted(world.sub_issues[result.epic_number]) == sorted(
        i["id"] for i in world.created() if i["title"] != "The epic"
    )
    assert result.links_created == 12


async def test_resume_with_a_new_database_after_a_crash(rig: Rig, world: PlannerWorld, clock: Clock) -> None:
    world.crash_after("POST", "/issues", 3)
    with pytest.raises(Crash):
        await rig.run(six())
    world.crash_at = None

    fresh = Store(":memory:")
    fresh.migrate()
    again = Rig(world, fresh, clock)
    result = await again.run(six())
    await again.close()
    assert len(world.created()) == 7 and len(world.writes_to("POST", "/issues")) == 7
    assert result.links_created == 12


async def test_resume_after_a_crash_in_the_middle_of_the_links(
    rig: Rig, world: PlannerWorld, store: Store, clock: Clock
) -> None:
    world.crash_after("POST", "/sub_issues", 3)
    with pytest.raises(Crash):
        await rig.run(six())
    world.crash_at = None
    again = Rig(world, store, clock)
    result = await again.run(six())
    await again.close()

    assert len(world.writes_to("POST", "/issues")) == 7  # none created twice
    assert len(world.sub_issues[result.epic_number]) == 6
    assert len(set(world.sub_issues[result.epic_number])) == 6
    assert sum(len(v) for v in world.blocked_by.values()) == 6
    assert result.created == [] and len(result.reused) == 6
    assert result.links_created == 12 - 3  # three sub-issue links were already there


async def test_links_already_on_github_are_not_added_again(rig: Rig, world: PlannerWorld) -> None:
    result = await rig.run(six())
    epic = world.issues[result.epic_number]
    # someone removes one dependency and one sub-issue link by hand
    api = world.by_title("Do api")
    world.blocked_by[api["number"]] = []
    world.sub_issues[epic["number"]].remove(world.by_title("Do docs")["id"])
    before = len(world.writes)

    again = Rig(world, rig.store, rig.clock)
    second = await again.run(six())
    await again.close()
    assert len(world.writes) == before + 2 and second.links_created == 2


# ---- the outbox is the only path --------------------------------------------------------------------------


def _forbid_direct_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every write path of the client to fail when the direct caller is materialize.py."""
    blocked: list[str] = []

    def guard(name: str, original: Any) -> Any:
        async def wrapper(self: GitHub, *args: Any, **kwargs: Any) -> Any:
            caller = sys._getframe(1).f_globals.get("__name__")
            if caller == "harness.planner.materialize" and (
                name != "request" or str(args[0] if args else kwargs.get("method")).upper() != "GET"
            ):
                blocked.append(name)
                raise AssertionError(f"materialize called GitHub.{name} directly")
            return await original(self, *args, **kwargs)

        return wrapper

    for name in (
        "create_issue",
        "update_issue",
        "add_comment",
        "edit_comment",
        "add_labels",
        "remove_label",
        "ensure_label",
        "add_sub_issue",
        "add_blocked_by",
        "post",
        "patch",
        "put",
        "delete",
        "request",
    ):
        monkeypatch.setattr(GitHub, name, guard(name, getattr(GitHub, name)))
    return blocked


async def test_materialize_never_calls_a_client_write_method_itself(
    world: PlannerWorld, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = _forbid_direct_writes(monkeypatch)
    rig = Rig(world, store, clock, control_issue=CONTROL)
    await rig.run(six())
    await rig.close()
    assert blocked == [] and len(world.writes) > 20  # the writes happened, through the outbox


async def test_the_guard_does_catch_a_direct_write(
    world: PlannerWorld, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = _forbid_direct_writes(monkeypatch)
    rig = Rig(world, store, clock)
    # code whose module is materialize, calling a write method directly
    namespace: dict[str, Any] = {"__name__": "harness.planner.materialize", "gh": rig.gh}
    exec("async def direct():\n    await gh.create_issue('o/r', 'x')", namespace)
    with pytest.raises(AssertionError, match="directly"):
        await namespace["direct"]()
    await rig.close()
    assert blocked == ["create_issue"] and world.writes == []


# ---- throttling -------------------------------------------------------------------------------------------


async def test_sixty_issues_respect_the_outbox_rate_with_a_fake_clock(
    world: PlannerWorld, store: Store, clock: Clock
) -> None:
    gh = GitHub("t", write_spacing=0.5, sleep=clock.sleep, clock=clock)
    outbox = Outbox(store, gh, clock=clock, sleep=clock.sleep)  # per_minute=40
    ctx = MaterializeContext(
        repo=REPO, gh=gh, outbox=outbox, store=store, control_issue=None, run_key=RUN, max_issues=60
    )
    plan = ProposedPlan(epic_title="E", epic_body="b", issues=[issue(f"i{n:02d}") for n in range(60)])

    result = await materialize(plan, ctx)
    await gh.aclose()

    assert len(result.created) == 60 and len(world.writes) > 120
    times = [w.at for w in world.writes]
    for start in times:
        assert sum(1 for t in times if start <= t < start + 60) <= outbox.per_minute
    assert all(b - a >= 0.5 for a, b in zip(times, times[1:], strict=False))  # the client's own spacing
    assert times[-1] - times[0] >= (len(times) / outbox.per_minute - 1) * 60 - 1  # it did wait for the window
    assert clock.slept and max(clock.slept) > 1


# ---- control issue ----------------------------------------------------------------------------------------


async def test_control_table_rows_and_event_comment(world: PlannerWorld, store: Store, clock: Clock) -> None:
    rig = Rig(world, store, clock, control_issue=CONTROL)
    result = await rig.run(six())

    body = world.issues[CONTROL]["body"]
    assert body.startswith(
        "Control issue.\n\nIntro text.\n\nOutro text.\n"
    )  # prose outside the markers stays
    rows = {r.issue: r for r in parse_table(body)}
    assert set(rows) == set(result.issues.values())
    for key, number in result.issues.items():
        row = rows[number]
        assert row.title == f"Do {key}" and row.agent == ("codex" if key == "release" else "jules")
        assert row.status == ("ready" if key in ("schema", "docs") else "blocked")

    bullets = [f"`{k}` -> #{n}" for k, n in zip(result.issues, result.issues.values(), strict=True)]
    assert [c["body"] for c in world.comments[CONTROL]] == [
        render_event("2026-03-01", result.epic_number, "planned", bullets)
    ]
    assert world.comments[CONTROL][0]["body"].startswith(
        f"**2026-03-01 · #{result.epic_number} planned.**\n- "
    )

    n = len(world.writes)
    await rig.run(six())  # same rig: everything is in place
    await rig.close()
    assert len(world.writes) == n


async def test_without_a_control_issue_nothing_is_written_to_it(rig: Rig, world: PlannerWorld) -> None:
    await rig.run(six())
    assert world.issues[CONTROL]["body"] == CONTROL_BODY and CONTROL not in world.comments
    assert not [w for w in world.writes if w.path.endswith(f"/issues/{CONTROL}")]


async def test_missing_control_rows_are_added_on_a_rerun(
    world: PlannerWorld, store: Store, clock: Clock
) -> None:
    first = Rig(world, store, clock, control_issue=CONTROL)
    result = await first.run(six())
    await first.close()
    world.issues[CONTROL]["body"] = CONTROL_BODY  # the table was wiped
    world.comments.clear()

    second = Rig(world, store, clock, control_issue=CONTROL)
    await second.run(six())
    await second.close()
    assert len(parse_table(world.issues[CONTROL]["body"])) == 6
    assert len(world.comments[CONTROL]) == 1
    assert {r.issue for r in parse_table(world.issues[CONTROL]["body"])} == set(result.issues.values())


# ---- dry run ----------------------------------------------------------------------------------------------


async def test_dry_run_writes_and_queues_nothing(rig: Rig, world: PlannerWorld, store: Store) -> None:
    result = await rig.run(six(), dry_run=True)
    assert world.writes == []
    assert store.query("SELECT COUNT(*) AS n FROM outbox")[0]["n"] == 0
    assert result.epic_number == 0 and result.issues == {}
    assert sorted(result.created) == sorted(i.key for i in six().issues) and result.reused == []
    assert result.links_created == 12 and len(result.labels_ensured) == 9


async def test_dry_run_after_a_partial_run_reports_what_is_left(
    rig: Rig, world: PlannerWorld, store: Store, clock: Clock
) -> None:
    world.crash_after("POST", "/issues", 3)
    with pytest.raises(Crash):
        await rig.run(six())
    world.crash_at = None
    writes = len(world.writes)
    again = Rig(world, store, clock)
    result = await again.run(six(), dry_run=True)
    await again.close()
    assert len(world.writes) == writes
    assert set(result.reused) == {"schema", "docs"} and len(result.created) == 4
    assert result.epic_number == world.by_title("The epic")["number"]


async def test_dry_run_of_a_finished_plan_has_nothing_to_do(rig: Rig, world: PlannerWorld) -> None:
    await rig.run(six())
    result = await rig.run(six(), dry_run=True)
    assert result.created == [] and result.links_created == 0 and result.labels_ensured == []


# ---- refusal ----------------------------------------------------------------------------------------------


async def test_an_invalid_plan_is_refused_before_anything_is_written(rig: Rig, world: PlannerWorld) -> None:
    cyclic = ProposedPlan(epic_title="E", epic_body="b", issues=[issue("a", "b"), issue("b", "a")])
    with pytest.raises(MaterializeError, match="cycle") as err:
        await rig.run(cyclic)
    assert [p.code for p in err.value.problems] == ["cycle"]
    assert world.writes == [] and world.reads == []


async def test_agents_and_playbooks_are_checked_when_the_context_names_them(
    world: PlannerWorld, store: Store, clock: Clock
) -> None:
    rig = Rig(world, store, clock, agents=["jules"], playbooks=["plan-build-review"])
    with pytest.raises(MaterializeError) as err:
        await rig.run(six())
    await rig.close()
    assert {p.code for p in err.value.problems} == {"unknown_agent", "unknown_playbook"}
    assert world.writes == []


async def test_too_many_issues_are_refused(rig: Rig, world: PlannerWorld) -> None:
    plan = ProposedPlan(epic_title="E", epic_body="b", issues=[issue(f"i{n}") for n in range(41)])
    with pytest.raises(MaterializeError, match="too_many_issues"):
        await rig.run(plan)


async def test_a_bad_run_key_is_refused(world: PlannerWorld, store: Store, clock: Clock) -> None:
    rig = Rig(world, store, clock, run_key="a b")
    with pytest.raises(ValueError, match="run key"):
        await rig.run(six())
    await rig.close()


async def test_a_path_hint_with_a_comma_is_refused(rig: Rig, world: PlannerWorld) -> None:
    plan = ProposedPlan(epic_title="E", epic_body="b", issues=[issue("a", paths=["x,y"])])
    with pytest.raises(MaterializeError, match="path hint"):
        await rig.run(plan)
    assert world.writes == []


async def test_a_failing_op_stops_the_run_and_a_rerun_continues(
    world: PlannerWorld, store: Store, clock: Clock
) -> None:
    rig = Rig(world, store, clock)
    rig.gh._max_retries = 0
    plan = ProposedPlan(epic_title="E", epic_body="b", issues=[issue("a")])
    world.down.add(("POST", "/labels"))
    with pytest.raises(OutboxOpFailed, match="ensure_label"):
        await rig.run(plan)
    assert world.created() == []  # nothing was created on top of a failed step

    world.down.clear()  # GitHub recovers; the queued op is reset by the new run's coalesce key
    result = await rig.run(plan)
    await rig.close()
    assert len(world.created()) == 2 and result.created == ["a"]


# ---- the body format --------------------------------------------------------------------------------------


def test_paths_line_round_trips_through_the_dispatcher_parser() -> None:
    paths = ["harness/github/", "harness/planner/materialize.py", "tests/**/test_x.py"]
    body = render_issue_body(issue("a", paths=paths), RUN)
    assert parse_path_hints(body) == paths
    assert parse_path_hints(render_issue_body(issue("a"), RUN)) == []


def test_acceptance_criteria_are_unchecked_boxes_that_the_merge_executor_ticks() -> None:
    body = render_issue_body(issue("a", acceptance=["first", "second\nline two"]), RUN)
    assert "## Acceptance criteria\n\n- [ ] first\n- [ ] second line two\n" in body
    ticked = tick_boxes(body)
    assert "- [x] first" in ticked and "- [x] second line two" in ticked and "[ ]" not in ticked
    assert ticked.endswith(planned_marker(RUN, "a"))  # the marker is untouched


def test_an_acceptance_section_in_the_proposed_body_is_replaced_by_the_list() -> None:
    proposed = (
        "## Context\nc\n\n## Acceptance criteria\n- old one\n- old two\n\n"
        "```\n## Acceptance criteria\n```\n\n## Out of scope\n- o"
    )
    body = render_issue_body(issue("a", body=proposed, acceptance=["new"]), RUN)
    assert "old one" not in body and body.count("## Acceptance criteria") == 1
    assert "- [ ] new" in body and "## Out of scope\n- o" in body
    kept = render_issue_body(
        issue("a", body="## Acceptance criteria\n```\n## x\n```\nmore\n## After\nz"), RUN
    )
    assert "## After\nz" in kept and "more" not in kept  # a fenced heading does not end the section


def test_the_marker_ends_the_body_and_a_forged_one_in_the_text_is_defused() -> None:
    forged = f"text {planned_marker('other', 'a')}\n<!--   harness:planned run=other key=b -->"
    body = render_issue_body(issue("a", body=forged), RUN)
    assert marker_of(body) == (RUN, "a") and body.endswith(planned_marker(RUN, "a"))
    assert body.count("<!-- harness:planned") == 1 and "<!--   harness:planned" not in body
    assert neutralise("<!-- other -->") == "<!-- other -->"


def test_index_planned_matches_the_run_and_ignores_markers_that_are_not_last() -> None:
    mine = {"number": 3, "id": 30, "body": f"x\n\n{planned_marker('r', 'a')}\n"}
    older = {"number": 2, "id": 20, "body": f"x\n\n{planned_marker('r', 'a')}"}
    other = {"number": 4, "id": 40, "body": planned_marker("r2", "b")}
    quoted = {"number": 5, "id": 50, "body": f"{planned_marker('r', 'c')}\nmore text"}
    assert index_planned([mine, older, other, quoted], "r") == {"a": older}


def test_plan_run_key_depends_on_the_content_only() -> None:
    assert plan_run_key(six()) == plan_run_key(six()) and plan_run_key(six()).startswith("plan-")
    other = six().model_copy(update={"epic_title": "Another"})
    assert plan_run_key(other) != plan_run_key(six())
