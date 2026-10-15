#!/bin/sh
# tick.sh <issue>: tick all boxes via REST, then close if still open
R=gargi-indic/gargi-meta-harness
gh api repos/$R/issues/$1 --jq .body | sed 's/- \[ \]/- [x]/g' | python3 -c "import json,sys;print(json.dumps({'body':sys.stdin.read(),'state':'closed'}))" | gh api repos/$R/issues/$1 --method PATCH --input - --jq '"#\(.number) \(.state) unchecked=\(.body | [scan("- \\[ \\]")] | length)"'
