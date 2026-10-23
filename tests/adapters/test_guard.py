from pathlib import Path

import pytest

from harness.adapters.guard import DEFAULT_PROTECTED, GuardDecision, PathGuard


@pytest.fixture
def guard(tmp_path: Path) -> PathGuard:
    ws = tmp_path / "ws"
    (ws / ".harness").mkdir(parents=True)
    (ws / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "link_out").symlink_to(outside)
    (ws / "link_harness").symlink_to(ws / ".harness")
    (ws / "link_ok").symlink_to(ws / "src")
    return PathGuard(ws, DEFAULT_PROTECTED)


FILE_CASES = [
    ("src/app.py", True),
    ("README.md", True),
    ("my.harness.txt", True),
    ("docs/.harnessy/x", True),
    ("link_ok/x.py", True),
    (".github/workflows-notes.md", True),
    (".harness/config.yaml", False),
    (".harness", False),
    ("./.harness/a/b", False),
    ("src/../.harness/x", False),
    (".claude/settings.json", False),
    (".orchestrator/specs/x.txt", False),
    (".github/workflows/ci.yml", False),
    ("../escape.txt", False),
    ("/etc/passwd", False),
    ("link_out/x", False),
    ("link_harness/x", False),
    (".Harness/x", True),  # case-sensitive
]


@pytest.mark.parametrize(("path", "allow"), FILE_CASES)
def test_check_file_write(guard: PathGuard, path: str, allow: bool) -> None:
    assert guard.check_file_write(path).allow is allow


def test_file_write_absolute_inside_and_path_object(guard: PathGuard) -> None:
    assert guard.check_file_write(guard.workspace / "src" / "a.py").allow
    assert not guard.check_file_write(guard.workspace / ".harness" / "a").allow


def test_deny_reason_is_actionable(guard: PathGuard) -> None:
    d = guard.check_file_write(".harness/config.yaml")
    assert d == GuardDecision(
        False, "writes to protected path .harness/config.yaml are not allowed; ask in the PR instead"
    )
    assert "outside the workspace" in (guard.check_file_write("../x").reason or "")


def test_custom_protected_glob(tmp_path: Path) -> None:
    g = PathGuard(tmp_path, ["secrets/*.env"])
    assert not g.check_file_write("secrets/a.env").allow
    assert g.check_file_write("secrets/a.txt").allow
    assert g.check_file_write(".harness/x").allow


BASH_ALLOW = [
    "ls -la",
    "cat .harness/config.yaml",
    "ls .claude/ .orchestrator/",
    "git diff .harness/config.yaml",
    "git status && git log --oneline",
    "grep -rn foo .harness/ | head",
    'git commit -m "update .harness handling"',
    "git commit -m 'docs: mention .claude/ and .orchestrator/'",
    "git commit -m \"$(cat <<'EOF'\nfix: stop writing .harness/x\n\nBody > .harness/y\nEOF\n)\"",
    "echo hi > my.harness.txt",
    "echo hi > src/out.txt",
    "echo hi >> src/out.txt 2>&1",
    "make test 2>&1 | tee src/build.log",
    "echo hi > /dev/null",
    "ls nope 2>/dev/null",
    "touch src/a.py && mkdir -p src/pkg",
    "rm -rf src/build",
    "mv src/a.py src/b.py",
    "cp .harness/config.yaml src/config.copy",
    "ln -s a.py src/b.py",
    "chmod +x src/run.sh",
    "sed -i 's/a/b/' src/a.py",
    "sed -i '/etc/d' src/a.py",
    "sed -n 's/a/b/p' .harness/config.yaml",
    "git checkout -b feature/x",
    "git checkout main -- src/a.py",
    "git restore src/a.py",
    "python -c \"print(open('.harness/x').read())\"",
    "python3 -c \"open('src/x.txt','w').write('hi')\"",
    "node -e \"console.log(require('fs').readFileSync('.claude/x'))\"",
    "bash -c 'echo hi > src/x'",
    "eval 'ls .harness'",
    "echo $(cat .harness/x)",
    "cat <<EOF > src/note.txt\nmentions .harness/x\nEOF",
    "cat > src/note.txt <<'EOF'\n.harness/x > .claude/y\nEOF",
    "find . -name '*.py'",
    "dd if=.harness/x of=src/x",
    "FOO=1 pytest -q",
    "echo 'unterminated",
    "",
]

