import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  CREATE_JOB_URL,
  DECOMPOSE_JOB_URL,
  OPTIONS_URL,
  PID,
  PLAN_URL,
  PROPOSAL_URL,
  REPO,
  TestEventSource,
  installEventSource,
  jobOut,
  optionsOut,
  planOut,
  plannerFetch,
  proposalOut,
  renderPlanner,
  type Handler,
} from './testkit';
import type { ProposalOut, PlanOut, JobOut } from '../../api/planner';

// The canvas is covered by PlannerCanvas.test.tsx and the Playwright smoke; here it is a double that records its props.
const canvas = vi.hoisted(() => ({ props: null as any }));
vi.mock('../../canvas/FlowCanvas', () => ({
  FlowCanvas: (props: any) => {
    canvas.props = props;
    return (
      <div data-testid="canvas" data-edges={props.edges.map((e: any) => e.id).join(',')}>
        {props.nodes.map((n: any) => (
          <button key={n.id} data-testid="node" data-invalid={String(n.data.invalid)} onClick={() => props.onSelect(n.id)}>
            {n.id}
          </button>
        ))}
      </div>
    );
  },
}));

interface World {
  proposal: ProposalOut;
  plan: PlanOut | null;
  decomposeJob: JobOut;
  createJob: JobOut;
}

function world(over: Partial<World> = {}): World {
  return {
    proposal: proposalOut(),
    plan: planOut(),
    decomposeJob: jobOut({ job_id: `${PID}.decompose`, kind: 'decompose', status: 'done' }),
    createJob: jobOut(),
    ...over,
  };
}

function routes(w: World, extra: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    [`GET ${PROPOSAL_URL}`]: () => ({ status: 200, json: w.proposal }),
    [`GET ${PLAN_URL}`]: () =>
      w.plan ? { status: 200, json: w.plan } : { status: 404, json: { detail: 'plan file not found' } },
    [`GET ${OPTIONS_URL}`]: () => ({ status: 200, json: optionsOut }),
    [`GET ${DECOMPOSE_JOB_URL}`]: () => ({ status: 200, json: w.decomposeJob }),
    [`GET ${CREATE_JOB_URL}`]: () => ({ status: 200, json: w.createJob }),
    ...extra,
  };
}

const approveButton = () => screen.getByRole('button', { name: 'Approve and create' });

async function open(w: World, extra: Record<string, Handler> = {}) {
  const kit = plannerFetch(routes(w, extra));
  renderPlanner(`/planner?proposal=${encodeURIComponent(PID)}`);
  await waitFor(() => expect(screen.getAllByTestId('node')).toHaveLength(w.proposal.proposal?.issues.length ?? 0));
  return kit;
}

async function openReady(w: World = world(), extra: Record<string, Handler> = {}) {
  const kit = await open(w, extra);
  await waitFor(() => expect(approveButton()).toBeEnabled());
  return kit;
}

