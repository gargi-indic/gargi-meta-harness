import { useState } from 'react';
import { Button } from '../../ui/Button';
import { DataTable } from '../../ui/DataTable';
import { TraceStep, DryRunResponse } from '../../api/types';

export interface DryRunPanelProps {
  onRun: (scenarioJson: string) => Promise<DryRunResponse | null>;
  isRunning: boolean;
}

export function DryRunPanel({ onRun, isRunning }: DryRunPanelProps) {
  const [scenarioText, setScenarioText] = useState('{\n  "outcomes": {},\n  "max_steps": 200\n}');
  const [result, setResult] = useState<DryRunResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleRun = async () => {
    setError(null);
    try {
      const res = await onRun(scenarioText);
      setResult(res);
    } catch (e: any) {
      setError(e.detail || e.message || 'Run failed');
    }
  };

  const columns: any[] = [
    {
      key: 'visit',
      header: 'Visit',
      render: (r: TraceStep) => <span style={{ fontFamily: '"JetBrains Mono", monospace' }}>{r.visit}</span>
    },
    {
      key: 'node',
      header: 'Node',
      render: (r: TraceStep) => <span style={{ fontFamily: '"JetBrains Mono", monospace' }}>{r.node}</span>
    },
    {
      key: 'outcome',
      header: 'Outcome',
      render: (r: TraceStep) => <span style={{ fontFamily: '"JetBrains Mono", monospace' }}>{r.outcome || '-'}</span>
    }
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: 16, borderBottom: '1px solid var(--line-soft)', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ fontSize: 12, fontWeight: 600 }}>Scenario (JSON)</div>
        <textarea
          value={scenarioText}
          onChange={(e) => setScenarioText(e.target.value)}
          style={{
            width: '100%',
            height: '100px',
            fontFamily: '"JetBrains Mono", monospace',
            fontSize: '12px',
            padding: '8px',
            border: '1px solid var(--ink)',
            // No border-radius rule!
            resize: 'vertical',
            background: 'var(--bg)',
            color: 'var(--ink)'
          }}
        />
        <Button onClick={handleRun} disabled={isRunning} style={{ alignSelf: 'flex-start' }}>
          {isRunning ? 'Running...' : 'Run'}
        </Button>
        {error && <div style={{ color: 'var(--fail)', fontSize: 12, marginTop: 8 }}>{error}</div>}
      </div>

      {result && (
        <div style={{ padding: 16 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, display: 'flex', justifyContent: 'space-between' }}>
            <span>Trace</span>
            <span style={{
              color: result.terminated === 'done' ? 'var(--ok)' : (result.terminated === 'error' ? 'var(--fail)' : 'var(--warn)')
            }}>
              {result.terminated.toUpperCase()}
            </span>
          </div>
          {result.error && <div style={{ color: 'var(--fail)', fontSize: 12, marginBottom: 8 }}>{result.error}</div>}
          <DataTable rows={result.trace} columns={columns} rowKey={(r) => `${r.node}-${r.visit}`} />
        </div>
      )}
    </div>
  );
}
