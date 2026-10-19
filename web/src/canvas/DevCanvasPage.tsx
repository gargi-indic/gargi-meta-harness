import { useState, useMemo } from 'react';
import { FlowCanvas } from './FlowCanvas';
import { playbookToGraph } from './convert';
import planBuildReview from './__fixtures__/plan-build-review.json';
import { Playbook } from '../generated/playbook';

export default function DevCanvasPage() {
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [logs, setLogs] = useState<string[]>([]);

  const { nodes, edges } = useMemo(() => {
    return playbookToGraph(planBuildReview as unknown as Playbook);
  }, []);

  const addLog = (msg: string) => {
    setLogs(prev => [msg, ...prev].slice(0, 10));
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', width: '100vw' }}>
      <div style={{ padding: '8px', borderBottom: '1px solid var(--line-soft)', background: 'var(--paper)', display: 'flex', gap: '8px', alignItems: 'center' }}>
        <button onClick={() => setMode(m => m === "view" ? "edit" : "view")}>
          Toggle Mode (Current: {mode})
        </button>
      </div>

      <div style={{ flex: 1, position: 'relative' }}>
        <FlowCanvas
          nodes={nodes}
          edges={edges}
          mode={mode}
          onSelect={(id) => addLog(`Selected: ${id}`)}
          onConnect={(c) => addLog(`Connect: ${c.source}.${c.sourcePort} -> ${c.target}`)}
          onNodesDelete={(ids) => addLog(`Delete Nodes: ${ids.join(', ')}`)}
          onEdgesDelete={(ids) => addLog(`Delete Edges: ${ids.join(', ')}`)}
          onNodeMove={(id, pos) => addLog(`Move: ${id} to ${pos.x},${pos.y}`)}
        />

        <div style={{ position: 'absolute', bottom: 16, left: 16, background: 'var(--paper)', border: '1px solid var(--line-soft)', padding: 8, zIndex: 10, maxWidth: 400, fontSize: 12, fontFamily: 'var(--font-mono)', pointerEvents: 'none' }}>
          <strong>Logs:</strong>
          {logs.map((log, i) => <div key={i}>{log}</div>)}
        </div>
      </div>
    </div>
  );
}
