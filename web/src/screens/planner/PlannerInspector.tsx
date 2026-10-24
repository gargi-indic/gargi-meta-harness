import { useState } from 'react';
import { Button } from '../../ui/Button';
import { Field } from '../../ui/Field';
import type { OptionsOut, ProposedIssue } from '../../api/planner';
import { AcceptanceList } from './AcceptanceList';

export interface PlannerInspectorProps {
  issue: ProposedIssue;
  issues: ProposedIssue[];
  options: OptionsOut | undefined;
  onChange: (patch: Partial<ProposedIssue>) => void;
  onToggleBlocker: (blocker: string, on: boolean) => void;
  onDelete: () => void;
}

function pathsText(paths: string[] | undefined): string {
  return (paths ?? []).join(', ');
}

function parsePaths(text: string): string[] {
  return text
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean);
}

/** The editable fields of one proposed issue. */
export function PlannerInspector({
  issue,
  issues,
  options,
  onChange,
  onToggleBlocker,
  onDelete,
}: PlannerInspectorProps) {
  // The paths field keeps what is typed (a trailing comma must survive); it resets when another issue is selected.
  const [pathsDraft, setPathsDraft] = useState<{ key: string; text: string }>({
    key: issue.key,
    text: pathsText(issue.paths),
  });
  const pathsValue = pathsDraft.key === issue.key ? pathsDraft.text : pathsText(issue.paths);

  const agents = options?.agents ?? [];
  const playbooks = options?.playbooks ?? [];
  const agentKnown = agents.some((a) => a.name === issue.agent);
  const playbookKnown = playbooks.includes(issue.playbook);
  const others = issues.filter((i) => i.key !== issue.key);

  return (
    <div className="planner-inspector-form">
      <p className="mono planner-key">{issue.key}</p>
      <Field label="Title" value={issue.title} onChange={(title) => onChange({ title })} />

      <div className="field">
        <label htmlFor="planner-agent" className="field-label">
          Agent
        </label>
        <select
          id="planner-agent"
          className="field-input"
          value={issue.agent}
          onChange={(e) => onChange({ agent: e.target.value })}
        >
          {!agentKnown && <option value={issue.agent}>{issue.agent || '(none)'}</option>}
          {agents.map((a) => (
            <option key={a.name} value={a.name} disabled={!a.enabled}>
              {a.enabled ? a.name : `${a.name} (disabled)`}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="planner-playbook" className="field-label">
          Playbook
        </label>
        <select
          id="planner-playbook"
          className="field-input"
          value={issue.playbook}
          onChange={(e) => onChange({ playbook: e.target.value })}
        >
          {!playbookKnown && <option value={issue.playbook}>{issue.playbook || '(none)'}</option>}
          {playbooks.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      <Field
        label="Paths (comma separated)"
        value={pathsValue}
        style={{ fontFamily: 'var(--font-mono)' }}
        onChange={(text) => {
          setPathsDraft({ key: issue.key, text });
          onChange({ paths: parsePaths(text) });
        }}
      />

      <Field
        label="Body"
        multiline
        rows={8}
        value={issue.body}
        style={{ fontFamily: 'var(--font-mono)', fontSize: 13 }}
        onChange={(body) => onChange({ body })}
      />

      <AcceptanceList items={issue.acceptance} onChange={(acceptance) => onChange({ acceptance })} />

      <fieldset className="planner-fieldset">
        <legend className="field-label">Blocked by</legend>
        {others.length === 0 && <p className="empty mono">No other issues.</p>}
        {others.map((o) => (
          <label key={o.key} className="planner-check mono">
            <input
              type="checkbox"
              checked={issue.blocked_by.includes(o.key)}
              onChange={(e) => onToggleBlocker(o.key, e.target.checked)}
            />
            {o.key}
          </label>
        ))}
      </fieldset>

      <Button onClick={onDelete} disabled={issues.length <= 1}>
        Delete issue
      </Button>
    </div>
  );
}
