"""Live smoke test for the maintainer. Skipped unless AGY_LIVE=1 (spends real quota).

Run: AGY_LIVE=1 uv run pytest -m agy_live tests/adapters/test_agy_live.py
Optionally set AGY_BIN to the binary path (default: agy).
"""

import os
import pty
import select
import subprocess
import time

import pytest

from harness.adapters.agy_stream import LineAssembler

pytestmark = [
    pytest.mark.agy_live,
    pytest.mark.skipif(os.environ.get("AGY_LIVE") != "1", reason="set AGY_LIVE=1 to call the real agy"),
]


def test_hello_under_pty_yields_parseable_event():
    master, slave = pty.openpty()
    argv = [os.environ.get("AGY_BIN", "agy"), "-p", "Say hello.", "--output-format", "stream-json"]
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    asm = LineAssembler()
    events = []
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline and not events:
            if select.select([master], [], [], 0.5)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                events += asm.feed_events(data.decode("utf-8", errors="replace"))
    finally:
        proc.kill()
        proc.wait()
        os.close(master)
    assert events
