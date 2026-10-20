import { useState } from 'react';
import { useParams, Navigate, Link } from 'react-router-dom';
import { useCampaigns, useCampaignGraph } from '../../api/queries';
import { Empty } from '../../ui/Empty';
import { Panel } from '../../ui/Panel';
import { StatusTag, Tone } from '../../ui/StatusTag';
import { FlowCanvas } from '../../canvas/FlowCanvas';
import { issueGraphToCanvas } from '../../canvas/convert';
import { CanvasNode } from '../../canvas/types';
import './campaigns.css';

export type IssueStatus = "ready" | "running" | "needs-you" | "review" | "blocked" | "done" | "failed";

export const ISSUE_TONE: Record<IssueStatus, Tone> = {
  running: "live",
  "needs-you": "live",
  failed: "fail",
  done: "ok",
  review: "muted",
  ready: "muted",
  blocked: "warn",
};

interface IssueNodeProps {
  node: CanvasNode;
  selected: boolean;
  onSelect: (id: string) => void;
  statusMap: Map<string, any>;
}

function IssueNode({ node, selected, onSelect, statusMap }: IssueNodeProps) {
  const originalNode = statusMap.get(node.id);
  const status = (originalNode?.status || "pending") as IssueStatus;
  const tone = ISSUE_TONE[status] || "muted";

  return (
    <div
      className={`issue-node issue--${status} ${selected ? 'issue-node--selected' : ''}`}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(node.id);
      }}
    >
      <div className="issue-node-header">
        <span className="mono issue-node-id">{node.id}</span>
      </div>
      <div className="issue-node-title">{node.label.replace(/^#\d+\s/, '')}</div>
      <div className="issue-node-footer">
        <StatusTag tone={tone}>{status.toUpperCase()}</StatusTag>
      </div>
    </div>
  );
}

export function CampaignsPage() {
  const { owner, repo } = useParams();
  const campaignsQuery = useCampaigns();

  const currentId = owner && repo ? `${owner}/${repo}` : undefined;

  const [selectedId, setSelectedId] = useState<string | null>(null);

  const graphQuery = useCampaignGraph(currentId || "");

  if (campaignsQuery.isLoading) return null;
  if (!campaignsQuery.data || campaignsQuery.data.length === 0) {
    return <Empty>No campaigns registered. Run harness init.</Empty>;
  }

  if (!currentId && campaignsQuery.data.length > 0) {
    const first = campaignsQuery.data[0];
    const [o, r] = first.repo.split('/');
    return <Navigate to={`/campaigns/${encodeURIComponent(o)}/${encodeURIComponent(r)}`} replace />;
  }

  let nodes: CanvasNode[] = [];
  let edges: any[] = [];
  const statusMap = new Map<string, any>();

  if (graphQuery.data) {
    const canvasData = issueGraphToCanvas(graphQuery.data);
    nodes = canvasData.nodes;
    edges = canvasData.edges;

    for (const n of graphQuery.data.nodes) {
      statusMap.set(`#${n.id}`, n);
    }
  }

  const selectedNode = selectedId ? statusMap.get(selectedId) : null;

  const statuses: IssueStatus[] = ["ready", "running", "needs-you", "review", "blocked", "done", "failed"];

  return (
    <div className="campaigns-page">
      <div className="campaigns-selector">
        <ul className="campaign-list">
          {campaignsQuery.data.map(c => (
            <li key={c.id}>
              <Link
                to={`/campaigns/${encodeURIComponent(c.repo.split('/')[0])}/${encodeURIComponent(c.repo.split('/')[1])}`}
                className={c.id === currentId ? "active" : ""}
              >
                {c.repo} <span className="mono">
                  READY {c.counts.ready} · RUNNING {c.counts.running} · NEEDS YOU {c.counts.needs_you} · REVIEW {c.counts.review} · BLOCKED {c.counts.blocked} · DONE {c.counts.done} · FAILED {c.counts.failed}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </div>

      <div className="campaigns-content">
        <div className="campaigns-canvas-container" onKeyDown={e => {
          if (e.key === 'Escape') setSelectedId(null);
        }} tabIndex={0}>
          {graphQuery.data && (
            <FlowCanvas
              mode="view"
              nodes={nodes}
              edges={edges}
              renderNode={(n) => (
                <IssueNode
                  node={n}
                  selected={n.id === selectedId}
                  onSelect={setSelectedId}
                  statusMap={statusMap}
                />
              )}
            />
          )}
        </div>

        {selectedNode && (
          <aside className="campaigns-side-panel">
            <Panel title={`${selectedId} ${selectedNode.title}`}>
              <button className="btn-close" onClick={() => setSelectedId(null)} aria-label="Close">×</button>
              <dl className="mono mt-16">
                <dt>Agent</dt>
                <dd>{selectedNode.agent}</dd>
                <dt>Playbook</dt>
                <dd>{selectedNode.playbook}</dd>
              </dl>
              <div className="mt-16">
                <StatusTag tone={ISSUE_TONE[selectedNode.status as IssueStatus] || "muted"}>
                  {(selectedNode.status as string).toUpperCase()}
                </StatusTag>
              </div>
              <div className="mt-16 campaigns-links">
                {selectedNode.run_id && (
                  <Link to={`/runs/${encodeURIComponent(selectedNode.run_id)}`}>Run</Link>
                )}
                {selectedNode.run_id && " · "}
                <a href={selectedNode.url} target="_blank" rel="noreferrer">Issue on GitHub</a>
              </div>
            </Panel>
          </aside>
        )}
      </div>

      <div className="campaigns-legend">
        {statuses.map(s => (
          <StatusTag key={s} tone={ISSUE_TONE[s] || "muted"}>{s.toUpperCase()}</StatusTag>
        ))}
      </div>
    </div>
  );
}
