"""Sandboxed templating and condition evaluation for playbooks.

Playbook strings (`agent`, `message`, `prompt`) and conditions (`auto_if`,
`branch.when`) share one small language: a Jinja2 sandbox with strict
undefined handling. Nothing in here performs I/O.
"""

from typing import Any

import jinja2
import jinja2.meta
import jinja2.sandbox
from pydantic import BaseModel, ConfigDict, Field

RESERVED_NAMES = ("issue", "pr", "inputs", "nodes", "prev", "run")

# Globals kept from Jinja's defaults (cycler, joiner, lipsum and namespace are removed).
# `true`, `false` and `none` are literals in Jinja already; they are also added as globals.
_KEPT_GLOBALS = ("range", "dict", "lists")


class TemplateError(Exception):
    """A template failed to compile, render or evaluate."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(path, message)
        self.path = path
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class IssueCtx(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    agent: str
    playbook: str | None = None


class PrCtx(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int
    url: str
    head_sha: str = ""
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0


class RunCtx(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    visits: dict[str, int] = Field(default_factory=dict)


class Context(BaseModel):
    """Everything a template or condition can see.

    `nodes` holds the latest output of each node by node id. `prev` is the output of the
    node that routed into the current node (revise messages use `{{ prev.correction }}`).
    """

    model_config = ConfigDict(extra="forbid")
    issue: IssueCtx
    pr: PrCtx | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    nodes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    prev: dict[str, Any] | None = None
    run: RunCtx

    def namespace(self) -> dict[str, Any]:
        """Build the template namespace.

        Node ids are reachable as top-level names, except ids that collide with a reserved
        name; those are reachable only through `nodes.<id>`.
        """
        dumped = self.model_dump()
        ns: dict[str, Any] = dict(dumped["nodes"])
        for name in RESERVED_NAMES:
            ns[name] = dumped[name]
        return ns


def _env() -> jinja2.sandbox.ImmutableSandboxedEnvironment:
    env = jinja2.sandbox.ImmutableSandboxedEnvironment(
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    kept = {name: env.globals[name] for name in _KEPT_GLOBALS if name in env.globals}
    env.globals.clear()
    env.globals.update(kept)
    env.globals.update({"true": True, "false": False, "none": None})
    return env


def _wrap(exc: Exception, path: str) -> TemplateError:
    if isinstance(exc, jinja2.TemplateSyntaxError):
        return TemplateError(path, f"syntax error: {exc.message} (line {exc.lineno})")
    if isinstance(exc, jinja2.exceptions.SecurityError):
        return TemplateError(path, f"forbidden operation: {exc}")
    if isinstance(exc, jinja2.UndefinedError):
        return TemplateError(path, f"undefined value: {exc}")
    return TemplateError(path, f"{type(exc).__name__}: {exc}")


def render(template: str, ctx: Context, *, path: str = "") -> str:
    """Render `template` against `ctx`. Raises TemplateError on any failure."""
    try:
        return _env().from_string(template).render(ctx.namespace())
    except Exception as exc:
        raise _wrap(exc, path) from exc


def evaluate(expr: str, ctx: Context, *, path: str = "") -> bool:
    """Evaluate a condition (bare or wrapped in `{{ }}`) to a strict bool."""
    text = expr.strip()
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2]
    try:
        result = _env().compile_expression(text, undefined_to_none=False)(ctx.namespace())
        if isinstance(result, jinja2.Undefined):
            str(result)  # StrictUndefined raises UndefinedError with the useful message
    except Exception as exc:
        raise _wrap(exc, path) from exc
    if not isinstance(result, bool):
        raise TemplateError(path, f"condition must evaluate to a boolean, got {type(result).__name__}")
    return result


def template_variables(template: str, *, path: str = "") -> set[str]:
    """Names a template reads from the namespace (for the editor and validator)."""
    try:
        return set(jinja2.meta.find_undeclared_variables(_env().parse(template)))
    except Exception as exc:
        raise _wrap(exc, path) from exc
