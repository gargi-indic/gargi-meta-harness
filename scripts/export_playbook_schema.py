#!/usr/bin/env python3
import json
import sys
from pathlib import Path

# Add harness to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from harness.playbook.models import (  # noqa: E402
    AgentNode,
    BranchNode,
    CheckNode,
    DecomposeNode,
    EscalateNode,
    GateNode,
    JoinNode,
    MergeNode,
    NotifyNode,
    ParallelNode,
    ReviewNode,
)
from harness.playbook.schema import playbook_json_schema  # noqa: E402


def build_ports_cases():
    cases = []

    # helper to add a case
    def add(node_model):
        node_dict = node_model.model_dump(mode="json")
        cases.append({"node": node_dict, "ports": list(node_model.ports())})

    # agent
    add(AgentNode(session="main", action="plan", agent="x"))
    add(AgentNode(session="main", action="plan", agent="x", max_visits=3))
    add(AgentNode(session="main", action="implement"))
    add(AgentNode(session="main", action="revise", expect="plan"))
    add(AgentNode(session="main", action="revise", expect="pr", max_visits=2))

    # review
    add(ReviewNode(target="plan", checklist=[]))
    add(ReviewNode(target="pr", checklist=["a"], max_visits=1))

    # check
    add(CheckNode(check="ci"))

    # gate
    add(GateNode(gate="test"))

    # branch
    add(BranchNode(cases=[{"when": "a", "port": "a"}, {"when": "b", "port": "b"}], default="c"))

    add(BranchNode(cases=[{"when": "a", "port": "a"}]))

    # parallel
    add(ParallelNode(branches=["a"]))

    # join
    add(JoinNode(mode="all"))

    # decompose
    add(DecomposeNode())

    # merge
    add(MergeNode())

    # escalate
    add(EscalateNode())

    # notify
    add(NotifyNode(message="test"))

    return cases


def main() -> None:
    schema = playbook_json_schema()
    schema_json = json.dumps(schema, indent=2) + "\n"

    cases = build_ports_cases()
    cases_json = json.dumps(cases, indent=2) + "\n"

    out_file = repo_root / "web" / "src" / "generated" / "playbook.schema.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    cases_file = repo_root / "web" / "src" / "generated" / "ports.cases.json"

    if "--check" in sys.argv:
        if not out_file.exists() or not cases_file.exists():
            print("Schema or cases file missing")
            sys.exit(1)
        current = out_file.read_text()
        current_cases = cases_file.read_text()

        if current != schema_json or current_cases != cases_json:
            print("Schema files are out of date. Run 'uv run python scripts/export_playbook_schema.py'")
            sys.exit(1)
        print("Schema is up to date.")
    else:
        out_file.write_text(schema_json)
        cases_file.write_text(cases_json)
        print(f"Wrote {out_file} and {cases_file}")


if __name__ == "__main__":
    main()
