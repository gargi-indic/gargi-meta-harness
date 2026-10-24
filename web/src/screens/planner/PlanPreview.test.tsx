import { describe, it, expect, afterEach } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { PlanPreview, splitPlan } from './PlanPreview';
import { planOut } from './testkit';

describe('PlanPreview', () => {
  afterEach(cleanup);

  it('does not render raw HTML from the plan', () => {
    const markdown = [
      '# Plan',
      '',
      'Intro <script>window.__pwned = true</script> text',
      '',
      '## <img src=x onerror="window.__pwned = true">Steps',
      '',
      '<iframe src="https://example.invalid"></iframe>',
    ].join('\n');
    const { container } = render(<PlanPreview plan={planOut({ markdown })} loading={false} error={null} />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
    // the markup is shown as text instead
    expect(screen.getByText(/<script>window.__pwned = true<\/script>/)).toBeInTheDocument();
  });

  it('splits the text at headings and ignores # lines inside code fences', () => {
    const blocks = splitPlan('intro\n\n# A\n\ntext\n\n```\n# not a heading\n```\n\n## B\nmore');
    expect(blocks.map((b) => b.heading)).toEqual([null, 'A', 'B']);
    expect(blocks[1].text).toContain('# not a heading');
  });

  it('shows the sha and approval status as tags, and says when there is no control issue', () => {
    const { rerender } = render(<PlanPreview plan={planOut()} loading={false} error={null} />);
    expect(screen.getByText('SHA sha111111111')).toBeInTheDocument();
    expect(screen.getByText('PLAN APPROVED')).toBeInTheDocument();
    rerender(<PlanPreview plan={planOut({ approval: null })} loading={false} error={null} />);
    expect(screen.getByText('NO CONTROL ISSUE')).toBeInTheDocument();
  });

  it('shows an error and an empty state', () => {
    const { rerender } = render(<PlanPreview plan={undefined} loading={false} error="plan file not found" />);
    expect(screen.getByRole('alert')).toHaveTextContent('plan file not found');
    rerender(<PlanPreview plan={undefined} loading={false} error={null} />);
    expect(screen.getByText('Load a plan to preview it.')).toBeInTheDocument();
  });
});
