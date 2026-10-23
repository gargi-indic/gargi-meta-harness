"""``build_app``: wires the store, GitHub client, outbox, adapters, executors, reviewer and runner.

Nothing here has behaviour of its own; it only connects the merged modules. Secrets are read from the
environment (after the global config's env files were loaded) and are never stored on or printed by the
:class:`App`.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from harness.adapters.base import AdapterRegistry
from harness.adapters.codex import CodexAdapter, CodexAgentConfig
from harness.adapters.fake import FakeAdapter
from harness.adapters.jules import JulesAdapter
from harness.config import AgentConfig, GlobalConfig, RepoConfig
from harness.engine.executors import default_registry
from harness.engine.executors.base import ExecutorRegistry
from harness.engine.runner import Runner
from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.llm.backend import ClaudeSdkBackend, ReviewBackend
from harness.playbook.load import builtin_playbooks
from harness.secrets import github_token, load_env_files
from harness.store.db import Store
from harness.store.etag_cache import SqliteEtagCache
from harness.store.repo import get_repo
from harness.workspaces import WorktreeManager


@dataclass
class App:
    cfg: GlobalConfig
    store: Store
    gh: GitHub
    outbox: Outbox
    adapters: AdapterRegistry
    executors: ExecutorRegistry
    review_backend: ReviewBackend
    runner: Runner
    repo_configs: dict[str, RepoConfig]
    clock: Callable[[], float] = field(default=time.time, repr=False)
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)

    def set_repo_config(self, repo: str, config: RepoConfig) -> None:
        """Use ``config`` for ``repo`` from now on (after a sync), in the app and in the runner."""
        self.repo_configs[repo] = config
        self.runner.repo_configs[repo] = config

    async def aclose(self) -> None:
        """Close the GitHub client, every adapter that can be closed, and the store."""
        try:
            await self.gh.aclose()
            for name in self.adapters.names():
                closer = getattr(self.adapters.get(name), "aclose", None)
                if closer is not None:
                    await closer()
        finally:
            self.store.close()


def _stored_repo_configs(store: Store, cfg: GlobalConfig) -> dict[str, RepoConfig]:
    """The repo configs that the last sync stored, for the registered repos and any other synced repo."""
    names = [r.name for r in cfg.repos]
    names += [row["full_name"] for row in store.query("SELECT full_name FROM repos")]
    configs: dict[str, RepoConfig] = {}
    for name in dict.fromkeys(names):
        stored = get_repo(store, name)
        try:
            configs[name] = RepoConfig.model_validate((stored or {}).get("config") or {})
        except ValueError:
            configs[name] = RepoConfig()  # an unreadable stored config falls back to the defaults
    return configs


def build_app(
    cfg: GlobalConfig,
    *,
    gh: GitHub | None = None,
    adapters: AdapterRegistry | None = None,
    review_backend: ReviewBackend | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> App:
    load_env_files(cfg.env_files)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    store.migrate()

    if gh is None:
        gh = GitHub(token=github_token(), cache=SqliteEtagCache(store), sleep=sleep)
    client = gh

    repo_configs = _stored_repo_configs(store, cfg)

    if adapters is None:
        adapters = AdapterRegistry()
        adapters.register(FakeAdapter())
        jules_key = os.environ.get("JULES_API_KEY")
        if jules_key:

            async def pr_head(repo: str, number: int) -> str:
                return str((await client.pull(repo, number))["head"]["sha"])

            # The wall clock, not the monotonic default: the adapter stores readings in the session ref,
            # which must stay meaningful after a restart.
            adapters.register(JulesAdapter(jules_key, clock=clock, pr_head=pr_head))
        if os.environ.get("CODEX_API_KEY"):
            codex_entries = [rc.agents["codex"] for rc in repo_configs.values() if "codex" in rc.agents]
            enabled = [entry for entry in codex_entries if entry.enabled]
            if enabled or not codex_entries:
                codex_cfg = CodexAgentConfig(**(enabled or [AgentConfig()])[0].model_dump())
                adapters.register(
                    CodexAdapter(workspaces=WorktreeManager(cfg.worktrees_dir), gh=client, config=codex_cfg)
                )

    backend = review_backend if review_backend is not None else ClaudeSdkBackend()
    executors = default_registry(review_backend=backend)
    outbox = Outbox(store, client, clock=clock, sleep=sleep)
    runner = Runner(
        store,
        outbox,
        executors,
        adapters,
        gh=client,
        repo_configs=repo_configs,
        clock=clock,
        playbooks=builtin_playbooks(),
    )
    # Runner keeps its own dict when handed an empty one; make both views share the same configs.
    runner.repo_configs = repo_configs
    return App(
        cfg=cfg,
        store=store,
        gh=client,
        outbox=outbox,
        adapters=adapters,
        executors=executors,
        review_backend=backend,
        runner=runner,
        repo_configs=repo_configs,
        clock=clock,
        sleep=sleep,
    )
