import json
import logging
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from harness.adapters.base import AgentAdapter, AgentState, SessionRef, StartRequest
from harness.adapters.jules import JulesAdapter, JulesError

pytestmark = pytest.mark.respx(
    assert_all_called=True, assert_all_mocked=True, base_url="https://jules.googleapis.com/v1alpha"
)

S = AgentState
API_KEY = "jules-secret-key-0123456789"
REPO = "o/r"
SID = "1234567890"
PR_URL = "https://github.com/o/r/pull/42"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakePrHead:
    def __init__(self, sha: str = "a1") -> None:
        self.sha = sha
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, repo: str, number: int) -> str:
        self.calls.append((repo, number))
        return self.sha


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pr_head() -> FakePrHead:
    return FakePrHead()


@pytest.fixture
def adapter(clock: FakeClock, pr_head: FakePrHead) -> JulesAdapter:
    return JulesAdapter(API_KEY, max_concurrency=3, clock=clock, pr_head=pr_head)


def make_request(key: str = "k1", mode: str = "plan_first") -> StartRequest:
    return StartRequest(
        repo=REPO,
        base_branch="main",
        branch="harness/18-jules",
        issue=18,
        title="Jules adapter",
        brief="Build the adapter.",
        mode=mode,  # type: ignore[arg-type]
        idempotency_key=key,
    )


def make_ref(**data: Any) -> SessionRef:
    return SessionRef(
        agent="jules", id=SID, url="https://jules.google.com/session/1", data={"repo": REPO, **data}
    )


def session_json(state: str, *, pr: bool = False, title: str = "t", sid: str = SID) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": f"sessions/{sid}",
        "id": sid,
        "title": title,
        "state": state,
        "url": f"https://jules.google.com/session/{sid}",
        "sourceContext": {"source": f"sources/github/{REPO}"},
    }
    if pr:
        body["outputs"] = [{"pullRequest": {"url": PR_URL, "title": "The PR"}}]
    return body


def progress(act_id: str, title: str = "Working", description: str = "") -> dict[str, Any]:
    return {
        "name": f"sessions/{SID}/activities/{act_id}",
        "id": act_id,
        "createTime": f"2026-01-01T00:00:{act_id[-2:]}Z",
        "originator": "agent",
        "progressUpdated": {"title": title, "description": description},
    }


def message(act_id: str, text: str) -> dict[str, Any]:
    return {
        "id": act_id,
        "createTime": f"2026-01-01T00:00:{act_id[-2:]}Z",
        "originator": "agent",
        "agentMessaged": {"agentMessage": text},
    }


def plan(act_id: str, *steps: tuple[str, str]) -> dict[str, Any]:
    return {
        "id": act_id,
        "createTime": f"2026-01-01T00:00:{act_id[-2:]}Z",
        "originator": "agent",
        "planGenerated": {
            "plan": {
                "id": "plan-1",
                "steps": [
                    {"id": f"s{i}", "index": i, "title": title, "description": description}
                    for i, (title, description) in enumerate(steps)
                ],
            }
        },
    }


def mock_session(router: respx.MockRouter, body: dict[str, Any]) -> respx.Route:
    return router.get(f"sessions/{SID}").mock(return_value=Response(200, json=body))


def mock_activities(router: respx.MockRouter, *pages: list[dict[str, Any]]) -> respx.Route:
    """Serve the pages in order; page ``i`` is requested with ``pageToken=p<i>`` (none for the first)."""

    def respond(request: httpx.Request) -> Response:
        assert request.url.params["pageSize"] == "100"
        token = request.url.params.get("pageToken")
        index = int(token[1:]) if token else 0
        body: dict[str, Any] = {"activities": pages[index]}
        if index + 1 < len(pages):
            body["nextPageToken"] = f"p{index + 1}"
        return Response(200, json=body)

    return router.get(f"sessions/{SID}/activities").mock(side_effect=respond)


def body_of(route: respx.Route, call: int = -1) -> Any:
    return json.loads(route.calls[call].request.content)


def test_is_an_agent_adapter(adapter: JulesAdapter) -> None:
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "jules"
    assert adapter.caps.model_dump() == {
        "plan_approval": True,
        "messaging": True,
        "opens_pr": True,
        "local_workspace": False,
        "max_concurrency": 3,
    }
    assert adapter.caps.names() == {"plan", "message", "pr"}


def test_repr_hides_the_api_key(adapter: JulesAdapter) -> None:
    assert API_KEY not in repr(adapter)
    assert API_KEY not in str(adapter)


# start / find


