"""build_app wiring and the default executor registry (M1.11). No network: GitHub is never called."""

from pathlib import Path

import pytest

from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.app import build_app
from harness.config import GlobalConfig, RepoConfig, RepoEntry
from harness.engine.executors import default_registry
from harness.github.client import GitHub
from harness.llm.backend import ClaudeSdkBackend, FakeBackend
from harness.playbook.load import builtin_playbooks
from harness.store.etag_cache import SqliteEtagCache
from harness.store.repo import upsert_repo

SECRET = "jules-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("JULES_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def make_cfg(tmp_path: Path, repos: tuple[str, ...] = ()) -> GlobalConfig:
    return GlobalConfig(
        data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name=name) for name in repos]
    )


async def test_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "gh-token-0123456789")
    cfg = make_cfg(tmp_path)
    app = build_app(cfg)
    try:
        assert (tmp_path / "data" / "harness.db").exists()
        assert app.store.query("SELECT value FROM meta WHERE key = 'schema_version'")  # migrated
        assert isinstance(app.gh, GitHub) and isinstance(app.gh._cache, SqliteEtagCache)
        assert app.outbox.store is app.store and app.outbox.gh is app.gh
        assert app.adapters.names() == ["fake"]  # no JULES_API_KEY
        assert isinstance(app.review_backend, ClaudeSdkBackend)
        assert app.runner.store is app.store and app.runner.outbox is app.outbox
        assert app.runner.executors is app.executors and app.runner.adapters is app.adapters
        assert app.runner.gh is app.gh
        assert set(app.runner.playbooks) == set(builtin_playbooks())
        assert "join" in app.executors.kinds()  # registered by the runner
    finally:
        await app.aclose()


async def test_jules_is_registered_only_with_a_key_and_the_key_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "gh-token-0123456789")
    monkeypatch.setenv("JULES_API_KEY", SECRET)
    app = build_app(make_cfg(tmp_path))
    try:
        assert app.adapters.names() == ["fake", "jules"]
        assert SECRET not in repr(app)
        assert SECRET not in repr(app.adapters.get("jules"))
        assert "gh-token-0123456789" not in repr(app)
    finally:
        await app.aclose()


async def test_env_files_are_loaded_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(f"JULES_API_KEY={SECRET}\nGH_TOKEN=gh-token-0123456789\n")
    # load_env_files fills empty variables; registering them with monkeypatch undoes what the file sets.
    monkeypatch.setenv("JULES_API_KEY", "")
    monkeypatch.setenv("GH_TOKEN", "")
    cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[env_file], repos=[])
    app = build_app(cfg)
    try:
        assert app.adapters.names() == ["fake", "jules"]
    finally:
        await app.aclose()


async def test_injected_parts_are_used_as_given(tmp_path: Path) -> None:
    gh = GitHub("injected-token")
    adapters = AdapterRegistry()
    adapter = FakeAdapter(name="scripted")
    adapters.register(adapter)
    backend = FakeBackend([])
    app = build_app(make_cfg(tmp_path), gh=gh, adapters=adapters, review_backend=backend, clock=lambda: 5.0)
    try:
        assert app.gh is gh and app.adapters is adapters and app.review_backend is backend
        assert app.adapters.names() == ["scripted"]  # no implicit fake or jules
        assert app.runner.clock() == 5.0 and app.outbox.clock() == 5.0
    finally:
        await app.aclose()


async def test_repo_configs_come_from_the_stored_sync_config(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, ("o/synced", "o/never-synced"))
    first = build_app(cfg, gh=GitHub("t"))
    stored = RepoConfig(control_issue=9, default_agent="fake")
    upsert_repo(first.store, "o/synced", "main", stored.model_dump())
    upsert_repo(first.store, "o/broken", "main", {"control_issue": "not a number"})
    upsert_repo(first.store, "o/extra", "main", {"default_playbook": "x"})
    await first.aclose()

    app = build_app(cfg, gh=GitHub("t"))
    try:
        assert app.repo_configs["o/synced"] == stored
        assert app.repo_configs["o/never-synced"] == RepoConfig()
        assert app.repo_configs["o/broken"] == RepoConfig()  # unreadable: defaults
        assert app.repo_configs["o/extra"].default_playbook == "x"
        assert app.runner.repo_configs is app.repo_configs
        app.set_repo_config("o/synced", RepoConfig(control_issue=3))
        assert app.runner.repo_configs["o/synced"].control_issue == 3
    finally:
        await app.aclose()


async def test_aclose_closes_adapters_that_can_be_closed(tmp_path: Path) -> None:
    class Closable(FakeAdapter):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    adapters = AdapterRegistry()
    closable = Closable(name="closable")
    adapters.register(closable)
    adapters.register(FakeAdapter(name="plain"))
    app = build_app(make_cfg(tmp_path), gh=GitHub("t"), adapters=adapters)
    await app.aclose()
    assert closable.closed


# ---- default_registry -------------------------------------------------------------------------------------


def test_default_registry_kinds() -> None:
    registry = default_registry(review_backend=FakeBackend([]))
    assert registry.kinds() == [
        "agent",
        "branch",
        "check",
        "decompose",
        "escalate",
        "gate",
        "merge",
        "notify",
        "review",
    ]


def test_default_registry_gives_the_reviewer_its_backend() -> None:
    backend = FakeBackend([])
    reviewer = default_registry(review_backend=backend).get("review")
    assert reviewer.backend is backend  # type: ignore[attr-defined]


def test_every_node_kind_of_every_builtin_playbook_has_an_executor() -> None:
    from harness.engine import Runner
    from harness.store.db import Store

    store = Store(":memory:")
    store.migrate()
    registry = default_registry(review_backend=FakeBackend([]))
    Runner(store, None, registry, AdapterRegistry())  # registers join
    used = {node.kind for pb in builtin_playbooks().values() for node in pb.nodes.values()}
    assert used, "no built-in playbooks found"
    # `parallel` is handled by the runner itself.
    missing = used - {"parallel"} - set(registry.kinds())
    assert not missing, f"no executor for node kinds: {sorted(missing)}"
    store.close()
