"""Planner API (M4.5): preview the plan, decompose it, edit the proposed issue DAG, approve to create it.

Behaviour and state handling are described in ``harness.server.planner_service``. The plan itself is approved
elsewhere (label, PR merge, ``harness plan approve``); these routes only read that approval.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from pydantic import Field

from harness.github.client import GitHubError
from harness.server.deps import get_planner
from harness.server.planner_service import (
    MAX_GUIDANCE_CHARS,
    DecomposeAccepted,
    JobOut,
    OptionsOut,
    PlannerError,
    PlannerService,
    PlanOut,
    ProposalOut,
    ValidationOut,
    parse_if_match,
)
from harness.server.schemas import Schema

router = APIRouter(prefix="/api/planner", tags=["planner"])

Service = Annotated[PlannerService, Depends(get_planner)]


class DecomposeRequest(Schema):
    repo: str = Field(description="Registered repo, owner/name.")
    path: str | None = Field(
        default=None, max_length=500, description="Plan file in the repo (or give issue)."
    )
    issue: int | None = Field(default=None, ge=1, description="Plan issue number (or give path).")
    ref: str | None = Field(default=None, max_length=200, description="Branch or commit of the plan file.")
    guidance: str | None = Field(
        default=None, max_length=MAX_GUIDANCE_CHARS, description="Extra instructions for the decomposer."
    )
    control_issue: int | None = Field(
        default=None, ge=1, description="Control issue that holds the draft; default: the repo config's."
    )


class ApproveRequest(Schema):
    control_issue: int | None = Field(
        default=None,
        ge=1,
        description="Control issue to record the created issues in; default: the repo config's.",
    )
    rev: int | None = Field(
        default=None,
        ge=1,
        description="The rev the human reviewed; the approval is refused with 409 when the draft moved on.",
    )


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except PlannerError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=f"github: {exc.status}") from exc


def _source(path: str | None, issue: int | None) -> None:
    if (path is None) == (issue is None):
        raise HTTPException(status_code=422, detail="give exactly one of path or issue as the plan source")
    if path is not None and (
        path.startswith("/") or "\\" in path or ".." in path.split("/") or not path.strip()
    ):
        raise HTTPException(status_code=422, detail="path must be a relative path inside the repo")


def _etag(response: Response, out: ProposalOut) -> None:
    response.headers["ETag"] = f'"{out.rev}"'


@router.get("/plan", summary="The plan document and whether it is approved")
async def read_plan(
    planner: Service,
    repo: str,
    path: Annotated[str | None, Query(max_length=500)] = None,
    issue: Annotated[int | None, Query(ge=1)] = None,
    ref: Annotated[str | None, Query(max_length=200)] = None,
    control_issue: Annotated[int | None, Query(ge=1)] = None,
) -> PlanOut:
    _source(path, issue)
    with _http_errors():
        return await planner.get_plan(repo, path=path, issue=issue, ref=ref, control_issue=control_issue)


@router.get("/options", summary="Agents and playbooks a proposed issue may name")
async def read_options(planner: Service, repo: str) -> OptionsOut:
    with _http_errors():
        return await planner.options(repo)


@router.post("/decompose", status_code=202, summary="Start decomposing the plan into a proposal")
async def decompose_plan(body: DecomposeRequest, planner: Service) -> DecomposeAccepted:
    _source(body.path, body.issue)
    with _http_errors():
        return await planner.start_decompose(
            body.repo,
            path=body.path,
            issue=body.issue,
            ref=body.ref,
            guidance=body.guidance,
            control_issue=body.control_issue,
        )


@router.get("/proposals/{proposal_id}", summary="One proposal with its validation issues")
async def read_proposal(proposal_id: str, planner: Service, response: Response) -> ProposalOut:
    with _http_errors():
        out = await planner.read_proposal(proposal_id)
    _etag(response, out)
    return out


@router.put("/proposals/{proposal_id}", summary="Replace the draft; invalid drafts are stored too")
async def edit_proposal(
    proposal_id: str,
    planner: Service,
    response: Response,
    body: Annotated[
        dict[str, Any],
        Body(
            description="A ProposedPlan: epic_title, epic_body, issues[], notes. Unknown fields are refused."
        ),
    ],
    if_match: Annotated[
        str | None, Header(description="The rev the edit is based on; 412 when the draft is at another rev.")
    ] = None,
) -> ProposalOut:
    with _http_errors():
        out = await planner.edit(proposal_id, body, parse_if_match(if_match))
    _etag(response, out)
    return out


@router.post("/proposals/{proposal_id}/validate", summary="Validation issues of the draft; writes nothing")
async def validate_proposal_route(proposal_id: str, planner: Service) -> ValidationOut:
    with _http_errors():
        return await planner.validate(proposal_id)


@router.post(
    "/proposals/{proposal_id}/approve", status_code=202, summary="Create the epic and issues from the draft"
)
async def approve_proposal(proposal_id: str, planner: Service, body: ApproveRequest | None = None) -> JobOut:
    request = body or ApproveRequest()
    with _http_errors():
        return await planner.approve(proposal_id, control_issue=request.control_issue, rev=request.rev)


@router.get("/jobs/{job_id}", summary="Status and result of a decompose or create job")
async def read_job(job_id: str, planner: Service) -> JobOut:
    with _http_errors():
        return await planner.get_job(job_id)
