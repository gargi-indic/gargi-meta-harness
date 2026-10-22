import { describe, test, expect, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, RouterProvider, createMemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { EditorPage } from './EditorPage';
import { mockApi, RouteHandler } from '../../test/fetchMock';

const PB_YAML = `
apiVersion: harness/v1
id: test-pb
nodes:
  agent_1:
    kind: agent
    session: main
    action: plan
edges: {}
`;

const okValidate: RouteHandler = () => ({ status: 200, json: { ok: true, issues: [] } });
const detail: RouteHandler = () => ({ status: 200, json: { id: 'test-pb', source: 'repo', yaml: PB_YAML } });

function wrap(ui: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>;
}

function renderEditor(path = '/playbooks/test-pb') {
  return render(
    wrap(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/playbooks/new" element={<EditorPage />} />
          <Route path="/playbooks/:id" element={<EditorPage />} />
        </Routes>
      </MemoryRouter>
    )
  );
}

const paletteButton = (kind: string) => screen.getAllByText(kind).find((el) => el.tagName === 'BUTTON')!;

async function loaded() {
  await waitFor(() => expect(screen.getAllByText('agent_1').length).toBeGreaterThanOrEqual(1));
}

describe('EditorPage', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test('renders palette, canvas and inspector columns', async () => {
    mockApi({ 'POST /api/playbooks/validate': okValidate, 'GET /api/playbooks/test-pb': detail });
    const { container } = renderEditor();
    await loaded();
    expect(container.querySelector('.editor-grid')).toBeInTheDocument();
    expect(container.querySelector('.editor-palette')).toBeInTheDocument();
    expect(container.querySelector('.editor-canvas-container')).toBeInTheDocument();
    expect(container.querySelector('.editor-sidebar-right')).toBeInTheDocument();
    expect(screen.getByText('Palette')).toBeInTheDocument();
    expect(screen.getByText('Inspector')).toBeInTheDocument();
    expect(screen.getByText('Playbook settings')).toBeInTheDocument();
    expect(screen.queryByText('MODIFIED')).not.toBeInTheDocument();
  });

  test('clicking a palette kind adds a node with a unique id', async () => {
    mockApi({ 'POST /api/playbooks/validate': okValidate, 'GET /api/playbooks/test-pb': detail });
    renderEditor();
    await loaded();
    fireEvent.click(paletteButton('agent'));
    fireEvent.click(paletteButton('agent'));
    await waitFor(() => {
      expect(screen.getAllByText('agent_2').length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText('agent_3').length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText('MODIFIED')).toBeInTheDocument();
  });

  test('inspector renders a form for the node kind and edits update state', async () => {
    mockApi({
      'GET /api/playbooks/test-pb': detail,
      'POST /api/playbooks/validate': () => ({
        status: 200,
        json: { ok: true, issues: [{ severity: 'warn', code: 'W_X', message: 'select me', node: 'agent_1' }] },
      }),
    });
    renderEditor();
    await loaded();
    fireEvent.click(await screen.findByText('select me', {}, { timeout: 3000 }));
    const session = await screen.findByLabelText(/session/i);
    expect(session).toHaveValue('main');
    fireEvent.change(session, { target: { value: 'alt' } });
    await waitFor(() => expect(screen.getByText('MODIFIED')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'YAML' }));
    expect((screen.getByLabelText('Playbook YAML') as HTMLTextAreaElement).value).toContain('session: alt');
  });

  test('error issues disable Save and clicking an issue selects the node', async () => {
    mockApi({
      'GET /api/playbooks/test-pb': detail,
      'POST /api/playbooks/validate': () => ({
        status: 200,
        json: {
          ok: false,
          issues: [{ severity: 'error', code: 'E_BAD', message: 'something is wrong', node: 'agent_1', port: null, path: 'nodes.agent_1' }],
        },
      }),
    });
    renderEditor();
    await loaded();
    fireEvent.click(paletteButton('check')); // make it dirty so only errors can disable Save
    await screen.findByText('E_BAD', {}, { timeout: 3000 });
    expect(screen.getByText('ERROR')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save as PR' })).toBeDisabled();

    expect(screen.queryByLabelText('Node ID')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('something is wrong'));
    expect(await screen.findByLabelText('Node ID')).toHaveValue('agent_1');
  });

  test('Save is disabled when the playbook is unmodified', async () => {
    mockApi({ 'POST /api/playbooks/validate': okValidate, 'GET /api/playbooks/test-pb': detail });
    renderEditor();
    await loaded();
    expect(screen.getByRole('button', { name: 'Save as PR' })).toBeDisabled();
  });

  test('dry run shows trace table and termination status', async () => {
    const dry = vi.fn(() => ({
      status: 200,
      json: { terminated: 'escalated', trace: [{ node: 'agent_1', visit: 1, outcome: 'failed' }], gates_auto_resolved: [], error: null },
    }));
    mockApi({ 'POST /api/playbooks/validate': okValidate, 'GET /api/playbooks/test-pb': detail, 'POST /api/playbooks/dry-run': dry });
    renderEditor();
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByTestId('dry-run-status')).toHaveTextContent('escalated');
    expect(screen.getAllByText('failed').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('agent_1').length).toBeGreaterThanOrEqual(2);
    expect(dry).toHaveBeenCalledWith(expect.objectContaining({ scenario: { outcomes: {}, max_steps: 200 } }));
  });

  test('save asks for confirmation, posts, shows PR link and clears MODIFIED', async () => {
    const save = vi.fn(() => ({ status: 200, json: { pr: { number: 7, url: 'https://github.com/o/r/pull/7' }, branch: 'b', existing: false } }));
    mockApi({
      'POST /api/playbooks/validate': okValidate,
      'GET /api/playbooks/test-pb?repo=acme%2Fwidgets': detail,
      'POST /api/playbooks/save': save,
    });
    renderEditor('/playbooks/test-pb?repo=acme%2Fwidgets');
    await loaded();
    fireEvent.click(paletteButton('check'));
    await screen.findByText('MODIFIED');
    const saveBtn = screen.getByRole('button', { name: 'Save as PR' });
    await waitFor(() => expect(saveBtn).toBeEnabled());
    fireEvent.click(saveBtn);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('.harness/playbooks/test-pb.yaml');
    expect(screen.getByLabelText('Target repository')).toHaveValue('acme/widgets');
    expect(save).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    const link = await screen.findByRole('link', { name: 'PR #7' });
    expect(link).toHaveAttribute('href', 'https://github.com/o/r/pull/7');
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ repo: 'acme/widgets', id: 'test-pb' }));
    expect(screen.queryByText('MODIFIED')).not.toBeInTheDocument();
  });

  test('YAML toggle: valid edits update the canvas, invalid YAML shows an error without wiping state', async () => {
    mockApi({ 'POST /api/playbooks/validate': okValidate, 'GET /api/playbooks/test-pb': detail });
    renderEditor();
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'YAML' }));
    const ta = screen.getByLabelText('Playbook YAML') as HTMLTextAreaElement;
    expect(ta.value).toContain('agent_1');

    fireEvent.change(ta, { target: { value: ta.value.replace('agent_1', 'renamed_node') } });
    expect(screen.queryByText(/YAML error/)).not.toBeInTheDocument();

    fireEvent.change(ta, { target: { value: 'nodes: [unclosed\n  : :' } });
    expect(await screen.findByText(/YAML error/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save as PR' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Canvas' }));
    await waitFor(() => expect(screen.getAllByText('renamed_node').length).toBeGreaterThanOrEqual(1));
    expect(screen.queryByText('agent_1')).not.toBeInTheDocument();
  });

  test('navigating away while dirty prompts and can be cancelled', async () => {
    mockApi({ 'POST /api/playbooks/validate': okValidate, 'GET /api/playbooks/test-pb': detail });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const router = createMemoryRouter(
      [
        { path: '/playbooks/:id', element: <EditorPage /> },
        { path: '/other', element: <div>other page</div> },
      ],
      { initialEntries: ['/playbooks/test-pb'] }
    );
    render(wrap(<RouterProvider router={router} />));
    await loaded();
    fireEvent.click(paletteButton('check'));
    await screen.findByText('MODIFIED');

    await router.navigate('/other');
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(router.state.location.pathname).toBe('/playbooks/test-pb');

    confirm.mockReturnValue(true);
    await router.navigate('/other');
    await waitFor(() => expect(screen.getByText('other page')).toBeInTheDocument());
  });
});
