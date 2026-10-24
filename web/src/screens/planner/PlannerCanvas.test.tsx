// The real FlowCanvas (not a double): the proposal's issues become node cards and invalid ones get the dashed border.
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { cleanup, waitFor } from '@testing-library/react';
import {
  DECOMPOSE_JOB_URL,
  OPTIONS_URL,
  PID,
  PLAN_URL,
  PROPOSAL_URL,
  installEventSource,
  jobOut,
  optionsOut,
  planOut,
  plannerFetch,
  proposalOut,
  renderPlanner,
} from './testkit';

describe('PlannerPage canvas', () => {
  beforeEach(installEventSource);
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('renders one node per issue with key, title and agent, and flags the ones with errors', async () => {
    plannerFetch({
      [`GET ${PROPOSAL_URL}`]: () => ({
        status: 200,
        json: proposalOut({
          status: 'invalid',
          issues_validation: [{ code: 'no_acceptance', key: 'b', message: 'no criterion' }],
        }),
      }),
      [`GET ${PLAN_URL}`]: () => ({ status: 200, json: planOut() }),
      [`GET ${OPTIONS_URL}`]: () => ({ status: 200, json: optionsOut }),
      [`GET ${DECOMPOSE_JOB_URL}`]: () => ({
        status: 200,
        json: jobOut({ job_id: `${PID}.decompose`, kind: 'decompose', status: 'done' }),
      }),
    });
    const { container } = renderPlanner(`/planner?proposal=${encodeURIComponent(PID)}`);

    await waitFor(() => expect(container.querySelectorAll('.planner-node')).toHaveLength(4));
    const cards = Array.from(container.querySelectorAll('.planner-node'));
    expect(cards.map((c) => c.querySelector('.node-card-id')?.textContent)).toEqual(['a', 'b', 'c', 'd']);
    expect(cards[0].textContent).toContain('Do a');
    expect(cards[0].querySelector('.node-card-badge')?.textContent).toBe('jules');
    expect(cards.map((c) => c.classList.contains('node-card--waiting'))).toEqual([false, true, false, false]);
    expect(cards[1].getAttribute('data-invalid')).toBe('true');
  });
});
