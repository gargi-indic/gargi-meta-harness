import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { FlowCanvas } from '../../canvas/FlowCanvas';
import { layoutGraph } from '../../canvas/layout';
import type { CanvasNode } from '../../canvas/types';
import { apiPost, ApiError } from '../../api/client';
import {
  errorText,
  plannerKeys,
  sourceReady,
  useJob,
  usePlan,
  usePlannerOptions,
  useProposal,
  type DecomposeAccepted,
  type Draft,
  type JobOut,
  type PlanSource,
  type ProposedIssue,
  type ValidationOut,
} from '../../api/planner';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '../../ui/Button';
import { Empty } from '../../ui/Empty';
import { Field } from '../../ui/Field';
import { Panel } from '../../ui/Panel';
import { StatusTag } from '../../ui/StatusTag';
import { ValidationPanel } from '../editor/ValidationPanel';
import type { ValidateIssue } from '../../api/types';
import { addBlockedBy, edgesOf, nextKey, parseEdgeId, removeBlockedBy, removeIssue } from './graph';
import { PlanPreview } from './PlanPreview';
import { PlannerInspector } from './PlannerInspector';
import { PlannerNode, truncate, type PlannerNodeData } from './PlannerNode';
import { useDraft } from './useDraft';
import './planner.css';

type Pos = Record<string, { x: number; y: number }>;

function sourceOfProposal(repo: string, plan: string): PlanSource {
  return plan.startsWith('#')
    ? { repo, kind: 'issue', value: plan.slice(1) }
    : { repo, kind: 'path', value: plan };
}

function jobLine(label: string, job: JobOut | undefined): string | null {
  return job ? `${label} ${job.status.toUpperCase()}` : null;
}

