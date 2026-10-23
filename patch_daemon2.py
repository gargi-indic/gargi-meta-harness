content = open("harness/daemon.py").read()

import_block = "from harness.store.repo import append_event, get_repo\nfrom harness.sync import sync_repo"
if "from harness.dag import ready_set" not in content:
    content = content.replace(
        import_block,
        import_block
        + "\nfrom harness.dag import ready_set\nfrom harness.store.repo import list_issues, list_runs\nfrom harness.engine.dispatcher import Dispatcher",
    )

if "_dispatch_job" not in content:
    dispatch_job = """
def _dispatch_job(app: App, repo: str) -> Callable[[], Awaitable[object]]:
    async def job() -> object:
        # Load dispatcher lazily to use latest config
        caps = {name: cfg.max_concurrency for name, cfg in app.cfg.agents.items() if cfg.enabled}
        global_cap = app.cfg.global_cap if hasattr(app.cfg, "global_cap") else None # Check if global_cap exists
        dispatcher = Dispatcher(store=app.store, caps=caps, global_cap=global_cap, clock=app.clock)

        async def ready_provider():
            issues = list_issues(app.store, repo)
            runs = list_runs(app.store)
            running_issues = {r.issue for r in runs if r.status == "running" and r.repo == repo}
            config = app.repo_configs.get(repo) or RepoConfig()
            escalate_labels = config.escalate.labels if config.escalate else ["needs-discussion"]

            ready_nums = ready_set(issues, running_issues, escalate_labels)
            return [i for i in issues if i.number in ready_nums]

        async def resolve_playbook(r: str, p_id: str):
            from harness.cli import _resolve_playbook
            # wait cli is mostly synchronous but `_resolve_playbook` is async.
            return await _resolve_playbook(app, r, p_id, None)

        return await dispatcher.tick(app.runner, ready_provider, resolve_playbook)

    return job
"""
    content = content + dispatch_job

if "dispatch_every is not None:" not in content:
    # insert dispatch task
    find_str = "for repo, lock in locks.items():"
    insert_str = """
        if dispatch_every is not None:
            for entry in app.cfg.repos:
                repo = entry.name
                tasks.append(
                    asyncio.create_task(
                        _every(
                            app, stop, "dispatch", dispatch_every, _dispatch_job(app, repo), first=True, repo=repo
                        )
                    )
                )
        for repo, lock in locks.items():"""
    content = content.replace(find_str, insert_str)

with open("harness/daemon.py", "w") as f:
    f.write(content)
