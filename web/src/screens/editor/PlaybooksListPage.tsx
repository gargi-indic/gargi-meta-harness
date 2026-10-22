import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Page } from '../../app/Page';
import { apiGet } from '../../api/client';
import { PlaybookSummary } from '../../api/types';
import { Button } from '../../ui/Button';
import { DataTable, Column } from '../../ui/DataTable';
import { StatusTag } from '../../ui/StatusTag';

export function PlaybooksListPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const repo = params.get('repo') ?? '';
  const suffix = repo ? `?repo=${encodeURIComponent(repo)}` : '';

  const { data, isLoading, error } = useQuery<PlaybookSummary[]>({
    queryKey: ['playbooks', repo],
    queryFn: () => apiGet(`/api/playbooks${suffix}`),
  });

  const columns: Column<PlaybookSummary>[] = [
    {
      key: 'id',
      header: 'ID',
      render: (pb) => (
        <Link to={`/playbooks/${encodeURIComponent(pb.id)}${suffix}`} className="text-ink hover:underline">
          {pb.id}
        </Link>
      ),
    },
    { key: 'source', header: 'Source', render: (pb) => <span className="mono">{pb.source}</span> },
    {
      key: 'valid',
      header: 'Valid',
      render: (pb) => (pb.valid ? <StatusTag tone="ok">valid</StatusTag> : <StatusTag tone="fail">invalid</StatusTag>),
    },
  ];

  return (
    <Page title="Playbooks">
      <div className="p-8" style={{ maxWidth: '1200px' }}>
        <div style={{ marginBottom: 16 }}>
          <Button variant="primary" onClick={() => navigate(`/playbooks/new${suffix}`)}>New playbook</Button>
        </div>
        {isLoading && <div className="mono">Loading...</div>}
        {error && <div className="mono text-accent">Error: {(error as Error).message}</div>}
        {data && <DataTable columns={columns} rows={data} rowKey={(pb) => `${pb.source}:${pb.path}`} />}
      </div>
    </Page>
  );
}