describe('PlannerPage', () => {
  beforeEach(() => {
    installEventSource();
    canvas.props = null;
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('Decompose posts to /api/planner/decompose; a planner.updated event loads the proposal as nodes and edges', async () => {
    const w = world({
      proposal: proposalOut({ status: 'drafting', proposal: null, rev: 1 }),
      decomposeJob: jobOut({ job_id: `${PID}.decompose`, kind: 'decompose', status: 'running' }),
    });
    const kit = plannerFetch(
      routes(w, {
        'POST /api/planner/decompose': () => ({
          status: 202,
          json: { proposal_id: PID, job_id: `${PID}.decompose` },
        }),
      }),
    );
    renderPlanner();
    const user = userEvent.setup();

    expect(screen.getByRole('button', { name: 'Decompose' })).toBeDisabled();
    await user.type(screen.getByLabelText('Repository'), REPO);
    await user.type(screen.getByLabelText('Plan path'), 'docs/PLAN.md');
    await user.type(screen.getByLabelText('Guidance'), 'Keep it small');
    await user.click(screen.getByRole('button', { name: 'Decompose' }));

    await waitFor(() => expect(kit.count('POST', '/api/planner/decompose')).toBe(1));
    const post = kit.calls.find((c) => c.method === 'POST')!;
    expect(post.body).toEqual({ repo: REPO, path: 'docs/PLAN.md', guidance: 'Keep it small' });

    expect(await screen.findByRole('status')).toHaveTextContent('DECOMPOSE RUNNING');
    expect(screen.queryAllByTestId('node')).toHaveLength(0);

    w.proposal = proposalOut();
    w.decomposeJob = jobOut({ job_id: `${PID}.decompose`, kind: 'decompose', status: 'done' });
    act(() => TestEventSource.last!.emit('planner.updated', { proposal_id: PID }));

    await waitFor(() => expect(screen.getAllByTestId('node')).toHaveLength(4));
    expect(screen.getByTestId('canvas')).toHaveAttribute('data-edges', 'a>b,b>c');
    expect(screen.getByRole('status')).toHaveTextContent('DECOMPOSE DONE');
  });

  it('can start from an issue number as well as a file path', async () => {
    const kit = plannerFetch({
      'POST /api/planner/decompose': () => ({ status: 409, json: { detail: { reason: 'x', message: 'busy' } } }),
    });
    renderPlanner();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Repository'), REPO);
    await user.selectOptions(screen.getByLabelText('Plan from'), 'issue');
    await user.type(screen.getByLabelText('Plan issue'), '12');
    await user.click(screen.getByRole('button', { name: 'Decompose' }));
    await waitFor(() => expect(kit.calls.some((c) => c.method === 'POST')).toBe(true));
    expect(kit.calls.find((c) => c.method === 'POST')!.body).toEqual({ repo: REPO, issue: 12 });
    expect(await screen.findByText('busy')).toBeInTheDocument();
  });

  it('shows the plan preview with the sha and approval status as tags', async () => {
    await open(world());
    expect(await screen.findByText('SHA sha111111111')).toBeInTheDocument();
    expect(screen.getByText('PLAN APPROVED')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Steps' })).toBeInTheDocument();
  });

  it('editing an issue marks UNSAVED, then sends one debounced PUT with If-Match and shows SAVED', async () => {
    let release: (() => void) | undefined;
    const kit = await openReady(world(), {
      [`PUT ${PROPOSAL_URL}`]: (req) =>
        new Promise((resolve) => {
          release = () =>
            resolve({
              status: 200,
              json: proposalOut({ rev: 4, proposal: req.body }),
            });
        }),
    });
    await userEvent.setup().click(screen.getAllByTestId('node')[0]);
    expect(await screen.findByText('SAVED')).toBeInTheDocument();

    const title = screen.getByLabelText('Title');
    fireEvent.change(title, { target: { value: 'Do a, better' } });
    fireEvent.change(title, { target: { value: 'Do a, better still' } });
    expect(screen.getByText('UNSAVED')).toBeInTheDocument();
    expect(approveButton()).toBeDisabled();

    await waitFor(() => expect(kit.count('PUT', PROPOSAL_URL)).toBe(1), { timeout: 3000 });
    const put = kit.calls.find((c) => c.method === 'PUT')!;
    expect(put.headers['If-Match']).toBe('"3"');
    expect(put.body.issues[0].title).toBe('Do a, better still');
    expect(put.body.issues).toHaveLength(4);
    // still UNSAVED until the PUT returns
    expect(screen.getByText('UNSAVED')).toBeInTheDocument();

    act(() => release!());
    expect(await screen.findByText('SAVED')).toBeInTheDocument();
    expect(kit.count('PUT', PROPOSAL_URL)).toBe(1);
  });

  it('changing the agent or the playbook marks UNSAVED until the PUT returns', async () => {
    let release: (() => void) | undefined;
    await openReady(world(), {
      [`PUT ${PROPOSAL_URL}`]: (req) =>
        new Promise((resolve) => {
          release = () => resolve({ status: 200, json: proposalOut({ rev: 4, proposal: req.body }) });
        }),
    });
    const user = userEvent.setup();
    await user.click(screen.getAllByTestId('node')[0]);
    await user.selectOptions(screen.getByLabelText('Agent'), 'claude');
    expect(screen.getByText('UNSAVED')).toBeInTheDocument();
    await waitFor(() => expect(release).toBeDefined(), { timeout: 3000 });
    expect(screen.getByText('UNSAVED')).toBeInTheDocument();
    act(() => release!());
    expect(await screen.findByText('SAVED')).toBeInTheDocument();

    release = undefined;
    await user.selectOptions(screen.getByLabelText('Playbook'), 'tournament');
    expect(screen.getByText('UNSAVED')).toBeInTheDocument();
    await waitFor(() => expect(release).toBeDefined(), { timeout: 3000 });
    act(() => release!());
    expect(await screen.findByText('SAVED')).toBeInTheDocument();
  });

  it('lists a disabled agent as disabled', async () => {
    await open(world());
    await userEvent.setup().click(screen.getAllByTestId('node')[0]);
    const codex = await screen.findByRole('option', { name: 'codex (disabled)' });
    expect(codex).toBeDisabled();
    expect(screen.getByRole('option', { name: 'claude' })).toBeEnabled();
  });

  it('a 412 shows the conflict message and blocks Approve; Reload fetches the draft again', async () => {
    const w = world();
    let rejectPut = true;
    const kit = await openReady(w, {
      [`PUT ${PROPOSAL_URL}`]: () =>
        rejectPut
          ? { status: 412, json: { detail: 'the proposal is at rev 5, not 3' } }
          : { status: 200, json: w.proposal },
    });
    await userEvent.setup().click(screen.getAllByTestId('node')[0]);
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'mine' } });

    expect(await screen.findByText('Draft changed elsewhere. Reload.', {}, { timeout: 3000 })).toBeInTheDocument();
    expect(approveButton()).toBeDisabled();
    expect(kit.count('PUT', PROPOSAL_URL)).toBe(1);

    rejectPut = false;
    w.proposal = proposalOut({ rev: 5 });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Reload' }));
    await waitFor(() => expect(screen.queryByText('Draft changed elsewhere. Reload.')).not.toBeInTheDocument());
    await waitFor(() => expect(approveButton()).toBeEnabled());
  });

  it('a failed save shows the error and stays UNSAVED', async () => {
    await openReady(world(), {
      [`PUT ${PROPOSAL_URL}`]: () => ({ status: 422, json: { detail: 'bad draft' } }),
    });
    await userEvent.setup().click(screen.getAllByTestId('node')[0]);
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'x' } });
    expect(await screen.findByText(/Save failed: bad draft/, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByText('UNSAVED')).toBeInTheDocument();
    expect(approveButton()).toBeDisabled();
  });

  describe('Approve and create is disabled for each unmet precondition', () => {
    it('is enabled when every precondition is met', async () => {
      await openReady();
      expect(screen.queryByLabelText('Why Approve and create is disabled')).not.toBeInTheDocument();
    });

    const cases: [string, Partial<World>, string][] = [
      [
        'validation issues',
        {
          proposal: proposalOut({
            status: 'invalid',
            issues_validation: [{ code: 'no_acceptance', key: 'a', message: 'there is no acceptance criterion' }],
          }),
        },
        '1 validation issue.',
      ],
      [
        'plan approval pending',
        { plan: planOut({ approval: { status: 'pending', via: null, approved_by: null, note: null } }) },
        'The plan is not approved.',
      ],
      ['plan without a control issue', { plan: planOut({ approval: null }) }, 'The plan is not approved.'],
      ['plan changed since decomposition', { plan: planOut({ sha: 'other0000000' }) }, 'The plan changed since it was decomposed.'],
      ['plan not loadable', { plan: null }, 'The plan is not loaded.'],
      ['draft not on GitHub yet', { proposal: proposalOut({ saved: false }) }, 'The draft is not on GitHub yet.'],
      ['creation already running', { proposal: proposalOut({ status: 'creating' }) }, 'The issues are already being created.'],
    ];

    it.each(cases)('%s', async (_name, over, reason) => {
      await open(world(over));
      expect(await screen.findByText(reason)).toBeInTheDocument();
      expect(approveButton()).toBeDisabled();
    });

    it('unsaved edits', async () => {
      await openReady(world(), { [`PUT ${PROPOSAL_URL}`]: () => new Promise(() => {}) });
      await userEvent.setup().click(screen.getAllByTestId('node')[0]);
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'x' } });
      expect(await screen.findByText('Unsaved edits.')).toBeInTheDocument();
      expect(approveButton()).toBeDisabled();
    });
  });

  it('Approve posts the reviewed rev and shows the job transitions and the created issues', async () => {
    const w = world();
    const kit = await openReady(w, {
      [`POST ${PROPOSAL_URL}/approve`]: () => ({ status: 202, json: jobOut() }),
    });
    await userEvent.setup().click(approveButton());
    await waitFor(() => expect(kit.count('POST', `${PROPOSAL_URL}/approve`)).toBe(1));
    expect(kit.calls.find((c) => c.url.endsWith('/approve'))!.body).toEqual({ rev: 3 });
    expect(await screen.findByText(/CREATE RUNNING/)).toBeInTheDocument();

    w.createJob = jobOut({
      status: 'done',
      result: {
        epic_number: 20,
        issues: { a: 21, b: 22, c: 23, d: 24 },
        created: ['a', 'b', 'c', 'd'],
        reused: [],
        links_created: 7,
        labels_ensured: [],
      },
    });
    act(() => TestEventSource.last!.emit('planner.updated', { proposal_id: PID }));
    expect(await screen.findByText(/CREATE DONE/)).toBeInTheDocument();
    const created = screen.getByLabelText('Created issues');
    const links = within(created).getAllByRole('link');
    expect(links.map((l) => l.textContent)).toEqual(['#20', '#21', '#22', '#23', '#24']);
    expect(links[1]).toHaveAttribute('href', `https://github.com/${REPO}/issues/21`);
  });

  it('shows why creation failed', async () => {
    const kit = await openReady(world(), {
      [`POST ${PROPOSAL_URL}/approve`]: () => ({
        status: 409,
        json: { detail: { reason: 'plan_pending', message: 'the plan is not approved (pending)' } },
      }),
    });
    await userEvent.setup().click(approveButton());
    expect(await screen.findByText('the plan is not approved (pending)')).toBeInTheDocument();
    expect(kit.count('POST', `${PROPOSAL_URL}/approve`)).toBe(1);
  });

  describe('drawing edges', () => {
    it('adds blocked_by for a valid connection and saves it', async () => {
      const kit = await openReady(world(), {
        [`PUT ${PROPOSAL_URL}`]: (req) => ({ status: 200, json: proposalOut({ rev: 4, proposal: req.body }) }),
      });
      act(() => canvas.props.onConnect({ source: 'd', target: 'c', sourcePort: null }));
      expect(screen.getByTestId('canvas')).toHaveAttribute('data-edges', 'a>b,b>c,d>c');
      expect(screen.getByText('UNSAVED')).toBeInTheDocument();
      await waitFor(() => expect(kit.count('PUT', PROPOSAL_URL)).toBe(1), { timeout: 3000 });
      const issues = kit.calls.find((c) => c.method === 'PUT')!.body.issues;
      expect(issues.find((i: any) => i.key === 'c').blocked_by).toEqual(['b', 'd']);
      expect(await screen.findByText('SAVED')).toBeInTheDocument();
    });

    it.each([
      ['a cycle', { source: 'c', target: 'a' }, /cycle/],
      ['a duplicate', { source: 'a', target: 'b' }, /already blocked/],
      ['a self edge', { source: 'a', target: 'a' }, /itself/],
    ])('rejects %s at once with an inline message and no edit', async (_n, conn, message) => {
      const kit = await openReady();
      act(() => canvas.props.onConnect({ ...conn, sourcePort: null }));
      expect(screen.getByRole('alert')).toHaveTextContent(message);
      expect(screen.getByTestId('canvas')).toHaveAttribute('data-edges', 'a>b,b>c');
      expect(screen.getByText('SAVED')).toBeInTheDocument();
      expect(kit.calls.some((c) => c.method === 'PUT')).toBe(false);
    });

    it('deleting an edge removes the blocker', async () => {
      await openReady(world(), {
        [`PUT ${PROPOSAL_URL}`]: (req) => ({ status: 200, json: proposalOut({ rev: 4, proposal: req.body }) }),
      });
      act(() => canvas.props.onEdgesDelete(['a>b']));
      expect(screen.getByTestId('canvas')).toHaveAttribute('data-edges', 'b>c');
    });

    it('the Blocked by checkboxes add and remove the same edges and refuse a cycle', async () => {
      await openReady(world(), {
        [`PUT ${PROPOSAL_URL}`]: (req) => ({ status: 200, json: proposalOut({ rev: 4, proposal: req.body }) }),
      });
      const user = userEvent.setup();
      await user.click(screen.getAllByTestId('node')[0]); // a
      await user.click(screen.getByRole('checkbox', { name: 'c' })); // a blocked by c: c <- b <- a
      expect(screen.getByRole('alert')).toHaveTextContent(/cycle/);
      await user.click(screen.getByRole('checkbox', { name: 'd' }));
      expect(screen.getByTestId('canvas')).toHaveAttribute('data-edges', 'd>a,a>b,b>c');
      await user.click(screen.getByRole('checkbox', { name: 'd' }));
      expect(screen.getByTestId('canvas')).toHaveAttribute('data-edges', 'a>b,b>c');
    });
  });

  it('adds and deletes issues with generated keys, and keeps at least one', async () => {
    await openReady(world(), {
      [`PUT ${PROPOSAL_URL}`]: (req) => ({ status: 200, json: proposalOut({ rev: 4, proposal: req.body }) }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Add issue' }));
    expect(screen.getAllByTestId('node').map((n) => n.textContent)).toEqual(['a', 'b', 'c', 'd', 'issue-5']);
    expect(screen.getByLabelText('Title')).toHaveValue('New issue');
    expect(screen.getByLabelText('Agent')).toHaveValue('claude'); // the first enabled agent
    await user.click(screen.getByRole('button', { name: 'Delete issue' }));
    expect(screen.getAllByTestId('node')).toHaveLength(4);

    act(() => canvas.props.onNodesDelete(['a', 'b', 'c', 'd']));
    expect(screen.getByRole('alert')).toHaveTextContent('A plan needs at least one issue.');
    expect(screen.getAllByTestId('node')).toHaveLength(4);
  });

  it('marks nodes that have validation errors and selects a node from the issue list', async () => {
    await open(
      world({
        proposal: proposalOut({
          status: 'invalid',
          issues_validation: [{ code: 'no_acceptance', key: 'c', message: 'there is no acceptance criterion' }],
        }),
      }),
    );
    const nodes = screen.getAllByTestId('node');
    expect(nodes.map((n) => n.dataset.invalid)).toEqual(['false', 'false', 'true', 'false']);
    await userEvent.setup().click(screen.getByText('there is no acceptance criterion'));
    expect(screen.getByLabelText('Selected issue')).toHaveValue('c');
  });

  it('Validate posts to the validate endpoint and shows the issues it returns', async () => {
    const kit = await openReady(world(), {
      [`POST ${PROPOSAL_URL}/validate`]: () => ({
        status: 200,
        json: { rev: 3, ok: false, issues: [{ code: 'unknown_playbook', key: 'b', message: 'playbook gone' }] },
      }),
    });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByText('playbook gone')).toBeInTheDocument();
    expect(kit.count('POST', `${PROPOSAL_URL}/validate`)).toBe(1);
    expect(approveButton()).toBeDisabled();
  });
});
