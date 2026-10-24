"""load_plan_doc: a plan from a repo file (contents API) or from an issue."""

import base64
import hashlib

import pytest

from harness.github.client import GitHub
from harness.planner.plan_doc import MAX_PLAN_BYTES, PlanDocError, issue_plan_sha, load_plan_doc
from tests.planner.world import PLAN_PATH, PLAN_TEXT, REPO, World


async def test_file_plan_is_decoded_with_blob_sha_title_and_headings(world: World, gh: GitHub) -> None:
    world.put_file()
    plan = await load_plan_doc(gh, REPO, path=PLAN_PATH)
    assert plan.kind == "file" and plan.path == PLAN_PATH and plan.issue is None
    assert plan.text == PLAN_TEXT
    assert plan.sha == world.files[PLAN_PATH].sha
    assert plan.title == "The plan"
    assert plan.headings == ["The plan", "Goals", "Steps"]  # the "# not a heading" in a code fence is skipped
    assert plan.source == PLAN_PATH


async def test_ref_is_sent_to_the_contents_api(world: World, gh: GitHub) -> None:
    world.put_file()
    plan = await load_plan_doc(gh, REPO, path=PLAN_PATH, ref="release")
    assert plan.ref == "release"
    contents = [c for c in world.router.calls if "/contents/" in c.request.url.path]
    assert contents[0].request.url.params["ref"] == "release"


async def test_second_load_uses_the_etag_and_a_304(world: World, gh: GitHub) -> None:
    world.put_file()
    first = await load_plan_doc(gh, REPO, path=PLAN_PATH)
    second = await load_plan_doc(gh, REPO, path=PLAN_PATH)
    contents = [c for c in world.router.calls if "/contents/" in c.request.url.path]
    assert len(contents) == 2
    assert "if-none-match" not in contents[0].request.headers
    assert contents[1].request.headers["if-none-match"] == '"v1"'
    assert contents[1].response.status_code == 304
    assert second == first


async def test_a_changed_file_is_loaded_again_with_a_new_sha(world: World, gh: GitHub) -> None:
    world.put_file()
    first = await load_plan_doc(gh, REPO, path=PLAN_PATH)
    world.put_file(text=PLAN_TEXT + "\n## More\n", version=2)
    second = await load_plan_doc(gh, REPO, path=PLAN_PATH)
    assert second.sha != first.sha and second.headings[-1] == "More"


async def test_missing_file_is_a_plan_error(world: World, gh: GitHub) -> None:
    with pytest.raises(PlanDocError, match="not found"):
        await load_plan_doc(gh, REPO, path="docs/NOPE.md")


async def test_size_cap_from_the_reported_size(world: World, gh: GitHub) -> None:
    world.put_file(text="# Big\n" + "x" * MAX_PLAN_BYTES)
    with pytest.raises(PlanDocError, match="limit"):
        await load_plan_doc(gh, REPO, path=PLAN_PATH)


async def test_size_cap_from_the_decoded_content(world: World, gh: GitHub) -> None:
    # A response that understates its size is still capped after decoding.
    raw = ("# Big\n" + "x" * MAX_PLAN_BYTES).encode()
    world.raw_contents[PLAN_PATH] = {
        "type": "file",
        "sha": "a" * 40,
        "size": 10,
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
    }
    with pytest.raises(PlanDocError, match="limit"):
        await load_plan_doc(gh, REPO, path=PLAN_PATH)


async def test_a_file_at_the_cap_loads(world: World, gh: GitHub) -> None:
    text = "# Edge\n" + "x" * (MAX_PLAN_BYTES - len("# Edge\n"))
    assert len(text.encode()) == MAX_PLAN_BYTES
    world.put_file(text=text)
    assert (await load_plan_doc(gh, REPO, path=PLAN_PATH)).title == "Edge"


async def test_non_text_content_is_rejected(world: World, gh: GitHub) -> None:
    world.raw_contents[PLAN_PATH] = {
        "type": "file",
        "sha": "a" * 40,
        "size": 3,
        "encoding": "base64",
        "content": base64.b64encode(b"\xff\xfe\xfd").decode(),
    }
    with pytest.raises(PlanDocError, match="UTF-8"):
        await load_plan_doc(gh, REPO, path=PLAN_PATH)


async def test_a_directory_is_not_a_plan(world: World, gh: GitHub) -> None:
    world.raw_contents["docs"] = {"type": "dir"}
    with pytest.raises(PlanDocError, match="not a file"):
        await load_plan_doc(gh, REPO, path="docs")


async def test_issue_plan_sha_is_sha256_of_title_newline_body(world: World, gh: GitHub) -> None:
    world.put_issue(12, "Campaign plan", "# Heading\n\nBody text\n\n## Phase 1\n")
    plan = await load_plan_doc(gh, REPO, issue=12)
    assert plan.kind == "issue" and plan.issue == 12 and plan.path is None
    assert plan.sha == hashlib.sha256(b"Campaign plan\n# Heading\n\nBody text\n\n## Phase 1\n").hexdigest()
    assert plan.sha == issue_plan_sha("Campaign plan", "# Heading\n\nBody text\n\n## Phase 1\n")
    assert plan.title == "Campaign plan"
    assert plan.headings == ["Heading", "Phase 1"]
    assert plan.source == "#12"


async def test_editing_the_issue_body_or_title_changes_the_sha(world: World, gh: GitHub) -> None:
    world.put_issue(12, "Plan", "v1")
    first = await load_plan_doc(gh, REPO, issue=12)
    world.issues[12]["body"] = "v2"
    second = await load_plan_doc(gh, REPO, issue=12)
    world.issues[12]["title"] = "Plan!"
    third = await load_plan_doc(gh, REPO, issue=12)
    assert len({first.sha, second.sha, third.sha}) == 3


async def test_an_issue_with_no_body_loads(world: World, gh: GitHub) -> None:
    world.put_issue(12, "Only a title", "")
    world.issues[12]["body"] = None
    plan = await load_plan_doc(gh, REPO, issue=12)
    assert plan.text == "" and plan.headings == []


async def test_a_pull_request_is_not_a_plan_issue(world: World, gh: GitHub) -> None:
    world.put_issue(12, "A PR", "body")
    world.issues[12]["pull_request"] = {"url": "x"}
    with pytest.raises(PlanDocError, match="not an issue"):
        await load_plan_doc(gh, REPO, issue=12)


async def test_an_oversized_issue_body_is_rejected(world: World, gh: GitHub) -> None:
    world.put_issue(12, "Huge", "x" * (MAX_PLAN_BYTES + 1))
    with pytest.raises(PlanDocError, match="larger"):
        await load_plan_doc(gh, REPO, issue=12)


async def test_missing_issue_is_a_plan_error(world: World, gh: GitHub) -> None:
    with pytest.raises(PlanDocError, match="not found"):
        await load_plan_doc(gh, REPO, issue=99)


@pytest.mark.parametrize("kwargs", [{}, {"path": PLAN_PATH, "issue": 3}])
async def test_exactly_one_source_is_required(world: World, gh: GitHub, kwargs: dict) -> None:
    with pytest.raises(PlanDocError, match="exactly one"):
        await load_plan_doc(gh, REPO, **kwargs)
