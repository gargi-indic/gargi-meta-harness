import { useState, useEffect, useMemo, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { Page } from '../../app/Page';
import { useRun, useRunActivity, useControlRequest } from '../../api/queries';
import { apiPost } from '../../api/client';
import { FlowCanvas } from '../../canvas/FlowCanvas';
import { playbookToGraph } from '../../canvas/convert';
import { computeOverlay } from './overlay';
import { DataTable, Column } from '../../ui/DataTable';
import { StatusTag } from '../../ui/StatusTag';
import { Button } from '../../ui/Button';
import { Field } from '../../ui/Field';
import { useEventStream, ServerEvent } from '../../api/sse';
import './run.css';
import { Playbook } from '../../generated/playbook';
import { StepOut } from '../../api/types';

function formatDuration(start: string | null, end: string | null): string {
  if (!start) return '-';
  const startTime = new Date(start).getTime();
  const endTime = end ? new Date(end).getTime() : Date.now();
  const diff = endTime - startTime;
  const secs = Math.floor(diff / 1000);
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}m ${s}s`;
}

export function RunPage() {
  const params = useParams();
  const runId = decodeURIComponent(params.id || params['*'] || '');

  const { data: runDetail, isLoading: isLoadingRun, error: errorRun } = useRun(runId);
  const { data: activityPage } = useRunActivity(runId);

  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [controlReqId, setControlReqId] = useState<number | null>(null);
  const { data: controlReq } = useControlRequest(controlReqId);

  const [confirmCancel, setConfirmCancel] = useState(false);
  const [messageText, setMessageText] = useState('');
  const [activities, setActivities] = useState<import('../../api/types').ActivityItem[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);

  const streamRef = useRef<HTMLDivElement>(null);

  // Derive all activities
  const allActivities = useMemo(() => {
    const merged = [...activities, ...(activityPage?.items || [])];
    return Array.from(new Map(merged.map(a => [a.seq, a])).values()).sort((a, b) => a.seq - b.seq);
  }, [activities, activityPage?.items]);

  useEventStream((e: ServerEvent) => {
    if (e.type === 'activity') {
      const act = e.data as import('../../api/types').ActivityItem;
      setActivities(prev => {
        const has = prev.some(p => p.seq === act.seq);
        if (has) return prev;
        return [...prev, act].sort((a, b) => a.seq - b.seq);
      });
    }
  });

  useEffect(() => {
    if (autoScroll && streamRef.current) {
      streamRef.current.scrollTop = streamRef.current.scrollHeight;
    }
  }, [allActivities, autoScroll]);

  const handleScroll = () => {
    if (streamRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = streamRef.current;
      const isAtBottom = scrollTop + clientHeight >= scrollHeight - 10;
      setAutoScroll(isAtBottom);
    }
  };

  const handleControlCall = async (endpoint: string, body: unknown = {}) => {
    try {
      const res = await apiPost<{ request_id: number }>(`/api/runs/${encodeURIComponent(runId)}/${endpoint}`, body);
      setControlReqId(res.request_id);
    } catch (err: any) {
      alert(`Error: ${err.message}`);
    }
  };

  const handlePauseResume = () => {
    if (runDetail?.run.status === 'paused') {
      handleControlCall('resume');
    } else {
      handleControlCall('pause');
    }
  };

  const handleCancel = () => {
    if (confirmCancel) {
      handleControlCall('cancel');
      setConfirmCancel(false);
    } else {
      setConfirmCancel(true);
    }
  };

  const handleRetryNode = () => {
    if (selectedNode) {
      handleControlCall(`nodes/${selectedNode}/retry`);
    }
  };

  const handleSendMessage = () => {
    if (messageText.trim()) {
      handleControlCall('message', { text: messageText.trim() });
      setMessageText('');
    }
  };

  const playbook = runDetail?.playbook;
  const steps = runDetail?.steps;

  const graphData = useMemo(() => {
    if (!playbook) return { nodes: [], edges: [] };
    const { nodes, edges } = playbookToGraph(playbook as unknown as Playbook);
    const overlay = computeOverlay(steps || [], playbook as unknown as Playbook);
    const oNodes = nodes.map(n => ({
      ...n,
      status: overlay[n.id]?.status || 'pending',
      badge: overlay[n.id]?.badge,
    }));
    return { nodes: oNodes, edges };
  }, [playbook, steps]);

  if (isLoadingRun) return <Page title="Run"><div className="p-8 mono">Loading...</div></Page>;
  if (errorRun || !runDetail) return <Page title="Run"><div className="p-8 mono text-accent">Failed to load run</div></Page>;

  const filteredSteps = runDetail.steps.filter(s => !selectedNode || s.node === selectedNode);

  const selectedStepIds = new Set(runDetail.steps.filter(s => s.node === selectedNode).map(s => s.id));
  const activeActivities = selectedNode ? allActivities.filter(a => a.step_id && selectedStepIds.has(a.step_id)) : allActivities;

  const latestSelectedStep = filteredSteps.length > 0 ? filteredSteps[filteredSteps.length - 1] : null;
  const canRetry = selectedNode && (latestSelectedStep?.status === 'failed' || latestSelectedStep?.status === 'done');

  const columns: Column<StepOut>[] = [
    { key: 'node', header: 'Node', render: s => s.node },
    { key: 'visit', header: 'Visit', render: s => <span className="mono">{s.visit}</span> },
    { key: 'status', header: 'Status', render: s => <StatusTag status={s.status} /> },
    { key: 'outcome', header: 'Outcome', render: s => <span className="mono">{s.outcome || '-'}</span> },
    { key: 'duration', header: 'Duration', render: s => <span className="mono">{formatDuration(s.started_at, s.finished_at)}</span> }
  ];

  return (
    <Page title={`Run #${runDetail.run.issue}`}>
      <div className="p-8 flex flex-col h-full">
        <div className="mb-4">
          <h1 className="text-xl mb-2">#{runDetail.run.issue} {runDetail.run.title}</h1>
          <div className="flex gap-4 items-center">
            <StatusTag status={runDetail.run.status} />
            <span className="mono text-sm">{runDetail.run.playbook_id}</span>
            <span className="mono text-sm">{formatDuration(runDetail.run.created_at, runDetail.run.updated_at)}</span>
          </div>
        </div>

        <div className="run-page-grid">
          <div style={{ height: '600px', border: '1px solid var(--line-soft)' }}>
            <FlowCanvas
              nodes={graphData.nodes}
              edges={graphData.edges}
              mode="view"
              selectedId={selectedNode}
              onSelect={setSelectedNode}
            />
          </div>

          <div className="flex flex-col gap-4">
            <div className="run-controls">
              <Button onClick={handlePauseResume}>
                {runDetail.run.status === 'paused' ? 'Resume' : 'Pause'}
              </Button>
              <div>
                <Button onClick={handleCancel}>Cancel</Button>
                {confirmCancel && <span className="inline-confirm">Are you sure? <button className="underline cursor-pointer" onClick={handleCancel}>Yes</button> / <button className="underline cursor-pointer" onClick={() => setConfirmCancel(false)}>No</button></span>}
              </div>
              <Button disabled={!canRetry} onClick={handleRetryNode}>Retry node</Button>
            </div>

            <div>
              <Field
                label="Send message"
                id="message"
                value={messageText}
                onChange={setMessageText}
                multiline
                rows={3}
              />
              <Button disabled={!messageText.trim()} onClick={handleSendMessage} className="mt-2">Send message</Button>
            </div>

            {controlReq && controlReq.status === 'pending' && (
              <div className="mono text-accent text-sm">Action pending...</div>
            )}
            {controlReq && controlReq.status === 'error' && (
              <div className="mono text-accent text-sm">Action failed.</div>
            )}
            {controlReq && controlReq.status === 'done' && (
              <div className="mono text-sm">Action succeeded.</div>
            )}

            <div className="mt-4">
              <h3 className="mb-2">Timeline</h3>
              <DataTable
                columns={columns}
                rows={filteredSteps}
                rowKey={s => s.id}
              />
            </div>

            <div className="mt-4">
              <h3 className="mb-2">Activity Stream</h3>
              <div
                className="activity-stream"
                ref={streamRef}
                onScroll={handleScroll}
              >
                {activeActivities.map(a => (
                  <div key={a.seq} className="activity-row mono text-sm mb-2 pb-2 border-b border-line-soft">
                    <div className="opacity-70">{a.ts ? new Date(a.ts).toLocaleTimeString() : ''} - {a.kind}</div>
                    <div className="whitespace-pre-wrap mt-1">{a.text}</div>
                  </div>
                ))}
                {activeActivities.length === 0 && <div className="mono text-sm opacity-50">No activity.</div>}
              </div>
            </div>
          </div>
        </div>
      </div>
    </Page>
  );
}
