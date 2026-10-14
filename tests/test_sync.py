import json
from datetime import UTC, datetime

import pytest
from httpx import Response

from harness.dag import ready_set
from harness.github.client import GitHub
from harness.store.db import Store
from harness.store.repo import get_issue, get_repo, list_issues
from harness.sync import sync_repo

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)


class FakeClock:
    def __init__(self):
        self.time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        from datetime import timedelta

        self.time += timedelta(seconds=1)
        return self.time


@pytest.fixture
def store():
    db = Store(":memory:", clock=FakeClock())
    db.migrate()
    yield db
    db.close()


@pytest.fixture
def gh():
    async def no_op_sleep(delay):
        pass

    # Passing sleep=no_op_sleep directly to GitHub constructor as requested
    client = GitHub("mock-token", write_spacing=0.0, sleep=no_op_sleep)
    return client


@pytest.mark.asyncio
async def test_sync_full_repo(gh, store, respx_mock):
    repo = "owner/repo"

    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(200, text="escalate:\n  labels: [needs-discussion]\n")
    )

    issues_resp = [
        {
            "number": 1,
            "id": 101,
            "title": "Epic",
            "state": "open",
            "labels": [],
            "updated_at": "2023-01-01T00:00:00Z",
        },
        {
            "number": 2,
            "id": 102,
            "title": "Sub 1",
            "state": "open",
            "labels": [],
            "updated_at": "2023-01-01T00:00:00Z",
        },
        {
            "number": 3,
            "id": 103,
            "title": "Sub 2",
            "state": "open",
            "labels": [],
            "updated_at": "2023-01-01T00:00:00Z",
        },
        {
            "number": 4,
            "id": 104,
            "title": "Sub 3",
            "state": "open",
            "labels": [{"name": "needs-discussion"}],
            "updated_at": "2023-01-01T00:00:00Z",
        },
        {
            "number": 5,
            "id": 105,
            "title": "Sub 4",
            "state": "open",
            "labels": [{"name": "harness:ready"}],
            "updated_at": "2023-01-01T00:00:00Z",
        },
        {
            "number": 6,
            "id": 106,
            "title": "PR",
            "state": "open",
            "pull_request": {},
            "updated_at": "2023-01-01T00:00:00Z",
        },
    ]
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues?state=all&per_page=100").mock(
        return_value=Response(200, json=issues_resp)
    )

    # Epic
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/1/parent").mock(return_value=Response(404))
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/1/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )
    # Sub 1
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/2/parent").mock(
        return_value=Response(200, json={"number": 1})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/2/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )
    # Sub 2
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/3/parent").mock(
        return_value=Response(200, json={"number": 1})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/3/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[{"number": 2}])
    )
    # Sub 3
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/4/parent").mock(
        return_value=Response(200, json={"number": 1})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/4/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )
    # Sub 4
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/5/parent").mock(
        return_value=Response(200, json={"number": 1})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/5/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )

    # Ledger
    ledger_state = {"test": 123}
    ledger_text = f"<!-- harness:ledger -->\nSummary\n\n<!-- harness:state v1 {json.dumps(ledger_state)} -->"
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/5/comments?per_page=100").mock(
        return_value=Response(
            200, json=[{"id": 1, "body": ledger_text, "created_at": "2023-01-01T00:00:00Z"}]
        )
    )

    # Actually wait, sync_repo will take start_time early on, which is the clock at that exact moment

    report = await sync_repo(gh, store, repo, full=True)

    assert report.issues_synced == 5
    assert report.ledgers == {5: ledger_state}
    assert get_issue(store, repo, 6) is None  # PR not inserted

    repo_row = get_repo(store, repo)
    assert repo_row is not None
    assert repo_row["config"]["escalate"]["labels"] == ["needs-discussion"]

    # Check that synced_at was the start_time (which is less than current clock time because of FakeClock advancing)
    assert repo_row["synced_at"] < store.now()

    # Check ready_set
    issues = list_issues(store, repo)
    config = report.config
    ready = ready_set(issues, set(), config.escalate.labels)
    # 1 is epic (has children), 2 is ready, 3 is blocked by 2, 4 has needs-discussion, 5 is ready
    assert set(ready) == {2, 5}


@pytest.mark.asyncio
async def test_sync_missing_config(gh, store, respx_mock):
    repo = "owner/repo"
    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(404)
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues?state=all&per_page=100").mock(
        return_value=Response(200, json=[])
    )

    report = await sync_repo(gh, store, repo, full=True)
    assert report.config.escalate.labels == ["needs-discussion"]  # Default
    assert len(report.errors) == 0


@pytest.mark.asyncio
async def test_sync_invalid_config(gh, store, respx_mock):
    repo = "owner/repo"
    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(200, text="invalid: [yaml")
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues?state=all&per_page=100").mock(
        return_value=Response(200, json=[])
    )

    report = await sync_repo(gh, store, repo, full=True)
    assert report.config.escalate.labels == ["needs-discussion"]  # Default
    assert len(report.errors) == 1
    assert "Failed to parse config" in report.errors[0]


@pytest.mark.asyncio
async def test_sync_incremental(gh, store, respx_mock):
    repo = "owner/repo"

    # Seed db
    from harness.store.repo import upsert_repo

    upsert_repo(store, repo, "main", None, synced_at="2022-10-10T00:00:00Z")

    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(404)
    )

    # Assert since param
    issues_resp = [
        {
            "number": 2,
            "id": 102,
            "title": "Sub 1",
            "state": "open",
            "labels": [],
            "updated_at": "2022-10-10T01:00:00Z",
        },
    ]
    respx_mock.get(
        f"https://api.github.com/repos/{repo}/issues?state=all&since=2022-10-10T00:00:00Z&per_page=100"
    ).mock(return_value=Response(200, json=issues_resp))

    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/2/parent").mock(return_value=Response(404))
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/2/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )

    report = await sync_repo(gh, store, repo, full=False)
    assert report.issues_synced == 1

    repo_row = get_repo(store, repo)
    assert repo_row["synced_at"] > "2022-10-10T00:00:00Z"


@pytest.mark.asyncio
async def test_sync_corrupt_ledger(gh, store, respx_mock):
    repo = "owner/repo"
    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(404)
    )

    issues_resp = [
        {
            "number": 7,
            "id": 107,
            "title": "Sub 5",
            "state": "open",
            "labels": [{"name": "harness:ready"}],
            "updated_at": "2023-01-01T00:00:00Z",
        }
    ]
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues?state=all&per_page=100").mock(
        return_value=Response(200, json=issues_resp)
    )

    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/7/parent").mock(return_value=Response(404))
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/7/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )

    corrupt_text = '<!-- harness:ledger -->\nSummary\n\n<!-- harness:state v1 {"test": 123 -->'
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/7/comments?per_page=100").mock(
        return_value=Response(
            200, json=[{"id": 2, "body": corrupt_text, "created_at": "2023-01-01T00:00:00Z"}]
        )
    )

    report = await sync_repo(gh, store, repo, full=True)
    assert report.issues_synced == 1
    assert len(report.errors) == 1
    assert "ledger corrupt" in report.errors[0]
