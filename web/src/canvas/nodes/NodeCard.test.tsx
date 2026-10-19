import { render, screen } from '@testing-library/react';
import { describe, test, expect } from 'vitest';
import { NodeCard } from './NodeCard';
import { ReactFlowProvider } from '@xyflow/react';

describe('NodeCard', () => {
  test('renders id and label', () => {
    render(
      <ReactFlowProvider>
        <NodeCard
          id="test"
          data={{
            id: "test",
            kind: "agent",
            label: "Test Label",
          }}
          selected={false}
          type="customNode"
          zIndex={0}
          isConnectable={true}
          positionAbsoluteX={0}
          positionAbsoluteY={0}
          dragging={false}
        />
      </ReactFlowProvider>
    );

    expect(screen.getByText('test')).toBeInTheDocument();
    expect(screen.getByText('Test Label')).toBeInTheDocument();
  });
});
