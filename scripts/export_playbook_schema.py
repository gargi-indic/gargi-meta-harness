#!/usr/bin/env python3
import json
import sys
from pathlib import Path

# Add harness to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from harness.playbook.schema import playbook_json_schema  # noqa: E402


def main() -> None:
    schema = playbook_json_schema()
    schema_json = json.dumps(schema, indent=2) + "\n"

    out_file = repo_root / "web" / "src" / "generated" / "playbook.schema.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if "--check" in sys.argv:
        if not out_file.exists():
            print("Schema file missing")
            sys.exit(1)
        current = out_file.read_text()
        if current != schema_json:
            print("Schema file is out of date. Run 'uv run python scripts/export_playbook_schema.py'")
            sys.exit(1)
        print("Schema is up to date.")
    else:
        out_file.write_text(schema_json)
        print(f"Wrote {out_file}")


if __name__ == "__main__":
    main()
