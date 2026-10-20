export interface CampaignCounts {
  ready: number;
  running: number;
  needs_you: number;
  review: number;
  blocked: number;
  done: number;
  failed: number;
}

export interface CampaignSummary {
  id: string;
  repo: string;
  control_issue: number | null;
  title: string;
  counts: CampaignCounts;
}

export interface GraphNode {
  id: number;
  title: string;
  status: string;
  agent: string;
  playbook: string;
  run_id: string | null;
  url: string;
}

export interface GraphEdge {
  source: number;
  target: number;
}

export interface CampaignGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Card {
  repo: string;
  issue: number;
  title: string;
  agent: string;
  playbook: string;
  run_id: string | null;
  pr: number | null;
  blocked_by: number[];
  updated_at: string;
}

export interface BoardColumns {
  ready: Card[];
  running: Card[];
  needs_you: Card[];
  review: Card[];
  blocked: Card[];
  done: Card[];
  failed: Card[];
}

export interface Board {
  columns: BoardColumns;
}

export interface InboxCheck {
  name: string;
  state: string;
}

export interface InboxPr {
  number: number;
  url: string;
  checks: InboxCheck[];
}

export interface InboxFinding {
  severity: string;
  text: string;
}

export interface InboxItem {
  id: string;
  kind: string;
  run_id: string;
  step_id: string;
  repo: string;
  issue: number;
  title: string;
  summary: string;
  pr: InboxPr | null;
  findings: InboxFinding[];
  agent: string | null;
  created_at: string;
  gate_id: string | null;
}

export interface RequestAccepted {
  request_id: number;
}

export interface GateDecision {
  gate_id: string;
  status: string;
}

export interface AgentConfigOut {
  name: string;
  enabled: boolean;
}

export interface AgentsEnabled {
  agents: AgentConfigOut[];
}
