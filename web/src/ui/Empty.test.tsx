import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { Empty } from './Empty';

describe('Empty', () => {
  it('renders children with empty class', () => {
    render(<Empty>No data</Empty>);
    const el = screen.getByText('No data');
    expect(el).toHaveClass('empty');
  });
});