export function PlannerPage() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const proposalId = params.get('proposal');

  const [repoInput, setRepoInput] = useState<string | null>(null);
  const [kindInput, setKindInput] = useState<'path' | 'issue' | null>(null);
  const [valueInput, setValueInput] = useState<string | null>(null);
  const [guidance, setGuidance] = useState('');
  const [applied, setApplied] = useState<PlanSource | null>(null);

  const proposalQuery = useProposal(proposalId);
  const proposal = proposalQuery.data;
  const fromProposal = proposal ? sourceOfProposal(proposal.repo, proposal.plan) : null;
  const repo = repoInput ?? fromProposal?.repo ?? '';
  const kind = kindInput ?? fromProposal?.kind ?? 'path';
  const value = valueInput ?? fromProposal?.value ?? '';
  const current: PlanSource = { repo: repo.trim(), kind, value: value.trim() };
  const source = applied ?? fromProposal;

  const planQuery = usePlan(source);
  const optionsQuery = usePlannerOptions(source?.repo ?? null);
  const options = optionsQuery.data;

  const { state, dirty, load, edit, clear } = useDraft();
  const { draft } = state;

  useEffect(() => {
    if (proposal) load(proposal);
  }, [proposal, load]);

  // ---- jobs ------------------------------------------------------------------------------------------------
  const [decomposeJobId, setDecomposeJobId] = useState<string | null>(null);
  const [createJobId, setCreateJobId] = useState<string | null>(null);
  const activeDecomposeJob = decomposeJobId ?? (proposalId ? `${proposalId}.decompose` : null);
  const activeCreateJob =
    createJobId ?? (proposalId && (state.status === 'creating' || state.status === 'created') ? `${proposalId}.create` : null);
  const decomposeJob = useJob(activeDecomposeJob).data;
  const createJob = useJob(activeCreateJob).data;

  const [busy, setBusy] = useState<'decompose' | 'validate' | 'approve' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [validated, setValidated] = useState<ValidationOut | null>(null);

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [graphMessage, setGraphMessage] = useState<string | null>(null);

  const issues = useMemo(() => draft?.issues ?? [], [draft]);
  const selected: ProposedIssue | null = issues.find((i) => i.key === selectedKey) ?? null;

  // ---- actions ---------------------------------------------------------------------------------------------

  const decompose = async () => {
    if (!sourceReady(current) || busy) return;
    setBusy('decompose');
    setActionError(null);
    try {
      const body: Record<string, unknown> = { repo: current.repo };
      body[current.kind] = current.kind === 'issue' ? Number(current.value) : current.value;
      if (guidance.trim()) body.guidance = guidance.trim();
      const res = await apiPost<DecomposeAccepted>('/api/planner/decompose', body);
      setApplied(current);
      clear();
      setValidated(null);
      setSelectedKey(null);
      setCreateJobId(null);
      setDecomposeJobId(res.job_id);
      setParams({ proposal: res.proposal_id });
      void queryClient.invalidateQueries({ queryKey: plannerKeys.all });
    } catch (err) {
      setActionError(errorText(err));
      if (err instanceof ApiError && err.status === 409) {
        try {
          const running = JSON.parse(err.detail)?.proposal_id;
          if (typeof running === 'string') {
            setApplied(current);
            setParams({ proposal: running });
          }
        } catch {
          // not JSON: nothing to attach to
        }
      }
    } finally {
      setBusy(null);
    }
  };

  const loadPlan = () => {
    if (!sourceReady(current)) return;
    setApplied(current);
    void queryClient.invalidateQueries({ queryKey: plannerKeys.plan(current) });
  };

  const validate = async () => {
    if (!proposalId || busy) return;
    setBusy('validate');
    setActionError(null);
    try {
      setValidated(await apiPost<ValidationOut>(`/api/planner/proposals/${encodeURIComponent(proposalId)}/validate`, {}));
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(null);
    }
  };

  const approve = async () => {
    if (!proposalId || busy) return;
    setBusy('approve');
    setActionError(null);
    try {
      const job = await apiPost<JobOut>(`/api/planner/proposals/${encodeURIComponent(proposalId)}/approve`, {
        rev: state.rev,
      });
      setCreateJobId(job.job_id);
      void queryClient.invalidateQueries({ queryKey: plannerKeys.all });
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(null);
    }
  };

  const reload = async () => {
    const fresh = await proposalQuery.refetch();
    if (fresh.data) load(fresh.data, true);
  };

  // ---- draft edits -----------------------------------------------------------------------------------------

  const updateIssue = (key: string, patch: Partial<ProposedIssue>) =>
    edit((d) => ({ ...d, issues: d.issues.map((i) => (i.key === key ? { ...i, ...patch } : i)) }));

  const connect = (blocker: string, blocked: string) => {
    if (!draft) return;
    const res = addBlockedBy(draft.issues, blocker, blocked);
    if (!res.ok) {
      setGraphMessage(res.message);
      return;
    }
    setGraphMessage(null);
    edit((d) => ({ ...d, issues: res.issues }));
  };

  const toggleBlocker = (blocked: string, blocker: string, on: boolean) => {
    if (!draft) return;
    if (on) connect(blocker, blocked);
    else {
      setGraphMessage(null);
      edit((d) => ({ ...d, issues: removeBlockedBy(d.issues, blocker, blocked) }));
    }
  };

  const deleteIssues = (keys: string[]) => {
    if (!draft) return;
    if (keys.length >= draft.issues.length) {
      setGraphMessage('A plan needs at least one issue.');
      return;
    }
    setGraphMessage(null);
    edit((d) => ({ ...d, issues: keys.reduce((acc, k) => removeIssue(acc, k), d.issues) }));
    if (selectedKey && keys.includes(selectedKey)) setSelectedKey(null);
  };

  const addIssue = () => {
    if (!draft) return;
    const key = nextKey(draft.issues);
    const agent = options?.agents.find((a) => a.enabled)?.name ?? '';
    const playbook = options?.playbooks.includes('plan-build-review')
      ? 'plan-build-review'
      : (options?.playbooks[0] ?? '');
    const fresh: ProposedIssue = {
      key,
      title: 'New issue',
      body: '## Context\n\n## Scope\n\n## Acceptance criteria\n',
      acceptance: [''],
      blocked_by: [],
      agent,
      playbook,
      paths: [],
    };
    setGraphMessage(null);
    edit((d) => ({ ...d, issues: [...d.issues, fresh] }));
    setSelectedKey(key);
  };

  // ---- canvas ----------------------------------------------------------------------------------------------

  const serverIssues = useMemo(() => {
    if (validated && validated.rev === state.rev && !dirty) return validated.issues;
    return state.issues;
  }, [validated, state.rev, state.issues, dirty]);

  const invalidKeys = useMemo(
    () => new Set(serverIssues.map((p) => p.key).filter((k): k is string => !!k)),
    [serverIssues],
  );

  const canvasNodes: CanvasNode[] = useMemo(
    () =>
      issues.map((i) => ({
        id: i.key,
        kind: 'issue',
        label: truncate(i.title),
        data: { agent: i.agent, invalid: invalidKeys.has(i.key), selected: i.key === selected?.key } satisfies PlannerNodeData,
      })),
    [issues, invalidKeys, selected?.key],
  );
  const canvasEdges = useMemo(() => edgesOf(issues), [issues]);

  // The layout is recomputed only when the shape of the graph changes, not while a title is typed.
  const shapeKey = useMemo(
    () => issues.map((i) => i.key).join('|') + '#' + canvasEdges.map((e) => e.id).join('|'),
    [issues, canvasEdges],
  );
  const [layout, setLayout] = useState<{ key: string; pos: Pos } | null>(null);
  useEffect(() => {
    if (issues.length === 0) return;
    let active = true;
    layoutGraph(canvasNodes, canvasEdges, { direction: 'DOWN' }).then((pos) => {
      if (active) setLayout({ key: shapeKey, pos });
    });
    return () => {
      active = false;
    };
    // canvasNodes only matter for their ids and kinds here, which shapeKey covers
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shapeKey]);
  const positions = layout && layout.key === shapeKey ? layout.pos : undefined;

  // FlowCanvas fits the view itself only when it lays the graph out; with the positions passed in (so a typed title
  // does not move the view) the Controls' own "fit view" button is pressed when the shape of the graph changes.
  const canvasRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!positions) return;
    const timer = setTimeout(() => {
      canvasRef.current?.querySelector<HTMLButtonElement>('.react-flow__controls-fitview')?.click();
    }, 80);
    return () => clearTimeout(timer);
  }, [positions]);

  const renderNode = useCallback((n: CanvasNode) => <PlannerNode node={n} />, []);

  // ---- approve preconditions -------------------------------------------------------------------------------

  const plan = planQuery.data;
  const blockers: string[] = [];
  if (!draft) blockers.push('No proposal yet.');
  else {
    if (state.status === 'drafting') blockers.push('The decomposition is still running.');
    if (state.status === 'creating' || state.status === 'created') blockers.push('The issues are already being created.');
    if (dirty) blockers.push('Unsaved edits.');
    if (state.saving) blockers.push('Saving the draft.');
    if (!state.saving && !dirty && !state.serverSaved) blockers.push('The draft is not on GitHub yet.');
    if (state.conflict) blockers.push('Draft changed elsewhere.');
    if (serverIssues.length > 0) blockers.push(`${serverIssues.length} validation issue${serverIssues.length === 1 ? '' : 's'}.`);
    if (!plan) blockers.push('The plan is not loaded.');
    else if (plan.approval?.status !== 'approved') blockers.push('The plan is not approved.');
    else if (proposal && proposal.plan_sha !== plan.sha) blockers.push('The plan changed since it was decomposed.');
  }
  const canApprove = blockers.length === 0 && busy === null;

  const panelIssues: ValidateIssue[] = serverIssues.map((p) => ({
    severity: 'error',
    code: p.code,
    message: p.message,
    node: p.key ?? undefined,
  }));

  const saveTag = state.conflict
    ? null
    : dirty || state.saveError
      ? 'UNSAVED'
      : state.saving || !state.serverSaved
        ? 'SAVING'
        : 'SAVED';

  const statusParts = [
    busy === 'decompose' ? 'DECOMPOSE STARTING' : null,
    jobLine('DECOMPOSE', decomposeJob),
    busy === 'validate' ? 'VALIDATING' : null,
    busy === 'approve' ? 'CREATE STARTING' : null,
    jobLine('CREATE', createJob),
  ].filter(Boolean);

  const createFailed = createJob?.status === 'failed';
  const decomposeFailed = decomposeJob?.status === 'failed';
  const planError = planQuery.error ? errorText(planQuery.error) : null;

  return (
    <div className="planner-page">
      <header className="planner-head">
        <h1 className="planner-title">Planner</h1>
      </header>

      <section className="planner-controls" aria-label="Plan source">
        <Field label="Repository" value={repo} placeholder="owner/name" onChange={setRepoInput} />
        <div className="field">
          <label htmlFor="planner-kind" className="field-label">
            Plan from
          </label>
          <select
            id="planner-kind"
            className="field-input"
            value={kind}
            onChange={(e) => setKindInput(e.target.value as 'path' | 'issue')}
          >
            <option value="path">File path</option>
            <option value="issue">Issue number</option>
          </select>
        </div>
        <Field
          label={kind === 'path' ? 'Plan path' : 'Plan issue'}
          value={value}
          placeholder={kind === 'path' ? 'docs/PLAN.md' : '12'}
          onChange={setValueInput}
        />
        <div className="planner-wide">
  <Field label="Guidance" multiline rows={2} value={guidance} onChange={setGuidance} />
        </div>
        <div className="planner-actions">
          <Button onClick={loadPlan} disabled={!sourceReady(current)}>
            Load plan
          </Button>
          <Button variant="primary" onClick={() => void decompose()} disabled={!sourceReady(current) || busy !== null}>
            Decompose
          </Button>
          <Button onClick={() => void validate()} disabled={!proposalId || !draft || busy !== null}>
            Validate
          </Button>
          <Button variant="primary" onClick={() => void approve()} disabled={!canApprove}>
            Approve and create
          </Button>
        </div>
        <p className="mono planner-status" role="status" aria-live="polite">
          {statusParts.length > 0 ? statusParts.join(' / ') : 'IDLE'}
        </p>
        {blockers.length > 0 && draft && (
          <ul className="mono planner-blockers" aria-label="Why Approve and create is disabled">
            {blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        )}
        {actionError && (
          <p role="alert" className="field-error">
            {actionError}
          </p>
        )}
        {decomposeFailed && decomposeJob?.error && (
          <p role="alert" className="field-error">
            Decomposition failed: {decomposeJob.error}
          </p>
        )}
        {createFailed && createJob?.error && (
          <p role="alert" className="field-error">
            Creation failed: {createJob.error}
          </p>
        )}
        {createJob?.status === 'done' && createJob.result && (
          <div className="planner-created" aria-label="Created issues">
            <p className="mono">CREATED</p>
            <ul className="mono">
              <li>
                <a href={`https://github.com/${proposal?.repo ?? current.repo}/issues/${createJob.result.epic_number}`} target="_blank" rel="noreferrer">
                  #{createJob.result.epic_number}
                </a>{' '}
                epic
              </li>
              {Object.entries(createJob.result.issues).map(([key, number]) => (
                <li key={key}>
                  <a href={`https://github.com/${proposal?.repo ?? current.repo}/issues/${number}`} target="_blank" rel="noreferrer">
                    #{number}
                  </a>{' '}
                  {key}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <div className="planner-zones">
        <aside className="planner-zone planner-preview" aria-label="Plan preview">
          <Panel title="Plan">
            <PlanPreview plan={plan} loading={planQuery.isFetching && !plan} error={planError} />
          </Panel>
        </aside>

        <main className="planner-zone planner-canvas-zone" aria-label="Issue graph">
          <div className="planner-canvas-bar">
            <h2 className="planner-zone-title">Issues</h2>
            {saveTag && <StatusTag tone={saveTag === 'SAVED' ? 'ok' : 'warn'}>{saveTag}</StatusTag>}
            <Button onClick={addIssue} disabled={!draft}>
              Add issue
            </Button>
          </div>
          {state.conflict && (
            <div role="alert" className="planner-conflict">
              <span>Draft changed elsewhere. Reload.</span>
              <Button onClick={() => void reload()}>Reload</Button>
            </div>
          )}
          {state.saveError && (
            <p role="alert" className="field-error">
              Save failed: {state.saveError}
            </p>
          )}
          {graphMessage && (
            <p role="alert" className="field-error">
              {graphMessage}
            </p>
          )}
          <div className="planner-canvas" ref={canvasRef}>
            {draft ? (
              <FlowCanvas
                mode="edit"
                direction="DOWN"
                nodes={canvasNodes}
                edges={canvasEdges}
                positions={positions}
                selectedId={selected?.key ?? null}
                onSelect={setSelectedKey}
                onConnect={({ source, target }) => connect(source, target)}
                onNodesDelete={deleteIssues}
                onEdgesDelete={(ids) => {
                  const pairs = ids.map(parseEdgeId).filter((p): p is { blocker: string; blocked: string } => !!p);
                  if (pairs.length === 0) return;
                  setGraphMessage(null);
                  edit((d) => ({
                    ...d,
                    issues: pairs.reduce((acc, p) => removeBlockedBy(acc, p.blocker, p.blocked), d.issues),
                  }));
                }}
                renderNode={renderNode}
              />
            ) : (
              <Empty>{proposal?.status === 'drafting' ? 'Decomposing the plan.' : 'No proposal. Decompose a plan to start.'}</Empty>
            )}
          </div>
        </main>

        <aside className="planner-zone planner-inspector" aria-label="Inspector">
          <Panel title="Inspector">
            {draft ? (
              <InspectorBody
                draft={draft}
                selected={selected}
                options={options}
                onSelect={setSelectedKey}
                onChange={(patch) => selected && updateIssue(selected.key, patch)}
                onToggleBlocker={(blocker, on) => selected && toggleBlocker(selected.key, blocker, on)}
                onDelete={() => selected && deleteIssues([selected.key])}
              />
            ) : (
              <Empty>Nothing to inspect.</Empty>
            )}
          </Panel>
          <Panel title="Validation">
            <ValidationPanel issues={panelIssues} isValidating={busy === 'validate'} onSelectNode={setSelectedKey} />
          </Panel>
        </aside>
      </div>
    </div>
  );
}

interface InspectorBodyProps {
  draft: Draft;
  selected: ProposedIssue | null;
  options: ReturnType<typeof usePlannerOptions>['data'];
  onSelect: (key: string | null) => void;
  onChange: (patch: Partial<ProposedIssue>) => void;
  onToggleBlocker: (blocker: string, on: boolean) => void;
  onDelete: () => void;
}

function InspectorBody({ draft, selected, options, onSelect, onChange, onToggleBlocker, onDelete }: InspectorBodyProps) {
  return (
    <div className="planner-inspector-body">
      <p className="planner-epic">{draft.epic_title}</p>
      <div className="field">
        <label htmlFor="planner-select" className="field-label">
          Selected issue
        </label>
        <select
          id="planner-select"
          className="field-input"
          value={selected?.key ?? ''}
          onChange={(e) => onSelect(e.target.value || null)}
        >
          <option value="">(none)</option>
          {draft.issues.map((i) => (
            <option key={i.key} value={i.key}>
              {i.key}
            </option>
          ))}
        </select>
      </div>
      {selected ? (
        <PlannerInspector
          key={selected.key}
          issue={selected}
          issues={draft.issues}
          options={options}
          onChange={onChange}
          onToggleBlocker={onToggleBlocker}
          onDelete={onDelete}
        />
      ) : (
        <Empty>Select an issue on the canvas or in the list.</Empty>
      )}
    </div>
  );
}
