import { useState, useEffect, useReducer, useMemo, useContext, useCallback, useRef } from 'react';
import { useParams, useSearchParams, useBlocker, UNSAFE_DataRouterContext } from 'react-router-dom';
import YAML from 'yaml';
import { Button } from '../../ui/Button';
import { FlowCanvas } from '../../canvas/FlowCanvas';
import { playbookToGraph } from '../../canvas/convert';
import { layoutGraph } from '../../canvas/layout';
import { editorReducer, initialEditorState } from './state';
import { Palette } from './Palette';
import { Inspector } from './Inspector';
import { ValidationPanel } from './ValidationPanel';
import { DryRunPanel, DEFAULT_SCENARIO } from './DryRunPanel';
import { apiPost, apiGet } from '../../api/client';
import { ValidateResponse, DryRunResponse, SaveResponse, ValidateIssue, PlaybookDetail } from '../../api/types';
import './editor.css';

function DirtyBlocker({ when }: { when: boolean }) {
  const blocker = useBlocker(when);
  useEffect(() => {
    if (blocker.state === 'blocked') {
      if (window.confirm('You have unsaved changes. Leave this page and discard them?')) blocker.proceed();
      else blocker.reset();
    }
  }, [blocker]);
  return null;
}

function parsePlaybookYaml(text: string): { playbook?: any; error?: string } {
  try {
    const pb = YAML.parse(text);
    if (pb === null || typeof pb !== 'object' || Array.isArray(pb)) {
      return { error: 'Playbook YAML must be a mapping' };
    }
    for (const key of ['nodes', 'edges']) {
      const v = pb[key];
      if (v !== undefined && v !== null && (typeof v !== 'object' || Array.isArray(v))) {
        return { error: `"${key}" must be a mapping` };
      }
    }
    return { playbook: { ...pb, nodes: pb.nodes ?? {}, edges: pb.edges ?? {} } };
  } catch (e: any) {
    return { error: e.message ?? 'Invalid YAML' };
  }
}

