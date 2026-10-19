export type NodeStatus = "pending" | "running" | "waiting" | "needs-you" | "done" | "failed" | "skipped";

export interface CanvasNode {
  id: string;
  kind: string;
  label: string;
  status?: NodeStatus;
  badge?: string;
  ports?: string[];
  data?: unknown;
}

export interface CanvasEdge {
  id: string;
  source: string;
  target: string;
  sourcePort?: string;
  label?: string;
  dashed?: boolean;
}
