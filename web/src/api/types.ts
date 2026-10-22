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

export interface RunSummary {
  id: string;
  repo: string;
  issue: number;
  title: string;
  playbook_id: string;
  status: string;
  agent: string | null;
  pr: number | null;
  current_node: string | null;
  created_at: string;
  updated_at: string;
}

export interface StepOut {
  id: string;
  node: string;
  visit: number;
  status: string;
  outcome: string | null;
  ref: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunDetail {
  run: RunSummary;
  playbook: Record<string, unknown> | null;
  steps: StepOut[];
  visit_counts: Record<string, number>;
}

export interface ActivityItem {
  seq: number;
  ts: string | null;
  step_id: string | null;
  kind: string;
  text: string;
}

export interface ActivityPage {
  items: ActivityItem[];
  next: number;
}

export interface ControlRequestOut {
  id: number;
  run_id: string;
  step_id: string | null;
  kind: string;
  status: string;
  result: Record<string, unknown> | null;
  created_at: string;
  handled_at: string | null;
}

export interface ValidateIssue {
  severity: 'error' | 'warn';
  code: string;
  message: string;
  node?: string;
  port?: string;
  path?: string;
}

export interface ValidateResponse {
  ok: boolean;
  issues: ValidateIssue[];
}

export interface TraceStep {
  node: string;
  visit: number;
  outcome?: string;
}

export interface DryRunResponse {
  terminated: string;
  trace: TraceStep[];
  gates_auto_resolved?: string[];
  error?: string | null;
}

export interface SaveResponse {
  pr?: {
    number: number;
    url: string;
  };
  branch: string;
  existing: boolean;
}

export interface PlaybookSummary {
  id: string;
  source: 'builtin' | 'repo';
  path: string;
  sha: string | null;
  valid: boolean;
}

export interface PlaybookDetail extends PlaybookSummary {
  yaml: string;
  parsed: Record<string, unknown> | null;
}