@pytest.mark.parametrize(("mode", "requires"), [("plan_first", True), ("direct", False)])
async def test_start_posts_the_session(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, mode: str, requires: bool
) -> None:
    route = respx_mock.post("sessions").mock(
        return_value=Response(200, json=session_json("QUEUED", title="#18: Jules adapter [h:k1]"))
    )
    ref = await adapter.start(make_request(mode=mode))

    assert body_of(route) == {
        "title": "#18: Jules adapter [h:k1]",
        "prompt": "Build the adapter.",
        "sourceContext": {"source": "sources/github/o/r", "githubRepoContext": {"startingBranch": "main"}},
        "automationMode": "AUTO_CREATE_PR",
        "requirePlanApproval": requires,
    }
    assert route.calls[0].request.headers["X-Goog-Api-Key"] == API_KEY
    assert ref == SessionRef(
        agent="jules",
        id=SID,
        url=f"https://jules.google.com/session/{SID}",
        data={"repo": REPO, "last_activity_id": None},
    )


async def test_find_matches_the_title_tag_across_two_pages(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    def respond(request: httpx.Request) -> Response:
        assert request.url.params["pageSize"] == "100"
        if request.url.params.get("pageToken") == "next":
            sessions = [
                session_json("COMPLETED", title="#20: Other [h:k10]", sid="3"),
                session_json("IN_PROGRESS", title="#18: Jules adapter [h:k1]", sid="4"),
            ]
            return Response(200, json={"sessions": sessions})
        sessions = [
            session_json("COMPLETED", title="#18: Jules adapter [h:k1-old]", sid="1"),
            session_json("COMPLETED", title="no tag at all", sid="2"),
        ]
        return Response(200, json={"sessions": sessions, "nextPageToken": "next"})

    route = respx_mock.get("sessions").mock(side_effect=respond)
    ref = await adapter.find("k1")

    assert route.call_count == 2
    assert ref is not None
    assert (ref.agent, ref.id) == ("jules", "4")
    assert ref.data == {"repo": REPO, "last_activity_id": None}


async def test_find_returns_none_when_no_title_matches(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    route = respx_mock.get("sessions").mock(
        return_value=Response(200, json={"sessions": [session_json("COMPLETED", title="x [h:other]")]})
    )
    assert await adapter.find("k1") is None
    assert route.call_count == 1


async def test_find_with_no_sessions_at_all(respx_mock: respx.MockRouter, adapter: JulesAdapter) -> None:
    respx_mock.get("sessions").mock(return_value=Response(200, json={}))
    assert await adapter.find("k1") is None


# poll: state mapping


@pytest.mark.parametrize(
    ("jules_state", "expected", "poll_after"),
    [
        ("QUEUED", S.STARTING, 60),
        ("PLANNING", S.PLANNING, 60),
        ("IN_PROGRESS", S.WORKING, 60),
        ("PAUSED", S.NEEDS_INPUT, 0),
        ("FAILED", S.FAILED, 0),
    ],
)
async def test_state_mapping(
    respx_mock: respx.MockRouter,
    adapter: JulesAdapter,
    jules_state: str,
    expected: AgentState,
    poll_after: float,
) -> None:
    mock_session(respx_mock, session_json(jules_state))
    mock_activities(respx_mock, [])
    snapshot = await adapter.poll(make_ref())
    assert snapshot.state == expected
    assert snapshot.poll_after == poll_after


async def test_awaiting_plan_approval_is_plan_ready_with_the_latest_plan(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    mock_session(respx_mock, session_json("AWAITING_PLAN_APPROVAL"))
    mock_activities(
        respx_mock,
        [plan("a01", ("Old", "stale plan")), progress("a02")],
        [plan("a03", ("Add client", "httpx wrapper"), ("Add tests", "respx fixtures")), progress("a04")],
    )
    snapshot = await adapter.poll(make_ref())
    assert snapshot.state is S.PLAN_READY
    assert snapshot.plan == "- Add client: httpx wrapper\n- Add tests: respx fixtures"
    assert snapshot.question is None
    assert snapshot.poll_after == 0


async def test_awaiting_user_feedback_is_needs_input_with_the_latest_question(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    mock_session(respx_mock, session_json("AWAITING_USER_FEEDBACK"))
    mock_activities(
        respx_mock,
        [message("a01", "first question"), progress("a02"), message("a03", "Which base branch?")],
    )
    snapshot = await adapter.poll(make_ref())
    assert snapshot.state is S.NEEDS_INPUT
    assert snapshot.question == "Which base branch?"


async def test_completed_with_a_pr_is_pr_ready(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, pr_head: FakePrHead
) -> None:
    pr_head.sha = "deadbeef"
    mock_session(respx_mock, session_json("COMPLETED", pr=True))
    mock_activities(respx_mock, [])
    snapshot = await adapter.poll(make_ref())
    assert snapshot.state is S.PR_READY
    assert snapshot.pr is not None
    assert (snapshot.pr.number, snapshot.pr.url, snapshot.pr.head_sha) == (42, PR_URL, "deadbeef")
    assert pr_head.calls == [(REPO, 42)]
    assert snapshot.poll_after == 0


async def test_pr_head_sha_is_empty_without_a_pr_head_callable(respx_mock: respx.MockRouter) -> None:
    mock_session(respx_mock, session_json("COMPLETED", pr=True))
    mock_activities(respx_mock, [])
    snapshot = await JulesAdapter(API_KEY).poll(make_ref())
    assert snapshot.pr is not None
    assert (snapshot.pr.number, snapshot.pr.head_sha) == (42, "")


async def test_completed_without_a_pr_is_failed(respx_mock: respx.MockRouter, adapter: JulesAdapter) -> None:
    mock_session(respx_mock, session_json("COMPLETED"))
    mock_activities(respx_mock, [])
    snapshot = await adapter.poll(make_ref())
    assert snapshot.state is S.FAILED
    assert snapshot.error == "completed without PR"
    assert snapshot.pr is None


async def test_failed_reports_the_failure_reason(respx_mock: respx.MockRouter, adapter: JulesAdapter) -> None:
    mock_session(respx_mock, session_json("FAILED"))
    mock_activities(
        respx_mock, [{"id": "a01", "createTime": "t", "sessionFailed": {"reason": "tests would not pass"}}]
    )
    snapshot = await adapter.poll(make_ref())
    assert snapshot.state is S.FAILED
    assert snapshot.error == "tests would not pass"


async def test_repo_is_filled_in_from_the_session_when_missing(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    mock_session(respx_mock, session_json("IN_PROGRESS"))
    mock_activities(respx_mock, [])
    ref = SessionRef(agent="jules", id=SID, data={})
    await adapter.poll(ref)
    assert ref.data["repo"] == REPO


# poll: activity


async def test_activity_pagination_and_last_seen_tracking(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    page1 = [progress("a01", "Reading", "the repo"), message("a02", "Hello"), plan("a03", ("Step", "do it"))]
    page2 = [
        {"id": "a04", "createTime": "t4", "originator": "user", "userMessaged": {"userMessage": "ignored"}},
        {"id": "a05", "createTime": "2026-01-01T00:00:05Z", "sessionCompleted": {}},
    ]
    mock_session(respx_mock, session_json("IN_PROGRESS"))
    route = mock_activities(respx_mock, page1, page2)
    ref = make_ref(last_activity_id=None)

    first = await adapter.poll(ref)
    assert route.call_count == 2
    assert [(a.kind, a.text) for a in first.activity] == [
        ("progress", "Reading: the repo"),
        ("message", "Hello"),
        ("plan", "- Step: do it"),
        ("end", "session completed"),
    ]
    assert first.activity[0].ts == "2026-01-01T00:00:01Z"
    assert ref.data["last_activity_id"] == "a05"

    second = await adapter.poll(ref)
    assert second.activity == []
    assert route.call_count == 4
    assert ref.data["last_activity_id"] == "a05"


async def test_only_activities_after_the_last_seen_id_are_new(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    mock_session(respx_mock, session_json("IN_PROGRESS"))
    mock_activities(
        respx_mock,
        [progress("a01"), progress("a02", "Second")],
        [progress("a03", "Third"), {"id": "a04", "createTime": "t", "sessionFailed": {"reason": "boom"}}],
    )
    ref = make_ref(last_activity_id="a02")
    snapshot = await adapter.poll(ref)
    assert [(a.kind, a.text) for a in snapshot.activity] == [
        ("progress", "Third"),
        ("end", "session failed: boom"),
    ]
    assert ref.data["last_activity_id"] == "a04"


# quirk (b): stale state after approve


async def test_stale_awaiting_plan_approval_is_working_for_120_seconds_after_approve(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, clock: FakeClock
) -> None:
    approve = respx_mock.post(f"sessions/{SID}:approvePlan").mock(return_value=Response(200, json={}))
    mock_session(respx_mock, session_json("AWAITING_PLAN_APPROVAL"))
    mock_activities(respx_mock, [plan("a01", ("Do", "it"))])
    ref = make_ref()

    await adapter.approve_plan(ref)
    assert body_of(approve) == {}
    assert ref.data["approved_at"] == 1000.0

    clock.now += 119
    stale = await adapter.poll(ref)
    assert stale.state is S.WORKING
    assert stale.plan is None
    assert stale.poll_after == 60

    clock.now += 1
    fresh = await adapter.poll(ref)
    assert fresh.state is S.PLAN_READY
    assert fresh.plan == "- Do: it"


# quirk (a): re-asks for approval


async def test_reask_gets_one_automatic_reply_then_needs_input(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, clock: FakeClock
) -> None:
    mock_session(respx_mock, session_json("AWAITING_USER_FEEDBACK"))
    mock_activities(respx_mock, [message("a01", "Does this plan look good to you?")])
    sent = respx_mock.post(f"sessions/{SID}:sendMessage").mock(return_value=Response(200, json={}))
    ref = make_ref(approved_at=clock.now)

    clock.now += 30
    first = await adapter.poll(ref)
    assert first.state is S.WORKING
    assert first.question is None
    assert body_of(sent) == {"prompt": "Approved, proceed."}
    assert ref.data["auto_replied"] is True

    clock.now += 30
    second = await adapter.poll(ref)
    assert second.state is S.NEEDS_INPUT
    assert second.question == "Does this plan look good to you?"
    assert sent.call_count == 1


async def test_auto_reply_state_survives_a_new_adapter_instance(
    respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    mock_session(respx_mock, session_json("AWAITING_USER_FEEDBACK"))
    mock_activities(respx_mock, [message("a01", "Shall I proceed?")])
    sent = respx_mock.post(f"sessions/{SID}:sendMessage").mock(return_value=Response(200, json={}))
    ref = make_ref(approved_at=clock.now)

    assert (await JulesAdapter(API_KEY, clock=clock).poll(ref)).state is S.WORKING
    persisted = SessionRef.model_validate_json(ref.model_dump_json())

    restarted = JulesAdapter(API_KEY, clock=clock)
    assert (await restarted.poll(persisted)).state is S.NEEDS_INPUT
    assert sent.call_count == 1


async def test_reask_after_the_window_is_needs_input_without_a_reply(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, clock: FakeClock
) -> None:
    mock_session(respx_mock, session_json("AWAITING_USER_FEEDBACK"))
    mock_activities(respx_mock, [message("a01", "Does this plan look good?")])
    ref = make_ref(approved_at=clock.now)
    clock.now += 601
    assert (await adapter.poll(ref)).state is S.NEEDS_INPUT
    assert "auto_replied" not in ref.data


@pytest.mark.parametrize(
    ("approved", "text"),
    [(True, "Which base branch should I use?"), (False, "Does this plan look good?")],
    ids=["unrelated-question", "never-approved"],
)
async def test_no_auto_reply_for_ordinary_questions(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, clock: FakeClock, approved: bool, text: str
) -> None:
    mock_session(respx_mock, session_json("AWAITING_USER_FEEDBACK"))
    mock_activities(respx_mock, [message("a01", text)])
    ref = make_ref(approved_at=clock.now) if approved else make_ref()
    snapshot = await adapter.poll(ref)
    assert snapshot.state is S.NEEDS_INPUT
    assert snapshot.question == text


# quirk (c): PR fixes


async def test_pr_ready_after_send_waits_for_a_new_head_and_an_idle_session(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, pr_head: FakePrHead
) -> None:
    session = respx_mock.get(f"sessions/{SID}")
    mock_activities(respx_mock, [])
    sent = respx_mock.post(f"sessions/{SID}:sendMessage").mock(return_value=Response(200, json={}))
    ref = make_ref()

    # send on a session with a PR records the head sha at that moment.
    session.mock(return_value=Response(200, json=session_json("COMPLETED", pr=True)))
    pr_head.sha = "a1"
    await adapter.send(ref, "Please fix the tests.")
    assert body_of(sent) == {"prompt": "Please fix the tests."}
    assert ref.data["fix_base_head"] == "a1"

    # Jules has not started yet: COMPLETED and the head unchanged.
    waiting = await adapter.poll(ref)
    assert waiting.state is S.WORKING
    assert ref.data["fix_base_head"] == "a1"

    # Busy and the head already moved: still WORKING.
    pr_head.sha = "b2"
    session.mock(return_value=Response(200, json=session_json("IN_PROGRESS", pr=True)))
    assert (await adapter.poll(ref)).state is S.WORKING
    assert ref.data["fix_base_head"] == "a1"

    # Head moved and Jules is idle: PR_READY with the new sha, and the field is cleared.
    session.mock(return_value=Response(200, json=session_json("COMPLETED", pr=True)))
    done = await adapter.poll(ref)
    assert done.state is S.PR_READY
    assert done.pr is not None
    assert done.pr.head_sha == "b2"
    assert ref.data["fix_base_head"] is None

    # Afterwards PR_READY is reported normally again.
    assert (await adapter.poll(ref)).state is S.PR_READY


async def test_pr_fix_round_does_not_hide_attention_states(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    mock_session(respx_mock, session_json("AWAITING_USER_FEEDBACK", pr=True))
    mock_activities(respx_mock, [message("a01", "Which test should I fix first?")])
    snapshot = await adapter.poll(make_ref(fix_base_head="a1"))
    assert snapshot.state is S.NEEDS_INPUT


async def test_send_on_a_session_without_a_pr_records_nothing(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, pr_head: FakePrHead
) -> None:
    mock_session(respx_mock, session_json("AWAITING_PLAN_APPROVAL"))
    respx_mock.post(f"sessions/{SID}:sendMessage").mock(return_value=Response(200, json={}))
    ref = make_ref()
    await adapter.send(ref, "Change the plan.")
    assert "fix_base_head" not in ref.data
    assert pr_head.calls == []


async def test_send_without_pr_head_skips_the_session_lookup(respx_mock: respx.MockRouter) -> None:
    sent = respx_mock.post(f"sessions/{SID}:sendMessage").mock(return_value=Response(200, json={}))
    ref = make_ref()
    await JulesAdapter(API_KEY).send(ref, "hello")
    assert sent.call_count == 1
    assert "fix_base_head" not in ref.data


# approve_plan / send / cancel


async def test_approve_plan_with_a_note_sends_it_after_approving(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    approve = respx_mock.post(f"sessions/{SID}:approvePlan").mock(return_value=Response(200, json={}))
    mock_session(respx_mock, session_json("AWAITING_PLAN_APPROVAL"))
    sent = respx_mock.post(f"sessions/{SID}:sendMessage").mock(return_value=Response(200, json={}))
    await adapter.approve_plan(make_ref(), "Also add a docstring.")
    assert approve.call_count == 1
    assert body_of(sent) == {"prompt": "Also add a docstring."}
    assert respx_mock.calls[0].request.url.path.endswith(":approvePlan")


async def test_cancel_archives_the_session(respx_mock: respx.MockRouter, adapter: JulesAdapter) -> None:
    archive = respx_mock.post(f"sessions/{SID}:archive").mock(return_value=Response(200, json={}))
    await adapter.cancel(make_ref())
    assert archive.call_count == 1


async def test_cancel_treats_a_missing_session_as_done_but_raises_other_errors(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    archive = respx_mock.post(f"sessions/{SID}:archive")
    archive.mock(return_value=Response(404, json={"error": {"message": "not found"}}))
    await adapter.cancel(make_ref())
    archive.mock(return_value=Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(JulesError) as info:
        await adapter.cancel(make_ref())
    assert info.value.status == 500


# errors never leak the key


async def test_4xx_error_does_not_leak_the_api_key(
    respx_mock: respx.MockRouter, adapter: JulesAdapter, caplog: pytest.LogCaptureFixture
) -> None:
    # Even if the server echoes the key back, it must be redacted.
    respx_mock.post("sessions").mock(
        return_value=Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": f"API key {API_KEY} is not valid",
                    "status": "PERMISSION_DENIED",
                }
            },
        )
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(JulesError) as info:
        await adapter.start(make_request())

    error = info.value
    assert (error.status, error.method, error.path) == (403, "POST", "sessions")
    assert error.message == "API key [redacted] is not valid"
    assert str(error) == "[403] POST sessions: API key [redacted] is not valid"
    for text in (str(error), repr(error), error.message, repr(error.args), repr(adapter), caplog.text):
        assert API_KEY not in text
    assert API_KEY not in "".join(str(a) for a in error.args)


async def test_non_json_error_body_is_used_as_the_message(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    respx_mock.get(f"sessions/{SID}").mock(return_value=Response(502, text="bad gateway"))
    with pytest.raises(JulesError, match=r"\[502\] GET sessions/1234567890: bad gateway"):
        await adapter.poll(make_ref())


async def test_transport_errors_are_wrapped_and_redacted(
    respx_mock: respx.MockRouter, adapter: JulesAdapter
) -> None:
    respx_mock.get(f"sessions/{SID}").mock(
        side_effect=httpx.ConnectError(f"cannot reach host with {API_KEY}")
    )
    with pytest.raises(JulesError) as info:
        await adapter.poll(make_ref())
    assert info.value.status == 0
    assert API_KEY not in str(info.value)
    assert "ConnectError" in info.value.message