export function EditorPage() {
  const { id } = useParams<{ id: string }>();
  const [search] = useSearchParams();
  const hasDataRouter = !!useContext(UNSAFE_DataRouterContext);
  const isNew = !id || id === 'new';

  const [state, dispatch] = useReducer(editorReducer, undefined, () => initialEditorState());
  const { playbook } = state;
  const pbId: string = playbook.id || id || 'new-playbook';

  const [loadError, setLoadError] = useState<string | null>(null);
  const [yamlMode, setYamlMode] = useState(false);
  const [yamlText, setYamlText] = useState('');
  const [yamlError, setYamlError] = useState<string | null>(null);

  const [validating, setValidating] = useState(false);
  const [issues, setIssues] = useState<ValidateIssue[]>([]);
  const [validateError, setValidateError] = useState<string | null>(null);

  const [scenarioText, setScenarioText] = useState(DEFAULT_SCENARIO);
  const [dryRunning, setDryRunning] = useState(false);
  const [dryResult, setDryResult] = useState<DryRunResponse | null>(null);
  const [dryError, setDryError] = useState<string | null>(null);

  const [saveOpen, setSaveOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [repo, setRepo] = useState(search.get('repo') ?? '');
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedPr, setSavedPr] = useState<{ number: number; url: string } | null>(null);

  const [moved, setMoved] = useState<Record<string, { x: number; y: number }>>({});
  const [basePos, setBasePos] = useState<{ key: string; pos: Record<string, { x: number; y: number }> } | null>(null);

  const repoParam = search.get('repo');

  // Load the playbook
  useEffect(() => {
    let active = true;
    if (isNew) {
      dispatch({ type: 'SET_PLAYBOOK', playbook: initialEditorState().playbook });
      return;
    }
    const q = repoParam ? `?repo=${encodeURIComponent(repoParam)}` : '';
    apiGet<PlaybookDetail>(`/api/playbooks/${encodeURIComponent(id || '')}${q}`)
      .then((data) => {
        if (!active) return;
        const res = parsePlaybookYaml(data.yaml);
        if (res.playbook) dispatch({ type: 'SET_PLAYBOOK', playbook: res.playbook });
        else setLoadError(res.error ?? 'Could not parse playbook');
      })
      .catch((e) => active && setLoadError(e.detail || e.message || 'Failed to load playbook'));
    return () => { active = false; };
  }, [id, isNew, repoParam]);

  const yamlOfPlaybook = useMemo(() => YAML.stringify(playbook), [playbook]);

  const runValidate = useCallback(async (yaml: string, isActive: () => boolean = () => true) => {
    setValidating(true);
    try {
      const res = await apiPost<ValidateResponse>('/api/playbooks/validate', { yaml });
      if (!isActive()) return;
      setIssues(res.issues ?? []);
      setValidateError(null);
    } catch (e: any) {
      if (isActive()) setValidateError(e.detail || e.message || 'Validation failed');
    } finally {
      if (isActive()) setValidating(false);
    }
  }, []);

  // Debounced validation (400ms)
  useEffect(() => {
    let active = true;
    const timer = setTimeout(() => { void runValidate(yamlOfPlaybook, () => active); }, 400);
    return () => { active = false; clearTimeout(timer); };
  }, [yamlOfPlaybook, runValidate]);

  // Browser-level guard (tab close / reload)
  useEffect(() => {
    if (!state.isDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [state.isDirty]);

  const toggleYaml = () => {
    if (!yamlMode) {
      setYamlText(yamlOfPlaybook);
      setYamlError(null);
    }
    setYamlMode(!yamlMode);
  };

  const handleYamlChange = (val: string) => {
    setYamlText(val);
    const res = parsePlaybookYaml(val);
    if (res.playbook) {
      setYamlError(null);
      dispatch({ type: 'SYNC_FROM_YAML', playbook: res.playbook });
    } else {
      setYamlError(res.error ?? 'Invalid YAML'); // canvas state is left untouched
    }
  };

  const runDry = async () => {
    setDryError(null);
    let scenario: unknown;
    try {
      scenario = JSON.parse(scenarioText);
    } catch (e: any) {
      setDryError(`Invalid scenario JSON: ${e.message}`);
      return;
    }
    setDryRunning(true);
    try {
      setDryResult(await apiPost<DryRunResponse>('/api/playbooks/dry-run', { yaml: yamlOfPlaybook, scenario }));
    } catch (e: any) {
      setDryError(e.detail || e.message || 'Dry run failed');
    } finally {
      setDryRunning(false);
    }
  };

  const hasErrors = issues.some((i) => i.severity === 'error');
  const savePath = `.harness/playbooks/${pbId}.yaml`;
  const canSave = state.isDirty && !hasErrors && !yamlError && !saving;

  const confirmSave = async () => {
    if (!repo.trim()) {
      setSaveError('Enter the target repository (owner/name)');
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const res = await apiPost<SaveResponse>('/api/playbooks/save', { repo: repo.trim(), id: pbId, yaml: yamlOfPlaybook });
      setSavedPr(res.pr ?? null);
      dispatch({ type: 'MARK_SAVED' });
      setSaveOpen(false);
    } catch (e: any) {
      setSaveError(e.detail || e.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const { nodes, edges } = useMemo(() => playbookToGraph(playbook), [playbook]);

  const graphKey = useMemo(() => nodes.map((n) => n.id).join('|') + '#' + edges.map((e) => e.id).join('|'), [nodes, edges]);
  const hasMoved = Object.keys(moved).length > 0;
  const layoutRef = useRef(0);
  useEffect(() => {
    if (!hasMoved) return;
    const ticket = ++layoutRef.current;
    layoutGraph(nodes, edges, { direction: 'RIGHT' }).then((pos) => {
      if (ticket === layoutRef.current) setBasePos({ key: graphKey, pos });
    });
  }, [hasMoved, nodes, edges, graphKey]);
  const positions = hasMoved && basePos?.key === graphKey ? { ...basePos.pos, ...moved } : undefined;

  const activeNodes = useMemo(() => {
    const errorNodes = new Set(issues.filter((i) => i.severity === 'error' && i.node).map((i) => i.node));
    const traced = new Set((dryResult?.trace ?? []).map((t) => t.node));
    return nodes.map((n) => ({
      ...n,
      status: errorNodes.has(n.id) ? ('failed' as const) : traced.has(n.id) ? ('running' as const) : n.status,
    }));
  }, [nodes, issues, dryResult]);

  const setPlaybookId = (value: string) => dispatch({ type: 'SYNC_FROM_YAML', playbook: { ...playbook, id: value } });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      {hasDataRouter && <DirtyBlocker when={state.isDirty} />}
      <div className="editor-top-bar">
        <div className="editor-title">
          {isNew ? (
            <input
              aria-label="Playbook ID"
              className="field-input"
              style={{ fontFamily: '"JetBrains Mono", monospace', width: 220 }}
              value={playbook.id ?? ''}
              onChange={(e) => setPlaybookId(e.target.value)}
            />
          ) : (
            <span>{pbId}</span>
          )}
          {state.isDirty && <span className="mono" style={{ color: 'var(--accent)', fontSize: 11 }}>MODIFIED</span>}
        </div>
        <div className="editor-actions">
          <Button onClick={() => dispatch({ type: 'UNDO' })} disabled={state.history.length === 0}>Undo</Button>
          <Button onClick={() => dispatch({ type: 'REDO' })} disabled={state.future.length === 0}>Redo</Button>
          <Button onClick={() => void runValidate(yamlOfPlaybook)}>Validate</Button>
          <Button onClick={() => void runDry()} disabled={dryRunning}>Dry run</Button>
          <Button variant="primary" disabled={!canSave} onClick={() => { setSaveError(null); setSaveOpen(true); }}>Save as PR</Button>
          <Button onClick={toggleYaml}>{yamlMode ? 'Canvas' : 'YAML'}</Button>
        </div>
      </div>

      {loadError && <div role="alert" className="editor-banner">Failed to load playbook: {loadError}</div>}

      {saveOpen && (
        <div className="editor-banner" role="dialog" aria-label="Confirm save">
          <div>
            Open a pull request in{' '}
            <input
              aria-label="Target repository"
              className="field-input"
              style={{ fontFamily: '"JetBrains Mono", monospace', width: 240, display: 'inline-block' }}
              placeholder="owner/name"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
            />{' '}
            writing <span className="mono">{savePath}</span>
          </div>
          {saveError && <div role="alert" style={{ color: 'var(--accent)' }}>{saveError}</div>}
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <Button variant="primary" onClick={() => void confirmSave()} disabled={saving}>{saving ? 'Saving...' : 'Confirm'}</Button>
            <Button onClick={() => setSaveOpen(false)}>Cancel</Button>
          </div>
        </div>
      )}

      {savedPr && (
        <div className="editor-banner">
          Saved. Pull request:{' '}
          <a href={savedPr.url} target="_blank" rel="noreferrer">PR #{savedPr.number}</a>
        </div>
      )}

      {yamlMode ? (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
          {yamlError && <div role="alert" className="editor-banner" style={{ color: 'var(--accent)' }}>YAML error: {yamlError}</div>}
          <textarea
            aria-label="Playbook YAML"
            className="editor-yaml-view"
            value={yamlText}
            onChange={(e) => handleYamlChange(e.target.value)}
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
              positions={positions}
              selectedId={state.selectedNodeId}
              onSelect={(nodeId) => {
                if (nodeId !== state.selectedNodeId) dispatch({ type: 'SELECT_NODE', id: nodeId });
              }}
              onConnect={({ source, sourcePort, target }) => dispatch({ type: 'CONNECT', source, port: sourcePort, target })}
              onNodesDelete={(ids) => ids.forEach((nid) => dispatch({ type: 'REMOVE_NODE', id: nid }))}
              onEdgesDelete={(ids) => ids.forEach((eid) => dispatch({ type: 'DISCONNECT', id: eid }))}
              onNodeMove={(nid, pos) => setMoved((m) => ({ ...m, [nid]: pos }))}
            />
          </div>

          <div className="editor-sidebar-right">
            <h3>Inspector</h3>
            <Inspector
              selectedNodeId={state.selectedNodeId}
              playbook={playbook}
              onUpdateNode={(nid, patch) => dispatch({ type: 'UPDATE_NODE', id: nid, patch })}
              onUpdateGates={(gates) => dispatch({ type: 'UPDATE_GATES', gates })}
              onUpdateLimits={(limits) => dispatch({ type: 'UPDATE_LIMITS', limits })}
              onRenameNode={(oldId, newId) => dispatch({ type: 'RENAME_NODE', oldId, newId })}
            />

            <hr style={{ borderColor: 'var(--line-soft)', margin: '16px 0' }} />

            <h3>Validation</h3>
            {validateError && <div role="alert" style={{ padding: 16, color: 'var(--accent)', fontSize: 12 }}>{validateError}</div>}
            <ValidationPanel
              issues={issues}
              isValidating={validating}
              onSelectNode={(nid) => dispatch({ type: 'SELECT_NODE', id: nid })}
            />

            <hr style={{ borderColor: 'var(--line-soft)', margin: '16px 0' }} />

            <h3>Dry Run</h3>
            <DryRunPanel
              scenarioText={scenarioText}
              onScenarioChange={setScenarioText}
              onRun={() => void runDry()}
              isRunning={dryRunning}
              result={dryResult}
              error={dryError}
            />
          </div>
        </div>
      )}
    </div>
  );
}
