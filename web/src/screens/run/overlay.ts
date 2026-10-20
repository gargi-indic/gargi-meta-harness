import { Playbook } from '../../generated/playbook';
import { StepOut } from '../../api/types';
import { NodeStatus } from '../../canvas/types';

export function computeOverlay(
  steps: StepOut[],
  playbook: Playbook | null
): Record<string, { status: NodeStatus; visits: number; badge?: string }> {
  const overlay: Record<string, { status: NodeStatus; visits: number; badge?: string }> = {};

  if (playbook) {
    for (const id of Object.keys(playbook.nodes)) {
      overlay[id] = { status: 'pending', visits: 0 };
    }
  }

  const latestSteps: Record<string, StepOut> = {};
  const visitCounts: Record<string, number> = {};

  for (const step of steps) {
    if (!latestSteps[step.node] || step.visit >= latestSteps[step.node].visit) {
      if (
        !latestSteps[step.node] ||
        step.visit > latestSteps[step.node].visit ||
        (step.visit === latestSteps[step.node].visit &&
          (step.finished_at ? 1 : 0) >= (latestSteps[step.node].finished_at ? 1 : 0))
      ) {
        latestSteps[step.node] = step;
      }
    }
    visitCounts[step.node] = Math.max(visitCounts[step.node] || 0, step.visit);
  }

  for (const [node, step] of Object.entries(latestSteps)) {
    let status: NodeStatus = 'pending';

    if (step.status === 'running') status = 'running';
    else if (step.status === 'waiting') {
      const isGate = playbook?.nodes[node]?.kind === 'gate';
      status = isGate ? 'needs-you' : 'waiting';
    }
    else if (step.status === 'done') status = 'done';
    else if (step.status === 'failed') status = 'failed';
    else if (step.status === 'skipped') status = 'skipped';
    else if (step.status === 'pending') status = 'pending';

    const visits = visitCounts[node];
    let badge: string | undefined = undefined;

    if (step.outcome === 'exhausted') {
      badge = 'EXHAUSTED';
    } else if (visits > 1) {
      badge = `x${visits}`;
    }

    overlay[node] = { status, visits, badge };
  }

  return overlay;
}
