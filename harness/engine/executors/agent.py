"""Executor for the agent node."""

import re
from typing import Any

from harness.adapters.base import AgentSnapshot, AgentState, SessionRef, StartRequest
from harness.engine.brief import build_brief
from harness.engine.executors.base import Done, ExecContext, Executor, Waiting
from harness.github.client import GitHubError
from harness.playbook.models import AgentNode, Node
from harness.playbook.templating import render
from harness.secrets import redact
from harness.store.repo import IssueRow, append_event, get_issue, update_step

_PAST_PLANNING = (AgentState.WORKING, AgentState.PR_READY, AgentState.NEEDS_INPUT, AgentState.FAILED)


def duration_seconds(timeout: str) -> float:
    match = re.match(r"^(\d+)([smhd])$", timeout)
    if not match:
        raise ValueError(f"invalid timeout format: {timeout}")
    val = int(match.group(1))
    unit = match.group(2)
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


class AgentExecutor(Executor):
    async def advance(self, step, node: Node, ctx: ExecContext) -> Waiting | Done:
        assert isinstance(node, AgentNode)

        agent_name = render(node.agent, ctx.template, path=f"nodes.{step.node_id}.agent")
        adapter = ctx.adapters.get(agent_name)
        # ``session`` may be a template (``{{ pick.winner }}``): a node after a join continues the winner's session.
        slot = render(node.session, ctx.template, path=f"nodes.{step.node_id}.session")
        key = f"{ctx.run.id}/{slot}"

        ref = dict(step.ref) if step.ref else {}

        session: SessionRef | None = None
        if "id" in ref:
            session = SessionRef(**{k: ref[k] for k in ("agent", "id", "url", "data") if k in ref})
        else:
            session = await adapter.find(key)

        def make_ref(s: SessionRef) -> dict[str, Any]:
            dump = s.model_dump(mode="json")
            return {**dump, **{k: v for k, v in ref.items() if k not in dump}}

        if "since" not in ref:
            ref["since"] = ctx.clock()
            if step.ref != ref:
                update_step(ctx.store, step.id, ref=ref)

        if session is not None and "id" not in ref:
            ref = make_ref(session)
            update_step(ctx.store, step.id, ref=ref)

        if node.timeout and (ctx.clock() - ref["since"]) > duration_seconds(node.timeout):
            return Done(
                "timeout", {"error": f"timed out after {node.timeout}", "correction": "Agent timed out."}
            )

        async def start_session(mode: str) -> SessionRef:
            base_branch = "main"
            if ctx.repo_config.base_branch:
                base_branch = ctx.repo_config.base_branch
            elif ctx.gh:
                try:
                    repo_data = await ctx.gh.repo(ctx.run.repo)
                    base_branch = repo_data.get("default_branch", "main")
                except GitHubError:
                    pass

            issue = get_issue(ctx.store, ctx.run.repo, ctx.run.issue)
            if not issue:
                issue = IssueRow(
                    repo=ctx.run.repo,
                    number=ctx.template.issue.number,
                    id=ctx.template.issue.number,
                    title=ctx.template.issue.title,
                    body=ctx.template.issue.body,
                    state="open",
                    labels=ctx.template.issue.labels,
                    parent=None,
                    blocked_by=[],
                    updated_at="1970-01-01T00:00:00Z",
                )

            parent = get_issue(ctx.store, ctx.run.repo, issue.parent) if issue.parent else None

            context_files_text = {}
            if ctx.gh and ctx.repo_config.context_files:
                for fpath in ctx.repo_config.context_files:
                    if fpath.endswith("/"):
                        continue
                    try:
                        text = await ctx.gh.file_text(ctx.run.repo, fpath, ref=base_branch)
                        context_files_text[fpath] = text
                    except GitHubError:
                        pass

            prompt = (
                render(node.prompt, ctx.template, path=f"nodes.{step.node_id}.prompt")
                if node.prompt
                else None
            )
            brief = build_brief(issue, parent, ctx.repo_config, context_files_text, prompt)

            slug = re.sub(r"[^a-z0-9]+", "-", issue.title.lower()).strip("-")[:40]
            branch_name = f"harness/{issue.number}-{slug}"

            req = StartRequest(
                repo=ctx.run.repo,
                base_branch=base_branch,
                branch=branch_name,
                issue=issue.number,
                title=issue.title,
                brief=brief,
                mode=mode,
                idempotency_key=key,
            )
            return await adapter.start(req)

        async def poll(current: SessionRef) -> AgentSnapshot:
            snapshot = await adapter.poll(current)
            if snapshot.activity:
                with ctx.store.tx():
                    for item in snapshot.activity:
                        append_event(
                            ctx.store,
                            kind="agent_activity",
                            data={
                                "step": step.id,
                                "ts": item.ts,
                                "kind": item.kind,
                                "text": redact(item.text)[:2000],
                            },
                            repo=ctx.run.repo,
                            issue=ctx.run.issue,
                            run_id=ctx.run.id,
                        )
            return snapshot

        if node.action == "plan":
            if not session:
                mode = "plan_first" if adapter.caps.plan_approval else "direct"
                session = await start_session(mode)
                ref = make_ref(session)
                update_step(ctx.store, step.id, ref=ref)
            snap = await poll(session)

        elif node.action == "implement":
            if not session:
                session = await start_session("direct")
                ref = make_ref(session)
                ref["approved"] = True
                update_step(ctx.store, step.id, ref=ref)
                snap = await poll(session)
            elif ref.get("approved"):
                snap = await poll(session)
            else:
                snap = await poll(session)
                if snap.state == AgentState.PLAN_READY:
                    ref["approved"] = True
                    update_step(ctx.store, step.id, ref=ref)
                    await adapter.approve_plan(session, None)
                    return Waiting(ref=ref, poll_after=0)
                if snap.state in _PAST_PLANNING:
                    ref["approved"] = True
                    update_step(ctx.store, step.id, ref=ref)

        elif node.action == "revise":
            if not session:
                return Done("failed", {"error": f"no session for slot {slot}"})

            msg = render(node.message, ctx.template, path=f"nodes.{step.node_id}.message")
            if not msg.strip():
                return Done("failed", {"error": "empty revise message"})

            snap = await poll(session)
            if not ref.get("sent"):
                ref["baseline_plan"] = snap.plan
                ref["baseline_sha"] = snap.pr.head_sha if snap.pr else None
                ref["sent"] = True
                update_step(ctx.store, step.id, ref=ref)
                await adapter.send(session, msg)
                return Waiting(ref=ref, poll_after=0)

            if node.expect == "plan":
                if snap.state == AgentState.PLAN_READY and snap.plan != ref.get("baseline_plan"):
                    return Done("plan_ready", {"plan": snap.plan, "session": session.id})
            elif node.expect == "pr":
                if (
                    snap.state == AgentState.PR_READY
                    and snap.pr
                    and snap.pr.head_sha != ref.get("baseline_sha")
                ):
                    return Done("pr_ready", {"pr": snap.pr.model_dump()})

        else:
            raise ValueError(f"unknown agent action: {node.action}")

        if snap.state == AgentState.NEEDS_INPUT:
            return Done("needs_input", {"question": snap.question, "session": session.id})
        if snap.state == AgentState.FAILED:
            return Done("failed", {"error": redact(snap.error or "agent failed")})

        if node.action == "plan":
            if snap.state == AgentState.PLAN_READY:
                return Done("plan_ready", {"plan": snap.plan, "session": session.id})
            if snap.state == AgentState.PR_READY and snap.pr:
                return Done("pr_ready", {"pr": snap.pr.model_dump()})
        elif node.action == "implement":
            if snap.state == AgentState.PR_READY and snap.pr:
                return Done("pr_ready", {"pr": snap.pr.model_dump()})

        return Waiting(ref=ref, poll_after=snap.poll_after)
