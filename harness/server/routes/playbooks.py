"""Playbook editor API (M2.8): list, get, schema, validate, dry run and save as a PR.

Fixed paths (``schema``, ``validate``, ``dry-run``, ``save``) are registered before ``/{playbook_id}``.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import harness.server.dry_run as dry_run_module
from harness.config import GlobalConfig
from harness.github.client import GitHub, GitHubError
from harness.playbook.schema import playbook_json_schema
from harness.server.deps import get_builtin_dir, get_clock, get_config, get_gh_token, get_github
from harness.server.dry_run import DryRunResult
from harness.server.playbook_service import (
    MAX_YAML_CHARS,
    Issue,
    PlaybookDetail,
    PlaybookSummary,
    SaveConflict,
    SaveResult,
    ValidationResult,
    get_playbook,
    github_detail,
    list_builtin,
    list_repo,
    save_playbook,
    valid_playbook_id,
    validate_yaml,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/playbooks", tags=["playbooks"])

_BRANCH_NAME = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"


class ValidateRequest(BaseModel):
    yaml: str = Field(max_length=MAX_YAML_CHARS, description="Playbook YAML text")
    agents: dict[str, list[str]] | None = Field(
        default=None, description="Agent name to capability names, enables the capability check"
    )


class ValidateResponse(BaseModel):
    ok: bool = Field(description="True when there are no error issues")
    issues: list[Issue] = Field(description="Every issue found")


class Scenario(BaseModel):
    outcomes: dict[str, list[str]] = Field(
        default_factory=dict, description="Node id to the port taken on its 1st, 2nd, ... visit"
    )
    max_steps: int = Field(default=200, ge=1, le=1000, description="Stop after this many steps")


class DryRunRequest(BaseModel):
    yaml: str = Field(max_length=MAX_YAML_CHARS, description="Playbook YAML text")
    scenario: Scenario | None = Field(
        default=None, description="Per-node outcomes; default is the primary port"
    )


class SaveRequest(BaseModel):
    repo: str = Field(description="Registered repo, owner/name")
    id: str = Field(description="Playbook id; becomes .harness/playbooks/<id>.yaml")
    yaml: str = Field(max_length=MAX_YAML_CHARS, description="Playbook YAML text")
    message: str | None = Field(default=None, max_length=500, description="Commit message")
    base: str | None = Field(
        default=None, pattern=_BRANCH_NAME, description="Base branch, default the repo's"
    )

    @field_validator("base")
    @classmethod
    def _no_dot_dot(cls, value: str | None) -> str | None:
        if value is not None and (".." in value or value.endswith("/")):
            raise ValueError("invalid branch name")
        return value


def _check_id(playbook_id: str) -> None:
    if not valid_playbook_id(playbook_id):
        raise HTTPException(status_code=422, detail="invalid playbook id")


def _check_repo(repo: str, config: GlobalConfig) -> None:
    if repo not in {entry.name for entry in config.repos}:
        raise HTTPException(status_code=404, detail="unknown repo")


def _need_github(gh: GitHub | None) -> GitHub:
    if gh is None:
        raise HTTPException(status_code=503, detail="GitHub not configured")
    return gh


def _invalid(result: ValidationResult) -> JSONResponse:
    issues = [issue.model_dump(mode="json") for issue in result.issues]
    return JSONResponse({"detail": "invalid playbook", "issues": issues}, status_code=422)


@router.get("", summary="List built-in playbooks and, with ?repo=, the repo's playbooks")
async def list_playbooks(
    config: Annotated[GlobalConfig, Depends(get_config)],
    builtin_dir: Annotated[Path, Depends(get_builtin_dir)],
    gh: Annotated[GitHub | None, Depends(get_github)],
    repo: Annotated[str | None, Query()] = None,
) -> list[PlaybookSummary]:
    summaries = await list_builtin(builtin_dir)
    if repo is not None:
        _check_repo(repo, config)
        client = _need_github(gh)
        try:
            summaries += await list_repo(client, repo)
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=github_detail(exc, with_message=False)) from exc
    return summaries


@router.get("/schema", summary="JSON Schema of a playbook")
async def playbook_schema() -> dict[str, Any]:
    return playbook_json_schema()


@router.post("/validate", summary="Validate playbook YAML; always 200 for any YAML text")
async def validate_playbook(body: ValidateRequest) -> ValidateResponse:
    caps = {name: set(values) for name, values in body.agents.items()} if body.agents is not None else None
    result = await asyncio.to_thread(validate_yaml, body.yaml, caps)
    return ValidateResponse(ok=result.ok, issues=result.issues)


@router.post(
    "/dry-run",
    summary="Run a playbook on scripted executors, without touching any store",
    response_model=DryRunResult,
)
async def dry_run_playbook(body: DryRunRequest) -> DryRunResult | JSONResponse:
    result = await asyncio.to_thread(validate_yaml, body.yaml, None)
    if not result.ok or result.playbook is None:
        return _invalid(result)
    playbook = result.playbook
    scenario = body.scenario or Scenario()
    for node_id, outcomes in scenario.outcomes.items():
        node = playbook.nodes.get(node_id)
        if node is None:
            raise HTTPException(status_code=422, detail=f"bad scenario: unknown node {node_id!r}")
        for outcome in outcomes:
            if outcome not in node.ports():
                raise HTTPException(
                    status_code=422, detail=f"bad scenario: node {node_id!r} has no port {outcome!r}"
                )
    # Read at call time so tests can shorten the timeout.
    return await dry_run_module.dry_run_with_timeout(
        playbook, outcomes=scenario.outcomes, max_steps=scenario.max_steps
    )


@router.post("/save", summary="Save a playbook to the repo by opening a PR", response_model=SaveResult)
async def save_playbook_route(
    body: SaveRequest,
    config: Annotated[GlobalConfig, Depends(get_config)],
    gh: Annotated[GitHub | None, Depends(get_github)],
    now: Annotated[Callable[[], datetime], Depends(get_clock)],
    gh_token: Annotated[str | None, Depends(get_gh_token)],
) -> SaveResult | JSONResponse:
    _check_id(body.id)
    _check_repo(body.repo, config)
    client = _need_github(gh)
    result = await asyncio.to_thread(validate_yaml, body.yaml, None)
    if not result.ok or result.playbook is None:
        return _invalid(result)
    if result.playbook.id != body.id:
        mismatch = Issue(
            code="id_mismatch",
            message=f"playbook id {result.playbook.id!r} does not match the requested id {body.id!r}",
            path="id",
        )
        return _invalid(ValidationResult(ok=False, issues=[mismatch]))
    warnings = [i for i in result.issues if i.severity == "warning"]
    try:
        return await save_playbook(
            client, body.repo, body.id, body.yaml, body.message, body.base, now, warnings
        )
    except SaveConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="a save for this playbook is already in progress; retry in a second",
        ) from exc
    except GitHubError as exc:
        secrets = [gh_token] if gh_token else None
        raise HTTPException(
            status_code=502, detail=github_detail(exc, with_message=True, secrets=secrets)
        ) from exc


@router.get("/{playbook_id}", summary="One playbook: YAML and parsed form")
async def read_playbook(
    playbook_id: str,
    config: Annotated[GlobalConfig, Depends(get_config)],
    builtin_dir: Annotated[Path, Depends(get_builtin_dir)],
    gh: Annotated[GitHub | None, Depends(get_github)],
    repo: Annotated[str | None, Query()] = None,
) -> PlaybookDetail:
    _check_id(playbook_id)
    client: GitHub | None = None
    if repo is not None:
        _check_repo(repo, config)
        client = _need_github(gh)
    try:
        detail = await get_playbook(playbook_id, builtin_dir=builtin_dir, gh=client, repo=repo)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=github_detail(exc, with_message=False)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown playbook")
    return detail
