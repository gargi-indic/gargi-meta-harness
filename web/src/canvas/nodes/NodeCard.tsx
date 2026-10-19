import { Handle, Position, NodeProps } from '@xyflow/react';
import { CanvasNode } from '../types';
import { StatusTag, Tone } from '../../ui/StatusTag';

export function NodeCard(props: NodeProps) {
  const { data: rawData, selected } = props;
  const data = rawData as unknown as CanvasNode;
  const isTerminal = data.kind === "terminal";

  let statusClass = "node--pending";
  let statusTone: Tone = "muted";

  if (data.status === "running") {
    statusClass = "node--running";
    statusTone = "live";
  } else if (data.status === "done") {
    statusClass = "node--done";
    statusTone = "ok";
  } else if (data.status === "failed") {
    statusClass = "node--failed";
    statusTone = "fail";
  } else if (data.status === "waiting" || data.status === "needs-you") {
    statusClass = "node--waiting";
    statusTone = "warn";
  } else if (data.status === "pending" || data.status === "skipped") {
    statusClass = "node--pending";
    statusTone = "muted";
  }

  return (
    <div className={`node-card ${statusClass} ${selected ? 'node-card--selected' : ''} ${isTerminal ? 'node-card--terminal' : ''}`}>
      <Handle type="target" position={Position.Left} className="node-handle target-handle" />

      <div className="node-card-header">
        <span className="node-card-id">{data.id}</span>
        {data.badge && <span className="node-card-badge">{data.badge}</span>}
      </div>

      <div className="node-card-content">
        <span className="node-card-kind">{data.kind.toUpperCase()}</span>
        <span className="node-card-label">{data.label}</span>
      </div>

      {data.status && (
        <div className="node-card-status">
          <StatusTag tone={statusTone}>{data.status.toUpperCase()}</StatusTag>
        </div>
      )}

      {data.ports && data.ports.length > 0 && (
        <div className="node-card-ports">
          {data.ports.map((port: string, idx: number) => {
            return (
              <div key={port} className="node-port">
                <span className="node-port-label">{port}</span>
                <Handle
                  type="source"
                  position={Position.Right}
                  id={port}
                  className="node-handle source-handle"
                  style={{ top: `${((idx + 1) * 100) / ((data.ports?.length || 0) + 1)}%` }}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
