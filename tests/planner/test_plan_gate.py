"""The plan gate: approval signals, the ledger record (GitHub first), staleness and the approval request."""

from datetime import UTC, datetime

import pytest

from harness.github.client import GitHub
from harness.github.ledger import render_ledger
from harness.github.outbox import Outbox, ledger_mirror_key
from harness.planner.plan_doc import PlanDoc, load_plan_doc
from harness.planner.plan_gate import (
    PlanApproval,
    PlanApprovalStatus,
    check_plan_approval,
    ledger_plan_record,
    mirrored_plan_record,
    plan_gate_outcome,
    record_plan_approval,
    request_plan_approval,
)
from harness.store.db import Store
from harness.store.repo import get_meta
from harness.sync import sync_repo
from tests.planner.world import CONTROL, PLAN_PATH, PLAN_TEXT, REPO, T_LATER, T_LATEST, World

PENDING, APPROVED, STALE = PlanApprovalStatus.PENDING, PlanApprovalStatus.APPROVED, PlanApprovalStatus.STALE


@pytest.fixture
def store():
    db = Store(":memory:", clock=lambda: datetime(2026, 3, 1, 13, 0, 0, tzinfo=UTC))
    db.migrate()
    yield db
    db.close()


@pytest.fixture
def outbox(store: Store, gh: GitHub) -> Outbox:
    return Outbox(store, gh)


async def plan_of(gh: GitHub) -> PlanDoc:
    return await load_plan_doc(gh, REPO, path=PLAN_PATH)


async def check(gh: GitHub, plan: PlanDoc, store: Store | None = None, allowlist=()) -> PlanApproval:
    return await check_plan_approval(
        gh, repo=REPO, control_issue=CONTROL, plan=plan, owner_allowlist=allowlist, store=store
    )


async def approve_and_send(gh, store, outbox, plan, approval) -> None:
    record_plan_approval(
        store,
        outbox,
        repo=REPO,
        control_issue=CONTROL,
        plan=plan,
        by=approval.approved_by or "",
        via=approval.via or "label",
        label_cursor=approval.label_cursor,
    )
    await outbox.drain_once()


# ---- the signals ----------------------------------------------------------------------------------------


async def test_no_approval_is_pending(world: World, gh: GitHub) -> None:
    world.put_file()
    plan = await plan_of(gh)
    approval = await check(gh, plan)
    assert approval.status is PENDING and approval.approved_sha is None and approval.note is None
    assert plan_gate_outcome(approval) is None


async def test_label_by_the_owner_approves_the_current_sha(world: World, gh: GitHub) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    plan = await plan_of(gh)
    approval = await check(gh, plan)
    assert approval.status is APPROVED
    assert (approval.approved_sha, approval.approved_by, approval.via) == (plan.sha, "o", "label")
    assert approval.fresh and approval.label_cursor == world.events[-1]["id"]
    assert plan_gate_outcome(approval) == "approved"


async def test_label_by_a_user_who_is_not_allowed_stays_pending_with_a_note(world: World, gh: GitHub) -> None:
    world.put_file()
    world.label_event("mallory", at=T_LATER)
    approval = await check(gh, await plan_of(gh), allowlist=["alice"])
    assert approval.status is PENDING and not approval.fresh
    assert approval.note is not None and "@mallory" in approval.note and "not allowed" in approval.note


async def test_an_allowlisted_user_may_approve_and_logins_are_case_insensitive(
    world: World, gh: GitHub
) -> None:
    world.put_file()
    world.label_event("Alice", at=T_LATER)
    approval = await check(gh, await plan_of(gh), allowlist=["alice"])
    assert approval.status is APPROVED and approval.approved_by == "Alice"


async def test_the_owner_counts_even_when_an_allowlist_is_set(world: World, gh: GitHub) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    assert (await check(gh, await plan_of(gh), allowlist=["alice"])).status is APPROVED


async def test_a_label_from_before_the_last_plan_change_does_not_approve_it(world: World, gh: GitHub) -> None:
    world.put_file(date=T_LATER)
    world.label_event("o", at="2026-03-01T09:00:00Z")
    approval = await check(gh, await plan_of(gh))
    assert approval.status is PENDING and approval.note is not None and "changed after" in approval.note


async def test_a_removed_label_does_not_approve(world: World, gh: GitHub) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    world.unlabel_event("o")
    assert (await check(gh, await plan_of(gh))).status is PENDING


