
import { Link } from 'react-router-dom';
import { Page } from '../../app/Page';
import { useRuns } from '../../api/queries';
import { DataTable, Column } from '../../ui/DataTable';
import { StatusTag } from '../../ui/StatusTag';
import { RunSummary } from '../../api/types';

export function RunsListPage() {
  const { data: runs, isLoading, error } = useRuns();

  if (isLoading) return <Page title="Runs"><div className="p-8 mono">Loading...</div></Page>;
  if (error) return <Page title="Runs"><div className="p-8 mono text-accent">Error: {(error as Error).message}</div></Page>;
  if (!runs || runs.length === 0) return <Page title="Runs"><div className="p-8 mono">No runs found.</div></Page>;

  const columns: Column<RunSummary>[] = [
    {
      key: 'issue',
      header: 'Issue',
      render: (r) => (
        <Link to={`/runs/${encodeURIComponent(r.id)}`} className="text-ink hover:underline">
          #{r.issue} {r.title}
        </Link>
      )
    },
    {
      key: 'playbook',
      header: 'Playbook',
      render: (r) => r.playbook_id
    },
    {
      key: 'status',
      header: 'Status',
      render: (r) => <StatusTag status={r.status} />
    },
    {
      key: 'started',
      header: 'Started',
      render: (r) => new Date(r.created_at).toLocaleString()
    }
  ];

  return (
    <Page title="Runs">
      <div className="p-8" style={{ maxWidth: '1200px' }}>
        <DataTable
          columns={columns}
          rows={runs}
          rowKey={(r) => r.id}
        />
      </div>
    </Page>
  );
}
