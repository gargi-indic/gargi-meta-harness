import { Nodes } from '../generated/playbook';

export type PlaybookNode = Nodes[string];

export function portsFor(node: PlaybookNode): string[] {
  let ports: string[] = [];

  switch (node.kind) {
    case "agent":
      if (node.action === "plan") {
        ports = ["plan_ready", "pr_ready", "needs_input", "failed", "timeout"];
      } else if (node.action === "implement") {
        ports = ["pr_ready", "needs_input", "failed", "timeout"];
      } else if (node.action === "revise") {
        ports = [`${node.expect}_ready`, "needs_input", "failed", "timeout"];
      }
      break;
    case "review":
      ports = ["approve", "changes", "escalate"];
      break;
    case "check":
      ports = ["pass", "fail"];
      break;
    case "gate":
      ports = ["approved", "rejected"];
      break;
    case "branch":
      ports = [...new Set([...node.cases.map(c => c.port), node.default ?? "failed"])];
      break;
    case "parallel":
      ports = [];
      break;
    case "join":
      ports = ["done", "failed"];
      break;
    case "decompose":
      ports = node.create
        ? ["proposed", "created", "invalid", "failed"]
        : ["proposed", "invalid", "failed"];
      break;
    case "merge":
      ports = ["merged", "failed"];
      break;
    case "escalate":
      ports = ["resolved"];
      break;
    case "notify":
      ports = ["sent"];
      break;
    default:
      ports = [];
  }

  if (node.max_visits != null) {
    ports.push("exhausted");
  }

  return ports;
}
