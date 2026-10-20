import { StatusTag } from '../../ui/StatusTag';
import { ValidateIssue } from '../../api/types';

export interface ValidationPanelProps {
  issues: ValidateIssue[];
  isValidating: boolean;
  onSelectNode: (nodeId: string) => void;
}

export function ValidationPanel({ issues, isValidating, onSelectNode }: ValidationPanelProps) {
  if (isValidating) {
    return <div style={{ padding: 16, color: 'var(--muted)', fontSize: 12 }}>Validating...</div>;
  }

  if (issues.length === 0) {
    return (
      <div style={{ padding: 16, color: 'var(--ok)', fontSize: 12, fontFamily: '"JetBrains Mono", monospace' }}>
        No validation issues.
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {issues.map((issue, idx) => (
        <div
          key={idx}
          onClick={() => {
            if (issue.node) onSelectNode(issue.node);
          }}
          style={{
            padding: '12px 16px',
            borderBottom: '1px solid var(--line-soft)',
            cursor: issue.node ? 'pointer' : 'default',
            fontFamily: '"JetBrains Mono", monospace',
            fontSize: 12,
            display: 'flex',
            flexDirection: 'column',
            gap: 4
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <StatusTag tone={issue.severity === 'error' ? 'fail' : 'warn'}>
              {issue.severity.toUpperCase()}
            </StatusTag>
            <span style={{ fontWeight: 600 }}>{issue.code}</span>
          </div>
          <div style={{ color: 'var(--ink)' }}>{issue.message}</div>
          {(issue.node || issue.path) && (
            <div style={{ color: 'var(--muted)', fontSize: 11 }}>
              {issue.node && `Node: ${issue.node} `}
              {issue.path && `Path: ${issue.path}`}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
