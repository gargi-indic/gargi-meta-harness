import { useState, useEffect, useReducer, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import YAML from 'yaml';
import { Button } from '../../ui/Button';
import { FlowCanvas } from '../../canvas/FlowCanvas';
import { playbookToGraph } from '../../canvas/convert';
import { editorReducer } from './state';
import { Palette } from './Palette';
import { Inspector } from './Inspector';
import { ValidationPanel } from './ValidationPanel';
import { DryRunPanel } from './DryRunPanel';
import { apiPost, apiGet } from '../../api/client';
import { ValidateResponse, DryRunResponse, SaveResponse, ValidateIssue } from '../../api/types';
import './editor.css';

export function EditorPage() {
  const { id } = useParams<{ id: string }>();
  const _navigate = useNavigate();
  void _navigate; // prevent unused error

  const [state, dispatch] = useReducer(editorReducer, {
    playbook: { apiVersion: "harness/v1", nodes: {}, edges: {} },
    yaml: '',
    selectedNodeId: null,
    isDirty: false,
    history: [],
    future: []
  });

  const [yamlMode, setYamlMode] = useState(false);
  const [yamlText, setYamlText] = useState('');
  const [yamlError, setYamlError] = useState<string | null>(null);

  const [validating, setValidating] = useState(false);
  const [validationIssues, setValidationIssues] = useState<ValidateResponse['issues']>([]);

  const [dryRunRunning, setDryRunRunning] = useState(false);
  const [dryRunTrace, setDryRunTrace] = useState<any[]>([]);

  // Load initial
  useEffect(() => {
    let active = true;
    if (id === 'new') {
      dispatch({
        type: 'SET_PLAYBOOK',
        playbook: { apiVersion: "harness/v1", nodes: {}, edges: {} }
      });
      return;
    }
    apiGet(`/api/playbooks/${encodeURIComponent(id || '')}`)
      .then((data: any) => {
        if (!active) return;
        try {
          const pb = YAML.parse(data.yaml);
          dispatch({ type: 'SET_PLAYBOOK', playbook: pb });
        } catch (e) {
          console.error("Parse err", e);
        }
      })
      .catch(console.error);

    return () => { active = false; };
  }, [id]);

  // Validation debounce
  useEffect(() => {
    const timer = setTimeout(async () => {
      try {
        setValidating(true);
        const yaml = YAML.stringify(state.playbook);
        const res = await apiPost<ValidateResponse>('/api/playbooks/validate', { yaml });
        setValidationIssues(res.issues);
      } catch (e) {
        console.error(e);
      } finally {
        setValidating(false);
      }
    }, 400);

    return () => clearTimeout(timer);
  }, [state.playbook]);

  // Prevent navigation if dirty
  useEffect(() => {
    if (!state.isDirty) return;

    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [state.isDirty]);

  // Sync yaml textarea
  useEffect(() => {
    if (yamlMode) {
      setTimeout(() => {
        setYamlText(YAML.stringify(state.playbook));
        setYamlError(null);
      }, 0);
    }
  }, [yamlMode, state.playbook]);

  const handleYamlChange = (val: string) => {
    setYamlText(val);
    try {
      const pb = YAML.parse(val);
      setYamlError(null);
      dispatch({ type: 'SYNC_FROM_YAML', playbook: pb });
    } catch (e: any) {
      setYamlError(e.message);
    }
  };

  const handleSave = async () => {
    const yaml = YAML.stringify(state.playbook);
    const pbId = state.playbook.id || id || 'new-playbook';

    const repo = prompt("Enter target repo (owner/name):");
    if (!repo) return;

    try {
      const res = await apiPost<SaveResponse>('/api/playbooks/save', {
        repo,
        id: pbId,
        yaml
      });
      alert(`Saved! PR: ${res.pr?.url}`);
      dispatch({ type: 'SET_PLAYBOOK', playbook: state.playbook }); // clear dirty
    } catch (e: any) {
      alert("Failed to save: " + (e.detail || e.message));
    }
  };

  const { nodes, edges } = useMemo(() => playbookToGraph(state.playbook), [state.playbook]);

  const activeNodes = useMemo(() => {
    const errorNodes = new Set(validationIssues.filter((i: ValidateIssue) => i.severity === 'error' && i.node).map((i: ValidateIssue) => i.node));
    return nodes.map(n => ({
      ...n,
      status: (errorNodes.has(n.id) ? 'failed' :
              (dryRunTrace.find(t => t.node === n.id) ? 'running' : n.status)) as any
    }));
  }, [nodes, validationIssues, dryRunTrace]);

  const hasErrors = validationIssues.some(i => i.severity === 'error');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <div className="editor-top-bar">
        <div className="editor-title">
          {state.playbook.id || id}
          {state.isDirty && <span style={{ color: 'var(--warn)', fontSize: 11 }}>MODIFIED</span>}
        </div>
        <div className="editor-actions">
          <Button onClick={() => dispatch({ type: 'UNDO' })} disabled={state.history.length === 0}>Undo</Button>
          <Button onClick={() => dispatch({ type: 'REDO' })} disabled={state.future.length === 0}>Redo</Button>
          <Button variant="primary" disabled={!state.isDirty || hasErrors} onClick={handleSave}>Save as PR</Button>
          <Button onClick={() => setYamlMode(!yamlMode)}>{yamlMode ? 'Canvas' : 'YAML'}</Button>
        </div>
      </div>

      {yamlMode ? (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
          {yamlError && <div style={{ padding: 8, background: 'var(--fail)', color: 'white', fontSize: 12 }}>{yamlError}</div>}
          <textarea
            className="editor-yaml-view"
            value={yamlText}
            onChange={e => handleYamlChange(e.target.value)}
          />
        </div>
      ) : (
        <div className="editor-grid">
          <div className="editor-palette">
            <h3>Palette</h3>
            <Palette onAddNode={(kind) => dispatch({ type: 'ADD_NODE', kind })} />
          </div>

          <div className="editor-canvas-container">
            <FlowCanvas
              mode="edit"
              nodes={activeNodes}
              edges={edges}
              selectedId={state.selectedNodeId}
              onSelect={(nodeId) => {
                if (nodeId !== state.selectedNodeId) {
                  dispatch({ type: 'SELECT_NODE', id: nodeId });
                }
              }}
              onConnect={({ source, sourcePort, target }) => dispatch({ type: 'CONNECT', source, port: sourcePort, target })}
              onNodesDelete={(ids) => {
                for (const delId of ids) {
                  dispatch({ type: 'REMOVE_NODE', id: delId });
                }
              }}
              onEdgesDelete={(ids) => {
                for (const edgeId of ids) {
                  dispatch({ type: 'DISCONNECT', id: edgeId });
                }
              }}
            />
          </div>

          <div className="editor-sidebar-right">
            <h3>Inspector</h3>
            <Inspector
              selectedNodeId={state.selectedNodeId}
              playbook={state.playbook}
              onUpdateNode={(nid, patch) => dispatch({ type: 'UPDATE_NODE', id: nid, patch })}
              onUpdateGates={(gates) => dispatch({ type: 'UPDATE_GATES', gates })}
              onUpdateLimits={(limits) => dispatch({ type: 'UPDATE_LIMITS', limits })}
              onRenameNode={(oldId, newId) => dispatch({ type: 'RENAME_NODE', oldId, newId })}
            />

            <hr style={{ borderColor: 'var(--line-soft)', margin: '16px 0' }} />

            <h3>Validation</h3>
            <ValidationPanel
              issues={validationIssues}
              isValidating={validating}
              onSelectNode={(nid) => dispatch({ type: 'SELECT_NODE', id: nid })}
            />

            <hr style={{ borderColor: 'var(--line-soft)', margin: '16px 0' }} />

            <h3>Dry Run</h3>
            <DryRunPanel
              isRunning={dryRunRunning}
              onRun={async (scenarioText) => {
                setDryRunRunning(true);
                try {
                  const scenario = JSON.parse(scenarioText);
                  const yaml = YAML.stringify(state.playbook);
                  const res = await apiPost<DryRunResponse>('/api/playbooks/dry-run', { yaml, scenario });
                  setDryRunTrace(res.trace || []);
                  return res;
                } finally {
                  setDryRunRunning(false);
                }
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