async def test_merged_pr_that_last_modified_the_file_approves_via_pr(world: World, gh: GitHub) -> None:
    world.put_file()
    world.merge_pr(5, by="o")
    plan = await plan_of(gh)
    approval = await check(gh, plan)
    assert approval.status is APPROVED and approval.via == "pr" and approval.approved_by == "o"
    assert approval.approved_sha == plan.sha and approval.fresh


async def test_pr_merged_by_someone_not_allowed_is_ignored_with_a_note(world: World, gh: GitHub) -> None:
    world.put_file()
    world.merge_pr(5, by="mallory")
    approval = await check(gh, await plan_of(gh))
    assert approval.status is PENDING and approval.note is not None and "PR #5" in approval.note


async def test_a_direct_push_or_unmerged_pr_is_no_approval(world: World, gh: GitHub) -> None:
    world.put_file()
    plan = await plan_of(gh)
    assert (await check(gh, plan)).status is PENDING  # no PR at all
    world.pulls[6] = {"number": 6, "merged_at": None, "merged_by": None}
    world.commit_pulls[world.files[PLAN_PATH].commit_sha] = [6]
    assert (await check(gh, plan)).status is PENDING  # a PR that was not merged


# ---- the record: GitHub first, then the mirror --------------------------------------------------------------


