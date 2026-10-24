#!/usr/bin/env python3
"""Probe the Antigravity CLI (`agy`) in pipe and pty modes (M5.2 spike tooling).

WARNING: this calls the real agent and spends quota. It refuses to run without
--i-understand-this-calls-the-agent. It never prints environment variables or tokens.
Stdlib only. See docs/spikes/antigravity.md.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.adapters.agy_stream import parse_stream_text  # noqa: E402

CONFIRM_FLAG = "--i-understand-this-calls-the-agent"
SCENARIOS = ("hello", "readonly-plan", "edit-file", "denied-tool", "resume")
MODES = ("pipes", "pty", "pty-print-timeout")
PROMPTS = {
    "hello": "Say hello in one short sentence.",
    "readonly-plan": "Write a short plan to add a README section. Do not edit any files.",
    "edit-file": "Edit notes.txt in this repository: replace the word 'old' with 'new'.",
    "denied-tool": "Run the shell command `uname -a` and show me its output.",
    "resume": "Reply with the single word: again.",
}
DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "antigravity"


def build_argv(agy: str, scenario: str, mode: str, print_timeout: int, conversation: str | None) -> list[str]:
    # UNVERIFIED flags: -p, --output-format, --print-timeout, --conversation.
    argv = [agy, "-p", PROMPTS[scenario], "--output-format", "stream-json"]
    if mode == "pty-print-timeout":
        argv += ["--print-timeout", str(print_timeout)]
    if conversation:
        argv += ["--conversation", conversation]
    return argv


def _killpg(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _run_pipes(argv: list[str], cwd: Path, timeout: float) -> tuple[bytes, bytes, int | None, bool]:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    hung = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        hung = True
        _killpg(proc)
        out, err = proc.communicate()
    finally:
        _killpg(proc)  # reap any stragglers left in the group
    return out or b"", err or b"", None if hung else proc.returncode, hung


def _run_pty(argv: list[str], cwd: Path, timeout: float) -> tuple[bytes, bytes, int | None, bool]:
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=slave, stdout=slave, stderr=slave, start_new_session=True, close_fds=True
        )
    finally:
        os.close(slave)
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    hung = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                hung = proc.poll() is None
                break
            ready, _, _ = select.select([master], [], [], min(remaining, 0.2))
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                chunks.append(data)
            elif proc.poll() is not None:
                break
        if hung:
            _killpg(proc)
        else:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=max(0.5, deadline - time.monotonic()))
            if proc.poll() is None:
                hung = True
    finally:
        _killpg(proc)
        proc.wait()
        os.close(master)
    return b"".join(chunks), b"", None if hung else proc.returncode, hung


def run_once(
    agy: str,
    scenario: str,
    mode: str,
    *,
    timeout: float,
    print_timeout: int,
    cwd: Path,
    conversation: str | None = None,
) -> dict:
    argv = build_argv(agy, scenario, mode, print_timeout, conversation)
    runner = _run_pipes if mode == "pipes" else _run_pty
    start = time.monotonic()
    out, err, code, hung = runner(argv, cwd, timeout)
    wall = round(time.monotonic() - start, 3)
    events = parse_stream_text(out.decode("utf-8", errors="replace"))
    return {
        "scenario": scenario,
        "mode": mode,
        "argv": argv,
        "exit_code": code,
        "wall_time_s": wall,
        "hung": hung,
        "events": [{"type": e.type, "raw": e.raw} for e in events],
        "conversation_id": next((e.conversation_id for e in events if e.conversation_id), None),
        "_stdout": out,
        "_stderr": err,
    }


def _make_workdir(scenario: str, base: Path) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix=f"agy-probe-{scenario}-", dir=base))
    if scenario == "edit-file" and shutil.which("git"):
        (workdir / "notes.txt").write_text("old\n")
        for cmd in (
            ["init", "-q"],
            ["add", "."],
            ["-c", "user.name=probe", "-c", "user.email=probe@example.invalid", "commit", "-qm", "init"],
        ):
            subprocess.run(["git", *cmd], cwd=workdir, check=False, capture_output=True)
    return workdir


def run_probe(
    agy: str,
    out_dir: Path,
    *,
    scenarios=SCENARIOS,
    modes=MODES,
    timeout: float = 120.0,
    print_timeout: int = 300,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="agy-probe-") as tmp:
        base = Path(tmp)
        for scenario in scenarios:
            for mode in modes:
                workdir = _make_workdir(scenario, base)
                conversation = None
                if scenario == "resume":
                    first = run_once(
                        agy, "hello", mode, timeout=timeout, print_timeout=print_timeout, cwd=workdir
                    )
                    conversation = first["conversation_id"]
                    if not conversation:
                        results.append(
                            {
                                "scenario": scenario,
                                "mode": mode,
                                "argv": [],
                                "exit_code": None,
                                "wall_time_s": 0.0,
                                "hung": first["hung"],
                                "events": [],
                                "conversation_id": None,
                                "note": "skipped: first turn produced no conversation id",
                                "_stdout": b"",
                                "_stderr": b"",
                            }
                        )
                        continue
                res = run_once(
                    agy,
                    scenario,
                    mode,
                    timeout=timeout,
                    print_timeout=print_timeout,
                    cwd=workdir,
                    conversation=conversation,
                )
                (out_dir / f"{scenario}.{mode}.raw").write_bytes(res["_stdout"])
                if res["_stderr"]:
                    (out_dir / f"{scenario}.{mode}.stderr.raw").write_bytes(res["_stderr"])
                results.append(res)
    return results


def render(results: list[dict]) -> tuple[dict, str]:
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    lines = ["| scenario | mode | hung | exit | wall (s) | events |", "|---|---|---|---|---|---|"]
    for r in clean:
        lines.append(
            f"| {r['scenario']} | {r['mode']} | {str(r['hung']).lower()} | {r['exit_code']} "
            f"| {r['wall_time_s']} | {len(r['events'])} |"
        )
    return {"runs": clean}, "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Probe `agy` in pipe/pty modes. Spends real quota.")
    p.add_argument("--agy", default="agy", help="path to the agy binary (default: agy)")
    p.add_argument("--timeout", type=float, default=120.0, help="kill a run after N seconds (default 120)")
    p.add_argument(
        "--print-timeout", type=int, default=300, help="value for --print-timeout in the third mode"
    )
    p.add_argument("--scenario", action="append", choices=SCENARIOS, help="limit to scenario(s)")
    p.add_argument("--mode", action="append", choices=MODES, help="limit to mode(s)")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_FIXTURES, help="where .raw captures go")
    p.add_argument(
        "--summary", type=Path, default=None, help="summary.json path (default: <out-dir>/summary.json)"
    )
    p.add_argument(
        CONFIRM_FLAG, dest="confirmed", action="store_true", help="required: acknowledge quota use"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirmed:
        print(
            f"refusing to run: this calls the real agent and spends quota. Pass {CONFIRM_FLAG} to proceed.",
            file=sys.stderr,
        )
        return 2
    results = run_probe(
        args.agy,
        args.out_dir,
        scenarios=tuple(args.scenario or SCENARIOS),
        modes=tuple(args.mode or MODES),
        timeout=args.timeout,
        print_timeout=args.print_timeout,
    )
    summary, table = render(results)
    summary_path = args.summary or args.out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(table)
    print(f"summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
