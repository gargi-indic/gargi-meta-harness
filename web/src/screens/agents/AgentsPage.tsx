import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Page } from '../../app/Page';
import { useAgents, useAgentCosts, useOpsStatus, queryKeys } from '../../api/queries';
import { setDailyBudget, errorText, usd } from '../../api/ops';
import { Button } from '../../ui/Button';
import { DataTable, Column } from '../../ui/DataTable';
import { Field } from '../../ui/Field';
import { StatusTag, Tone } from '../../ui/StatusTag';
import type { AgentCapsOut, AgentHealthStatus, AgentOut, AgentRunCostRow } from '../../api/types';
import './agents.css';

const HEALTH_TONE: Record<AgentHealthStatus, Tone> = {
  ok: 'ok',
  degraded: 'warn',
  down: 'fail',
  disabled: 'muted',
  unknown: 'muted',
};

function capFlags(caps: AgentCapsOut): string {
  const flags = [
    caps.plan_approval && 'PLAN',
    caps.messaging && 'MSG',
    caps.opens_pr && 'PR',
    caps.local_workspace && 'LOCAL',
  ].filter(Boolean);
  return flags.length ? flags.join(' ') : 'NONE';
}

export function UseBar({ inUse, max }: { inUse: number; max: number }) {
  const pct = max > 0 ? Math.min(100, (inUse / max) * 100) : 0;
  return (
    <div className="usebar" role="img" aria-label={`${inUse} of ${max} in use`}>
      <div className="usebar-fill" data-testid="usebar-fill" style={{ width: `${pct}%` }} />
    </div>
  );
}

function AgentCell({ agent }: { agent: AgentOut }) {
  return (
    <section className="agent-cell" aria-label={agent.name}>
      <div className="agent-head">
        <h2 className="mono agent-name">{agent.name}</h2>
        <StatusTag tone={HEALTH_TONE[agent.health.status]}>{agent.health.status}</StatusTag>
      </div>
      <p className="mono agent-detail">{agent.health.detail}</p>
      <p className="mono">IN USE {agent.in_use} / {agent.max_concurrency}</p>
      <UseBar inUse={agent.in_use} max={agent.max_concurrency} />
      <p className="mono">{capFlags(agent.caps)}</p>
      <p className="mono">TODAY {agent.today_cost_usd === null ? 'n/a' : usd(agent.today_cost_usd)}</p>
      {agent.active_sessions.length > 0 && (
        <ul className="agent-sessions">
          {agent.active_sessions.map((s) => (
            <li key={s.step} className="mono">
              <Link to={`/runs/${encodeURIComponent(s.run_id)}`}>#{s.issue}</Link> {s.step.split('/').slice(-2).join('/')}{' '}
              {s.state} {s.cost_usd === null ? 'n/a' : usd(s.cost_usd)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function BudgetEditor({ limit }: { limit: number | null }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: setDailyBudget,
    onSuccess: () => {
      setDraft(null);
      setError(null);
      queryClient.invalidateQueries({ queryKey: queryKeys.ops });
    },
    onError: (e) => setError(errorText(e)),
  });
  const value = draft ?? (limit === null ? '' : String(limit));

  const submit = () => {
    const text = value.trim();
    if (text === '') return save.mutate(null);
    if (!/^\d+(\.\d+)?$/.test(text)) return setError('Enter a non-negative number, or leave empty for no cap.');
    setError(null);
    save.mutate(Number(text));
  };

  return (
    <div className="budget-editor">
      <Field label="Daily cap (USD)" value={value} onChange={setDraft} error={error ?? undefined} inputMode="decimal" />
      <Button variant="primary" onClick={submit} disabled={save.isPending}>Save cap</Button>
    </div>
  );
}

export function AgentsPage() {
  const agents = useAgents();
  const costs = useAgentCosts();
  const ops = useOpsStatus();

  const columns: Column<AgentRunCostRow>[] = [
    {
      key: 'run',
      header: 'Run',
      render: (r) => <Link to={`/runs/${encodeURIComponent(r.run_id)}`} className="mono">{r.run_id}</Link>,
    },
    { key: 'issue', header: 'Issue', render: (r) => <span className="mono">{r.issue === null ? 'n/a' : `#${r.issue}`}</span> },
    { key: 'agent', header: 'Agent', render: (r) => <span className="mono">{r.agent ?? 'n/a'}</span> },
    {
      key: 'cost',
      header: 'Cost',
      numeric: true,
      render: (r) => (
        <>
          {r.usd === null ? 'n/a' : usd(r.usd)}
          {r.estimated && <> <StatusTag tone="muted">ESTIMATED</StatusTag></>}
        </>
      ),
    },
  ];

  return (
    <Page title="Agents">
      {agents.isLoading && <p className="mono">Loading...</p>}
      {agents.error && <p className="mono field-error" role="alert">Error: {(agents.error as Error).message}</p>}
      {agents.data && (
        <div className="agents-grid">
          {agents.data.map((a) => <AgentCell key={a.name} agent={a} />)}
        </div>
      )}
      {ops.data && <BudgetEditor limit={ops.data.budget.limit_usd} />}
      <h2 className="agents-subhead">Cost per run</h2>
      {costs.data && <DataTable columns={columns} rows={costs.data} rowKey={(r) => r.run_id} />}
    </Page>
  );
}
