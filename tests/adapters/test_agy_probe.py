import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts" / "spikes" / "agy_probe.py"
FIXTURES = ROOT / "tests" / "fixtures" / "antigravity"

FAKE_OK = """#!/bin/sh
echo '{"type": "conversation", "conversation_id": "fake-1"}'
echo '{"type": "message", "text": "hello"}'
"""
# Hangs unless stdout is a TTY; spawns a child so group-kill is exercised.
FAKE_HANG = """#!/bin/sh
if [ -t 1 ]; then
  echo '{"type": "conversation", "conversation_id": "fake-1"}'
  echo '{"type": "message", "text": "hello"}'
  exit 0
fi
sleep 30713 &
sleep 30713
"""


def fake_agy(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "agy"
    path.write_text(body)
    path.chmod(0o755)
    return path


def run_probe(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PROBE), *args], capture_output=True, text=True, timeout=60)


def pgrep_sleep() -> bool:
    return subprocess.run(["pgrep", "-f", "sleep 30713"], capture_output=True).returncode == 0


def test_help():
    r = run_probe(["--help"])
    assert r.returncode == 0
    assert "--i-understand-this-calls-the-agent" in r.stdout


def test_refuses_without_flag(tmp_path):
    marker = tmp_path / "ran"
    agy = fake_agy(tmp_path, f"#!/bin/sh\ntouch {marker}\n")
    r = run_probe(["--agy", str(agy), "--out-dir", str(tmp_path / "out")])
    assert r.returncode == 2
    assert "refusing" in r.stderr
    assert not marker.exists()
    assert not (tmp_path / "out").exists()


def test_hang_detected_in_pipes_not_pty(tmp_path):
    agy = fake_agy(tmp_path, FAKE_HANG)
    out = tmp_path / "out"
    r = run_probe(
        [
            "--agy", str(agy), "--out-dir", str(out), "--scenario", "hello",
            "--mode", "pipes", "--mode", "pty", "--timeout", "1.5",
            "--i-understand-this-calls-the-agent",
        ]
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    runs = {x["mode"]: x for x in json.loads((out / "summary.json").read_text())["runs"]}
    assert runs["pipes"]["hung"] is True
    assert runs["pty"]["hung"] is False
    assert runs["pty"]["exit_code"] == 0
    assert [e["type"] for e in runs["pty"]["events"]] == ["conversation", "message"]
    assert runs["pty"]["conversation_id"] == "fake-1"
    assert (out / "hello.pty.raw").exists() and (out / "hello.pipes.raw").exists()
    assert "| hello | pipes | true |" in r.stdout
    assert not pgrep_sleep()


def test_resume_uses_conversation_and_never_prints_env(tmp_path):
    agy = fake_agy(tmp_path, FAKE_OK)
    out = tmp_path / "out"
    env = {**os.environ, "AGY_PROBE_FAKE_TOKEN": "supersecretvalue123"}
    r = subprocess.run(
        [
            sys.executable, str(PROBE), "--agy", str(agy), "--out-dir", str(out),
            "--scenario", "resume", "--mode", "pipes", "--timeout", "5",
            "--i-understand-this-calls-the-agent",
        ],
        capture_output=True, text=True, timeout=60, env=env,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "supersecretvalue123" not in r.stdout + r.stderr
    summary = (out / "summary.json").read_text()
    assert "supersecretvalue123" not in summary
    run = json.loads(summary)["runs"][0]
    assert run["argv"][-2:] == ["--conversation", "fake-1"]


def test_print_timeout_mode_argv(tmp_path):
    agy = fake_agy(tmp_path, FAKE_OK)
    out = tmp_path / "out"
    r = run_probe(
        [
            "--agy", str(agy), "--out-dir", str(out), "--scenario", "hello",
            "--mode", "pty-print-timeout", "--print-timeout", "77", "--timeout", "5",
            "--i-understand-this-calls-the-agent",
        ]
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    run = json.loads((out / "summary.json").read_text())["runs"][0]
    assert run["argv"][-2:] == ["--print-timeout", "77"]
    assert run["hung"] is False


def test_fixtures_have_no_secrets():
    pattern = re.compile(r"key|token|secret", re.IGNORECASE)
    files = [p for p in FIXTURES.rglob("*") if p.is_file()]
    assert files
    for p in files:
        text = p.read_bytes().decode("utf-8", errors="replace")
        assert not pattern.search(text), f"possible secret in {p}"


def test_synthetic_files_are_marked():
    for p in (FIXTURES / "synthetic").iterdir():
        assert p.is_file()
