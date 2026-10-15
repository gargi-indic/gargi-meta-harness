#!/usr/bin/env python3
"""Edit #1's tables. log.py row <issue> <status-emoji> [session] [pr-commit] | log.py current <active> <session> <pr> <next>"""
import re, subprocess, sys
R = "gargi-indic/gargi-meta-harness"
body = subprocess.check_output(["gh", "issue", "view", "1", "-R", R, "--json", "body", "--jq", ".body"], text=True)
cmd = sys.argv[1]
if cmd == "row":
    issue, st = sys.argv[2], sys.argv[3]
    lines = body.split("\n"); hit = 0
    for i, l in enumerate(lines):
        c = l.split("|")
        if len(c) == 7 and c[2].strip().startswith(f"#{issue} "):
            c[3] = f" {st} "
            if len(sys.argv) > 4 and sys.argv[4]: c[4] = f" `{sys.argv[4]}` "
            if len(sys.argv) > 5 and sys.argv[5]: c[5] = f" {sys.argv[5]} "
            lines[i] = "|".join(c); hit += 1
    assert hit == 1, f"row #{issue} matched {hit}"
    body = "\n".join(lines)
elif cmd == "current":
    for k, v in zip(["Active task", "Session", "PR", "Next"], sys.argv[2:6]):
        body, n = re.subn(rf"^\| {k} \|.*\|$", f"| {k} | {v} |", body, flags=re.M)
        assert n == 1, k
import json
subprocess.run(["gh", "api", f"repos/{R}/issues/1", "--method", "PATCH", "--input", "-"], input=json.dumps({"body": body}), text=True, check=True, capture_output=True)
print("ok")
