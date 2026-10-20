import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useInbox, useEnabledAgents, queryKeys } from '../../api/queries';
import { apiPost, ApiError } from '../../api/client';
import { InboxItem } from '../../api/types';
import { Panel } from '../../ui/Panel';
import { Button } from '../../ui/Button';
import { DataTable } from '../../ui/DataTable';
import { Empty } from '../../ui/Empty';
import { StatusTag, Tone } from '../../ui/StatusTag';
import './inbox.css';

function InboxItemPanel({ item }: { item: InboxItem }) {
  const queryClient = useQueryClient();
  const agentsQuery = useEnabledAgents(item.repo);

  const [inFlight, setInFlight] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  const [showNote, setShowNote] = useState(false);
  const [noteText, setNoteText] = useState("");

  const [showReject, setShowReject] = useState(false);
  const [rejectText, setRejectText] = useState("");

  const [rerouteAgent, setRerouteAgent] = useState("");

  const executeOptimistic = async (action: () => Promise<void>) => {
    if (inFlight) return;
    setInFlight(true);
    setErrorText(null);

    // Backup
    const previousItems = queryClient.getQueryData<InboxItem[]>(queryKeys.inbox);

    // Optimistic removal
    queryClient.setQueryData<InboxItem[]>(queryKeys.inbox, (old) => {
      if (!old) return old;
      return old.filter(i => i.id !== item.id);
    });

    try {
      await action();
      queryClient.invalidateQueries({ queryKey: queryKeys.inbox });
      queryClient.invalidateQueries({ queryKey: ["board"] });
    } catch (err: any) {
      // Rollback
      queryClient.setQueryData<InboxItem[]>(queryKeys.inbox, previousItems);

      if (err instanceof ApiError) {
        setErrorText(err.detail);
      } else {
        setErrorText(String(err));
      }
      setInFlight(false);
    }
  };

  const handleApprove = () => executeOptimistic(() =>
    apiPost(`/api/inbox/${encodeURIComponent(item.id)}/approve`, noteText.trim() ? { note: noteText.trim() } : {})
  );

  const handleReject = () => executeOptimistic(() =>
    apiPost(`/api/inbox/${encodeURIComponent(item.id)}/reject`, { message: rejectText.trim() })
  );

  const handleReroute = () => executeOptimistic(() =>
    apiPost(`/api/inbox/${encodeURIComponent(item.id)}/reroute`, { agent: rerouteAgent })
  );

  const checkColumns = [
    { key: "name", header: "Name" },
    {
      key: "state",
      header: "State",
      render: (c: any) => {
        const s = c.state.toLowerCase();
        let tone: Tone = "muted";
        if (s.match(/fail|failure|error|timed_out/)) tone = "fail";
        if (s.match(/pass|success/)) tone = "ok";
        return <span className="mono"><StatusTag tone={tone}>{c.state}</StatusTag></span>;
      }
    }
  ];

  return (
    <Panel title={`${item.issue ? `#${item.issue}` : ''} ${item.title}`.trim()}>
      <div className="inbox-item-meta mono">
        <span>{item.repo}</span>
        <span>{item.agent || 'no-agent'}</span>
        <span>{item.created_at}</span>
      </div>

      {item.summary && <div className="inbox-item-summary mt-16"><pre>{item.summary}</pre></div>}

      {item.pr && (
        <div className="inbox-item-pr mt-16">
          <a href={item.pr.url} target="_blank" rel="noreferrer">PR #{item.pr.number}</a>
          <div className="mt-8">
            <DataTable
              columns={checkColumns}
              rows={item.pr.checks}
              rowKey={(c) => c.name}
              empty={<Empty>No checks</Empty>}
            />
          </div>
        </div>
      )}

      {item.findings.length > 0 && (
        <div className="inbox-item-findings mt-16">
          <ul className="findings-list">
            {item.findings.map((f, i) => (
              <li key={i}>
                <StatusTag tone={f.severity === 'blocker' || f.severity === 'major' ? "fail" : "warn"}>
                  {f.severity}
                </StatusTag>
                {' '}
                <span className="finding-text">{f.text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="inbox-item-actions mt-16">
        <div className="action-row">
          <Button variant="primary" onClick={handleApprove} disabled={inFlight}>Approve</Button>
          {!showNote && (
            <button type="button" className="text-btn ml-8" onClick={() => setShowNote(true)}>Add note</button>
          )}
        </div>

        {showNote && (
          <div className="mt-8">
            <div className="field">
              <label htmlFor={`note-${item.id}`} className="field-label">Note</label>
              <input
                id={`note-${item.id}`}
                className="field-input"
                value={noteText}
                onChange={e => setNoteText(e.target.value)}
                disabled={inFlight}
              />
            </div>
          </div>
        )}

        {item.gate_id && (
          <div className="action-row mt-16">
            {!showReject ? (
              <Button variant="secondary" onClick={() => setShowReject(true)} disabled={inFlight}>Reject</Button>
            ) : (
              <div className="reject-form">
                <div className="field">
                  <label htmlFor={`reject-${item.id}`} className="field-label">Reason</label>
                  <textarea
                    id={`reject-${item.id}`}
                    className="field-input"
                    value={rejectText}
                    onChange={e => setRejectText(e.target.value)}
                    disabled={inFlight}
                    rows={3}
                  />
                </div>
                <Button
                  variant="secondary"
                  className="mt-8"
                  onClick={handleReject}
                  disabled={inFlight || !rejectText.trim()}
                >
                  Submit Reject
                </Button>
              </div>
            )}
          </div>
        )}

        {agentsQuery.data && (
          <div className="action-row mt-16 reroute-row">
            <select
              value={rerouteAgent}
              onChange={e => setRerouteAgent(e.target.value)}
              disabled={inFlight}
              className="field-input"
            >
              <option value="">Select agent...</option>
              {agentsQuery.data.agents
                .filter(a => a.name !== item.agent)
                .map(a => <option key={a.name} value={a.name}>{a.name}</option>)
              }
            </select>
            <Button
              variant="secondary"
              onClick={handleReroute}
              disabled={inFlight || !rerouteAgent}
              className="ml-8"
            >
              Reroute
            </Button>
          </div>
        )}

        {errorText && (
          <div className="inbox-error mt-8" role="alert">{errorText}</div>
        )}
      </div>
    </Panel>
  );
}

export function InboxPage() {
  const inboxQuery = useInbox();

  if (inboxQuery.isLoading) return null;
  const items = inboxQuery.data || [];

  if (items.length === 0) {
    return (
      <div className="container">
        <Empty>Nothing waiting on you.</Empty>
      </div>
    );
  }

  // Group by kind
  const order = ["plan_approval", "merge_approval", "gate", "question", "escalation"];
  const groups: Record<string, InboxItem[]> = {};

  items.forEach(item => {
    if (!groups[item.kind]) groups[item.kind] = [];
    groups[item.kind].push(item);
  });

  const sortedKinds = Object.keys(groups).sort((a, b) => {
    const idxA = order.indexOf(a);
    const idxB = order.indexOf(b);
    if (idxA !== -1 && idxB !== -1) return idxA - idxB;
    if (idxA !== -1) return -1;
    if (idxB !== -1) return 1;
    return a.localeCompare(b);
  });

  return (
    <div className="inbox-page container">
      {sortedKinds.map(kind => (
        <div key={kind} className="inbox-group">
          <h2 className="mono inbox-group-header">
            {kind.replace(/_/g, ' ')} {groups[kind].length}
          </h2>
          <div className="inbox-group-items">
            {groups[kind].map(item => (
              <InboxItemPanel key={item.id} item={item} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
