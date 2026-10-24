import { StatusTag, type Tone } from '../../ui/StatusTag';
import type { PlanOut } from '../../api/planner';

interface Block {
  level: number;
  heading: string | null;
  text: string;
}

const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const FENCE = /^\s*(```+|~~~+)/;

/** Split markdown into one block per heading (headings inside code fences do not count). */
export function splitPlan(markdown: string): Block[] {
  const blocks: Block[] = [{ level: 0, heading: null, text: '' }];
  let fence: string | null = null;
  const lines: string[][] = [[]];
  for (const line of markdown.split('\n')) {
    const marker = FENCE.exec(line);
    if (marker) fence = fence === marker[1][0] ? null : (fence ?? marker[1][0]);
    const heading = fence === null && !marker ? HEADING.exec(line) : null;
    if (heading) {
      blocks.push({ level: heading[1].length, heading: heading[2], text: '' });
      lines.push([]);
    } else {
      lines[lines.length - 1].push(line);
    }
  }
  return blocks
    .map((b, i) => ({ ...b, text: lines[i].join('\n').replace(/^\n+|\s+$/g, '') }))
    .filter((b) => b.heading !== null || b.text !== '');
}

const APPROVAL_TONE: Record<string, Tone> = {
  approved: 'ok',
  pending: 'warn',
  stale: 'fail',
  rejected: 'fail',
};

export interface PlanPreviewProps {
  plan: PlanOut | undefined;
  loading: boolean;
  error: string | null;
}

/**
 * The plan as text. There is no markdown renderer in the web bundle, so each heading is shown as a heading and
 * the text under it as pre-formatted text. Everything is a React text node: HTML in the plan is never parsed.
 */
export function PlanPreview({ plan, loading, error }: PlanPreviewProps) {
  if (error) {
    return (
      <p role="alert" className="field-error">
        {error}
      </p>
    );
  }
  if (loading) return <p className="empty mono">Loading plan...</p>;
  if (!plan) return <p className="empty">Load a plan to preview it.</p>;

  const approval = plan.approval;
  const blocks = splitPlan(plan.markdown);
  return (
    <div className="plan-preview">
      <div className="plan-preview-tags">
        <StatusTag tone="muted">{`SHA ${plan.sha.slice(0, 12)}`}</StatusTag>
        {approval ? (
          <StatusTag tone={APPROVAL_TONE[approval.status] ?? 'muted'}>{`PLAN ${approval.status.toUpperCase()}`}</StatusTag>
        ) : (
          <StatusTag tone="muted">NO CONTROL ISSUE</StatusTag>
        )}
      </div>
      {approval?.note && <p className="mono plan-preview-note">{approval.note}</p>}
      <div className="plan-reading" aria-label="Plan text">
        {blocks.map((b, i) => (
          <section key={i} className="plan-block">
            {b.heading !== null && (
              <h3 className={`plan-heading plan-heading--${Math.min(b.level, 3)}`}>{b.heading}</h3>
            )}
            {b.text && <pre className="plan-text">{b.text}</pre>}
          </section>
        ))}
      </div>
    </div>
  );
}
