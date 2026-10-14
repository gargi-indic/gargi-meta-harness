import json

import pytest
from httpx import Response

from harness.github.client import GitHub
from harness.store.db import Store
from harness.store.repo import get_issue, get_repo
from harness.sync import sync_repo

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)


@pytest.fixture
def store():
    db = Store(":memory:")
    db.migrate()
    yield db
    db.close()


@pytest.fixture
def gh():
    # Make sure GitHub uses mock client (no real network)
    # The tests patch _make_github but here we can just create one directly
    # For async sleep we can use a no-op but respx catches httpx calls.
    async def no_op_sleep(delay):
        pass

    client = GitHub("mock-token", write_spacing=0.0)
    client._sleep = no_op_sleep
    return client


@pytest.mark.asyncio
async def test_sync_full_repo(gh, store, respx_mock):
    repo = "owner/repo"

    # 1. repo info
    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )

    # 2. config
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(200, text="escalate:\n  labels: [needs-discussion]\n")
    )

    # 3. issues
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

    # 4. Parents & blockers
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

    # Sub 2 (blocked by 2)
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

    # 5. Ledger for issue 5
    ledger_state = {"test": 123}
    ledger_text = f"<!-- harness:ledger -->\nSummary\n\n<!-- harness:state v1 {json.dumps(ledger_state)} -->"
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/5/comments?per_page=100").mock(
        return_value=Response(
            200, json=[{"id": 1, "body": ledger_text, "created_at": "2023-01-01T00:00:00Z"}]
        )
    )

    report = await sync_repo(gh, store, repo, full=True)

    assert report.issues_synced == 5
    assert report.ledgers == {5: ledger_state}
    assert get_issue(store, repo, 6) is None  # PR not inserted

    repo_row = get_repo(store, repo)
    assert repo_row is not None
    assert repo_row["config"]["escalate"]["labels"] == ["needs-discussion"]

    # Incremental sync test
    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(404)
    )

    # Since should be the timestamp of previous sync
    synced_at = repo_row["synced_at"]
    respx_mock.get(
        f"https://api.github.com/repos/{repo}/issues?state=all&since={synced_at}&per_page=100"
    ).mock(return_value=Response(200, json=[]))

    report_inc = await sync_repo(gh, store, repo)
    assert report_inc.issues_synced == 0

    # Test corrupt ledger
    corrupt_text = (
        f"<!-- harness:ledger -->\nSummary\n\n<!-- harness:state v1 {json.dumps(ledger_state)[:-1]} -->"
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/contents/.harness/config.yaml?ref=main").mock(
        return_value=Response(404)
    )
    respx_mock.get(
        f"https://api.github.com/repos/{repo}/issues?state=all&since={report_inc.config.model_dump().get('synced_at', repo_row['synced_at'])}&per_page=100"
    ).mock(
        return_value=Response(
            200,
            json=[
                {
                    "number": 7,
                    "id": 107,
                    "title": "Sub 5",
                    "state": "open",
                    "labels": [{"name": "harness:ready"}],
                    "updated_at": "2023-01-01T00:00:00Z",
                }
            ],
        )
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/7/parent").mock(return_value=Response(404))
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/7/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )
    respx_mock.get(f"https://api.github.com/repos/{repo}/issues/7/comments?per_page=100").mock(
        return_value=Response(
            200, json=[{"id": 2, "body": corrupt_text, "created_at": "2023-01-01T00:00:00Z"}]
        )
    )

    report_corrupt = await sync_repo(gh, store, repo)
    assert report_corrupt.issues_synced == 1
    assert "ledger corrupt" in report_corrupt.errors[0]
