import { Button } from '../../ui/Button';
import { DataTable, Column } from '../../ui/DataTable';
import { TraceStep, DryRunResponse } from '../../api/types';

export const DEFAULT_SCENARIO = '{\n  "outcomes": {},\n  "max_steps": 200\n}';

export interface DryRunPanelProps {
  scenarioText: string;
  onScenarioChange: (text: string) => void;
  onRun: () => void;
  isRunning: boolean;
  result: DryRunResponse | null;
  error: string | null;
}

const mono = { fontFamily: '"JetBrains Mono", monospace' } as const;

export function DryRunPanel({ scenarioText, onScenarioChange, onRun, isRunning, result, error }: DryRunPanelProps) {
  const columns: Column<TraceStep>[] = [
    { key: 'node', header: 'Node', render: (r) => <span style={mono}>{r.node}</span> },
    { key: 'visit', header: 'Visit', render: (r) => <span style={mono}>{r.visit}</span> },
    { key: 'outcome', header: 'Outcome', render: (r) => <span style={mono}>{r.outcome || '-'}</span> },
  ];

  return (
    <div id="dry-run-panel" style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: 16, borderBottom: '1px solid var(--line-soft)', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <label htmlFor="dry-run-scenario" style={{ fontSize: 12, fontWeight: 600 }}>Scenario (JSON)</label>
        <textarea
          id="dry-run-scenario"
          value={scenarioText}
          onChange={(e) => onScenarioChange(e.target.value)}
          style={{
            ...mono,
            width: '100%',
            height: '100px',
            fontSize: '12px',
            padding: '8px',
            border: '1px solid var(--ink)',
            resize: 'vertical',
            background: 'var(--bg)',
            color: 'var(--ink)',
          }}
        />
        <Button onClick={onRun} disabled={isRunning} style={{ alignSelf: 'flex-start' }}>
          {isRunning ? 'Running...' : 'Run'}
        </Button>
        {error && <div role="alert" style={{ color: 'var(--accent)', fontSize: 12, marginTop: 8 }}>{error}</div>}
      </div>

      {result && (
        <div style={{ padding: 16 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, display: 'flex', justifyContent: 'space-between' }}>
            <span>Trace</span>
            <span data-testid="dry-run-status" style={mono}>{result.terminated}</span>
          </div>
          {result.error && <div style={{ color: 'var(--accent)', fontSize: 12, marginBottom: 8 }}>{result.error}</div>}
          <DataTable rows={result.trace} columns={columns} rowKey={(r) => `${r.node}-${r.visit}`} />
        </div>
      )}
    </div>
  );
}
