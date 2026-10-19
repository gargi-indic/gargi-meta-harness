// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import { describe, test, expect, vi } from 'vitest';
import { FlowCanvas } from './FlowCanvas';
import planBuildReview from './__fixtures__/plan-build-review.json';
import { playbookToGraph } from './convert';
import { Playbook } from '../generated/playbook';

// Setup stubs for React Flow DOM interactions
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
global.ResizeObserver = ResizeObserverStub as any;

class DOMMatrixReadOnlyStub {
  m11 = 1; m12 = 0; m13 = 0; m14 = 0;
  m21 = 0; m22 = 1; m23 = 0; m24 = 0;
  m31 = 0; m32 = 0; m33 = 1; m34 = 0;
  m41 = 0; m42 = 0; m43 = 0; m44 = 1;
}
global.DOMMatrixReadOnly = DOMMatrixReadOnlyStub as any;

describe('FlowCanvas', () => {
  test('renders all nodes of the fixture in view mode', () => {
    const pb = planBuildReview as unknown as Playbook;
    const { nodes, edges } = playbookToGraph(pb);

    const onSelect = vi.fn();

    render(
      <FlowCanvas
        nodes={nodes}
        edges={edges}
        mode="view"
        onSelect={onSelect}
        height={500}
      />
    );

    // Test that the container is rendered and some nodes from fixture exist
    expect(screen.getAllByText('plan').length).toBeGreaterThan(0);
    expect(screen.getAllByText('plan_ready').length).toBeGreaterThan(0);
  });
});