async def test_record_writes_the_ledger_first_and_the_store_mirror_after_it(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    plan = await plan_of(gh)
    approval = await check(gh, plan, store)
    record_plan_approval(
        store,
        outbox,
        repo=REPO,
        control_issue=CONTROL,
        plan=plan,
        by="o",
        via="label",
        label_cursor=approval.label_cursor,
    )
    # Queued, but neither GitHub nor the local mirror has it yet.
    assert world.ledger() is None
    assert mirrored_plan_record(store, REPO, CONTROL) is None
    assert (await check(gh, plan, store)).fresh  # still just a signal, not yet a record

    await outbox.drain_once()
    record = world.ledger()["plan"]  # type: ignore[index]
    assert (record["plan_sha"], record["approved_by"], record["via"]) == (plan.sha, "o", "label")
    assert (record["kind"], record["path"], record["issue"], record["title"]) == (
        "file",
        PLAN_PATH,
        None,
        "The plan",
    )
    assert PLAN_TEXT not in world.bodies()[0]  # only the hash is in the ledger, not the plan text
    assert mirrored_plan_record(store, REPO, CONTROL) == record
    assert (await ledger_plan_record(gh, REPO, CONTROL)) == record
    again = await check(gh, plan, store)
    assert again.status is APPROVED and not again.fresh and again.via == "label"


async def test_record_keeps_the_lock_and_the_summary_already_in_the_ledger(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    lock = {"host": "h", "pid": 1, "token": "t", "since": "x", "heartbeat": "y"}
    world.comments[CONTROL] = [
        {
            "id": 7,
            "body": render_ledger({"lock": lock, "other": [1]}, "Harness run lock: held."),
            "created_at": "2026-03-01T00:00:00Z",
        }
    ]
    plan = await plan_of(gh)
    record_plan_approval(store, outbox, repo=REPO, control_issue=CONTROL, plan=plan, by="o", via="cli")
    await outbox.drain_once()
    snapshot = world.ledger()
    assert snapshot is not None
    assert snapshot["lock"] == lock and snapshot["other"] == [1] and snapshot["plan"]["via"] == "cli"
    assert "Harness run lock: held." in world.bodies()[0]
    assert len(world.comments[CONTROL]) == 1  # edited in place, no second ledger


async def test_record_also_drops_the_needs_you_label_once(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    world.labels.add("harness:needs-you")
    record_plan_approval(
        store, outbox, repo=REPO, control_issue=CONTROL, plan=await plan_of(gh), by="o", via="cli"
    )
    await outbox.drain_once()
    assert "harness:needs-you" not in world.labels


async def test_recording_twice_before_it_is_sent_queues_one_write(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    plan = await plan_of(gh)
    for _ in range(3):
        record_plan_approval(store, outbox, repo=REPO, control_issue=CONTROL, plan=plan, by="o", via="cli")
    await outbox.drain_once()
    assert world.count("PATCH", "") + world.count("POST", f"/issues/{CONTROL}/comments") == 1


async def test_wiping_the_store_then_syncing_restores_the_approval(
    world: World, gh: GitHub, outbox: Outbox, store: Store
) -> None:
    world.put_file()
    world.put_file(".harness/config.yaml", f"control_issue: {CONTROL}\n")
    world.label_event("o", at=T_LATER)
    plan = await plan_of(gh)
    await approve_and_send(gh, store, outbox, plan, await check(gh, plan, store))
    assert (await check(gh, plan, store)).status is APPROVED
    store.close()

    wiped = Store(":memory:")
    wiped.migrate()
    assert get_meta(wiped, ledger_mirror_key(REPO, CONTROL, "plan")) is None
    report = await sync_repo(gh, wiped, REPO)
    assert report.errors == []
    mirror = mirrored_plan_record(wiped, REPO, CONTROL)
    assert mirror is not None and mirror["plan_sha"] == plan.sha and mirror["approved_by"] == "o"
    approval = await check(gh, plan, wiped)
    assert approval.status is APPROVED and approval.approved_by == "o" and not approval.fresh
    wiped.close()


async def test_a_wiped_store_without_a_sync_still_finds_the_approval_in_the_ledger(
    world: World, gh: GitHub, outbox: Outbox, store: Store
) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    plan = await plan_of(gh)
    await approve_and_send(gh, store, outbox, plan, await check(gh, plan, store))
    fresh_store = Store(":memory:")
    fresh_store.migrate()
    assert (await check(gh, plan, fresh_store)).status is APPROVED
    assert mirrored_plan_record(fresh_store, REPO, CONTROL) is not None  # the read refilled the mirror
    fresh_store.close()


async def test_sync_clears_a_mirror_the_ledger_does_not_have(world: World, gh: GitHub, store: Store) -> None:
    world.put_file(".harness/config.yaml", f"control_issue: {CONTROL}\n")
    store.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        (ledger_mirror_key(REPO, CONTROL, "plan"), '{"plan_sha": "abc"}'),
    )
    await sync_repo(gh, store, REPO)
    assert mirrored_plan_record(store, REPO, CONTROL) is None


async def test_sync_without_a_control_issue_leaves_the_plan_mirror_alone(
    world: World, gh: GitHub, store: Store
) -> None:
    key = ledger_mirror_key(REPO, CONTROL, "plan")
    store.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, '{"plan_sha": "abc"}'))
    report = await sync_repo(gh, store, REPO)  # no .harness/config.yaml in the world: 404 -> defaults
    assert report.errors == [] and get_meta(store, key) == '{"plan_sha": "abc"}'


async def test_a_ledger_without_a_plan_key_is_no_record(world: World, gh: GitHub) -> None:
    world.comments[CONTROL] = [
        {
            "id": 7,
            "body": render_ledger({"lock": {"host": "h"}}, "lock"),
            "created_at": "2026-03-01T00:00:00Z",
        }
    ]
    assert await ledger_plan_record(gh, REPO, CONTROL) is None


# ---- stale: the plan changed after it was approved ------------------------------------------------------


async def test_a_changed_file_makes_the_approval_stale_and_the_used_label_does_not_revive_it(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    plan = await plan_of(gh)
    await approve_and_send(gh, store, outbox, plan, await check(gh, plan, store))

    world.put_file(text=PLAN_TEXT + "\n## Added later\n", version=2, date=T_LATEST)
    changed = await plan_of(gh)
    assert changed.sha != plan.sha
    approval = await check(gh, changed, store)
    assert approval.status is STALE
    assert approval.approved_sha == plan.sha and approval.approved_by == "o"
    assert plan_gate_outcome(approval) is None  # the gate reopens


async def test_after_a_stale_plan_a_new_label_event_approves_the_new_sha(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    plan = await plan_of(gh)
    await approve_and_send(gh, store, outbox, plan, await check(gh, plan, store))
    world.put_file(text=PLAN_TEXT + "\nmore\n", version=2, date=T_LATER)
    changed = await plan_of(gh)
    assert (await check(gh, changed, store)).status is STALE

    world.unlabel_event("o")
    new_event = world.label_event("o", at=T_LATEST)
    approval = await check(gh, changed, store)
    assert approval.status is APPROVED and approval.fresh and approval.approved_sha == changed.sha
    assert approval.label_cursor == new_event


async def test_after_a_stale_plan_a_merged_pr_for_the_new_text_approves_it(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    world.merge_pr(5)
    plan = await plan_of(gh)
    await approve_and_send(gh, store, outbox, plan, await check(gh, plan, store))
    assert (await check(gh, plan, store)).via == "pr"

    world.put_file(text=PLAN_TEXT + "\nmore\n", version=2, date=T_LATEST)  # a direct push
    changed = await plan_of(gh)
    assert (await check(gh, changed, store)).status is STALE
    world.merge_pr(8, by="o")  # the new text arrived through a PR merged by the owner
    approval = await check(gh, changed, store)
    assert approval.status is APPROVED and approval.via == "pr" and approval.fresh


async def test_a_plan_from_another_source_is_not_covered_by_the_record(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    plan = await plan_of(gh)
    await approve_and_send(gh, store, outbox, plan, await check(gh, plan, store))
    world.put_issue(12, "Other plan", "body")
    other = await load_plan_doc(gh, REPO, issue=12)
    assert (await check(gh, other, store)).status is STALE


async def test_an_issue_plan_goes_stale_when_the_issue_body_is_edited(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_issue(12, "Campaign", "# Plan\n\nPhase 1\n")
    world.label_event("o", at=T_LATER)
    plan = await load_plan_doc(gh, REPO, issue=12)
    approval = await check(gh, plan, store)
    assert approval.status is APPROVED and approval.fresh
    await approve_and_send(gh, store, outbox, plan, approval)
    assert (await check(gh, plan, store)).status is APPROVED
    assert world.count("GET", "/commits") == 0  # issue plans have no file history to look at

    world.issues[12]["body"] = "# Plan\n\nPhase 1\n\nPhase 2 (added after approval)\n"
    edited = await load_plan_doc(gh, REPO, issue=12)
    assert edited.sha != plan.sha
    stale = await check(gh, edited, store)
    assert stale.status is STALE and stale.approved_sha == plan.sha

    world.issues[12]["body"] = "# Plan\n\nPhase 1\n"  # edited back: the old approval fits again
    assert (await check(gh, await load_plan_doc(gh, REPO, issue=12), store)).status is APPROVED


async def test_an_issue_plan_has_no_pr_signal(world: World, gh: GitHub) -> None:
    world.put_issue(12, "Campaign", "body")
    approval = await check(gh, await load_plan_doc(gh, REPO, issue=12))
    assert approval.status is PENDING
    assert world.count("GET", "/commits") == 0


def test_gate_outcome_mapping() -> None:
    assert plan_gate_outcome(PlanApproval(APPROVED)) == "approved"
    assert plan_gate_outcome(PlanApproval(PlanApprovalStatus.REJECTED)) == "rejected"
    assert plan_gate_outcome(PlanApproval(PENDING)) is None
    assert plan_gate_outcome(PlanApproval(STALE)) is None


# ---- request_plan_approval ------------------------------------------------------------------------------


def label_posts(world: World) -> int:
    return world.count("POST", f"/issues/{CONTROL}/labels")


async def test_request_is_idempotent_per_sha(world: World, gh: GitHub, store: Store, outbox: Outbox) -> None:
    world.put_file()
    plan = await plan_of(gh)
    assert await request_plan_approval(gh, outbox, repo=REPO, control_issue=CONTROL, plan=plan) is True
    assert await request_plan_approval(gh, outbox, repo=REPO, control_issue=CONTROL, plan=plan) is False
    await outbox.drain_once()
    assert await request_plan_approval(gh, outbox, repo=REPO, control_issue=CONTROL, plan=plan) is False
    await outbox.drain_once()

    assert len(world.bodies()) == 1 and label_posts(world) == 1
    body = world.bodies()[0]
    assert body.startswith(f"**2026-03-01 · #{CONTROL} plan approval requested.**")
    assert PLAN_PATH in body and plan.sha[:12] in body and "harness:plan-approved" in body
    assert "harness:needs-you" in world.labels


async def test_request_posts_again_for_a_new_sha(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    first = await plan_of(gh)
    await request_plan_approval(gh, outbox, repo=REPO, control_issue=CONTROL, plan=first)
    await outbox.drain_once()
    world.put_file(text=PLAN_TEXT + "\nmore\n", version=2)
    second = await plan_of(gh)
    assert await request_plan_approval(gh, outbox, repo=REPO, control_issue=CONTROL, plan=second) is True
    await outbox.drain_once()
    assert len(world.bodies()) == 2 and label_posts(world) == 2
    assert first.sha[:12] in world.bodies()[0] and second.sha[:12] in world.bodies()[1]


async def test_request_does_not_repost_after_the_store_is_wiped(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    world.put_file()
    plan = await plan_of(gh)
    await request_plan_approval(gh, outbox, repo=REPO, control_issue=CONTROL, plan=plan)
    await outbox.drain_once()
    wiped = Store(":memory:")
    wiped.migrate()
    assert (
        await request_plan_approval(gh, Outbox(wiped, gh), repo=REPO, control_issue=CONTROL, plan=plan)
        is False
    )
    assert len(world.bodies()) == 1
    wiped.close()
