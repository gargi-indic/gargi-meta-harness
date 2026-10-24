import { Handle, Position } from '@xyflow/react';
import { StatusTag } from '../../ui/StatusTag';
import type { CanvasNode } from '../../canvas/types';

export interface PlannerNodeData {
  agent: string;
  invalid: boolean;
  selected: boolean;
}

const TITLE_CHARS = 36;

export function truncate(text: string, max = TITLE_CHARS): string {
  return text.length > max ? `${text.slice(0, max - 1)}...` : text;
}

/** One proposed issue on the canvas: key, truncated title, agent tag; dashed accent border when it has errors. */
export function PlannerNode({ node }: { node: CanvasNode }) {
  const data = node.data as PlannerNodeData;
  const classes = ['node-card', 'planner-node'];
  if (data.invalid) classes.push('node-card--waiting');
  if (data.selected) classes.push('node-card--selected');
  return (
    <div className={classes.join(' ')} data-invalid={data.invalid ? 'true' : undefined}>
      <Handle type="target" position={Position.Top} className="node-handle target-handle" />
      <div className="node-card-header">
        <span className="node-card-id">{node.id}</span>
        <span className="node-card-badge">{data.agent || 'no agent'}</span>
      </div>
      <div className="node-card-content">
        <span className="node-card-label">{node.label}</span>
      </div>
      {data.invalid && (
        <div className="node-card-status">
          <StatusTag tone="fail">ERRORS</StatusTag>
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="node-handle source-handle" />
    </div>
  );
}
