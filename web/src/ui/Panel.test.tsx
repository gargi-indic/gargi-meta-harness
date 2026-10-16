import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { Panel } from './Panel';

describe('Panel', () => {
  it('renders content and title', () => {
    render(<Panel title="My Panel">Content</Panel>);
    expect(screen.getByRole('heading', { name: 'My Panel' })).toBeInTheDocument();
    expect(screen.getByText('Content')).toBeInTheDocument();
  });

  it('renders without title', () => {
    render(<Panel>Content only</Panel>);
    expect(screen.queryByRole('heading')).not.toBeInTheDocument();
    expect(screen.getByText('Content only')).toBeInTheDocument();
  });
});