BASH_DENY = [
    "echo x > .harness/config.yaml",
    "echo x >> .claude/settings.json",
    "echo x>.orchestrator/x",
    "echo x 2> .harness/err",
    "echo x &> .harness/err",
    "echo x > ../outside.txt",
    "echo x > /etc/passwd",
    "echo x > link_out/f",
    "echo x > link_harness/f",
    "echo x > src/../.harness/f",
    "echo x > ./.harness//f",
    'echo x > ".harness/f"',
    'echo x > .har""ness/f',
    "echo x > \\.harness/f",
    "echo x | tee .harness/f",
    "echo x | tee -a .claude/f",
    "sed -i 's/a/b/' .harness/config.yaml",
    "sed -i.bak 's/a/b/' .claude/x",
    "sed -ni 's/a/b/p' .harness/x",
    "sed --in-place -e 's/a/b/' .harness/x",
    "mv src/a.py .harness/a.py",
    "mv .harness/a.py src/a.py",
    "cp src/a.py .claude/a.py",
    "cp -t .harness/ src/a.py",
    "rm -rf .harness",
    "rm .orchestrator/specs/x",
    "touch .harness/x",
    "mkdir .claude/new",
    "chmod 777 .harness/x",
    "ln -s src/a.py .harness/link",
    "ln -s .harness src/h",
    "git checkout -- .harness/config.yaml",
    "git checkout HEAD -- .claude/",
    "git restore .orchestrator/x",
    "git restore --source=HEAD~1 .harness/",
    "git -C . checkout -- .harness",
    "bash -c 'echo x > .harness/f'",
    'sh -lc "rm -rf .claude"',
    "eval 'echo x > .harness/f'",
    "eval echo x '>' .harness/f",
    "echo $(echo x > .harness/f)",
    "echo `touch .harness/f`",
    "cat <(echo hi) > .harness/f",
    "cat > .harness/f <<EOF\nhello\nEOF",
    "ls && cat > .harness/f <<'EOF'\nhello\nEOF",
    "python -c \"open('.harness/x','w').write('hi')\"",
    'python3 -c \'import pathlib; pathlib.Path(".claude/x").write_text("hi")\'',
    "node -e \"require('fs').writeFileSync('.harness/x','hi')\"",
    "python -c \"import os; os.remove('.orchestrator/x')\"",
    "python -c \"open('/etc/x','w')\"",
    "echo 'unterminated > .harness/x",
    'cat "foo .harness/x',
    "find .harness -delete",
    "find .claude -name x -exec rm {} +",
    "dd if=/dev/zero of=.harness/x",
    "cd src && echo x > ../.harness/f",
    "FOO=1 sudo rm .harness/x",
    "echo hi; rm .github/workflows/ci.yml",
    "echo hi\nrm .harness/x",
    "bash -c \"bash -c 'rm -rf .harness'\"",
    "echo $(echo $(echo $(echo $(rm .harness/x))))",
]


@pytest.mark.parametrize("command", BASH_ALLOW)
def test_bash_allow(guard: PathGuard, command: str) -> None:
    d = guard.check_bash(command)
    assert d.allow, d.reason


@pytest.mark.parametrize("command", BASH_DENY)
def test_bash_deny(guard: PathGuard, command: str) -> None:
    d = guard.check_bash(command)
    assert not d.allow
    assert d.reason


def test_unparsable_without_protected_mention_uses_fallback(guard: PathGuard) -> None:
    assert guard.check_bash("echo 'oops > src/x").allow


@pytest.mark.parametrize(
    ("tool", "tool_input", "allow"),
    [
        ("Write", {"file_path": "src/a.py"}, True),
        ("Write", {"file_path": ".harness/a"}, False),
        ("Edit", {"file_path": ".claude/a", "old_string": "a", "new_string": "b"}, False),
        ("Edit", {"file_path": "src/a.py"}, True),
        ("MultiEdit", {"file_path": "../x"}, False),
        ("MultiEdit", {"file_path": "src/x"}, True),
        ("NotebookEdit", {"notebook_path": ".orchestrator/n.ipynb"}, False),
        ("NotebookEdit", {"notebook_path": "src/n.ipynb"}, True),
        ("Write", {}, False),
        ("Bash", {"command": "echo x > .harness/f"}, False),
        ("Bash", {"command": "ls"}, True),
        ("Bash", {}, False),
        ("Read", {"file_path": ".harness/config.yaml"}, True),
        ("Grep", {"pattern": "x", "path": ".claude"}, True),
    ],
)
def test_check_tool(guard: PathGuard, tool: str, tool_input: dict, allow: bool) -> None:
    assert guard.check_tool(tool, tool_input).allow is allow


def test_module_has_no_sdk_or_network_imports() -> None:
    import harness.adapters.guard as mod

    src = Path(mod.__file__).read_text()
    for banned in ("claude_agent_sdk", "httpx", "requests", "urllib", "socket", "aiohttp"):
        assert f"import {banned}" not in src
        assert f"from {banned}" not in src
