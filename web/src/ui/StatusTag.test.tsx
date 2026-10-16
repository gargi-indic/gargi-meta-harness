import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { StatusTag, TONE_COLOR } from './StatusTag';

describe('StatusTag', () => {
  it('renders the text', () => {
    render(<StatusTag tone="ok">Ready</StatusTag>);
    const el = screen.getByText('Ready');
    expect(el).toBeInTheDocument();
    expect(el).toHaveClass('tag', 'tag--ok');
  });

  it('has valid tone mapping', () => {
    expect(TONE_COLOR.live).toBe('--accent');
    expect(TONE_COLOR.fail).toBe('--accent');
    expect(TONE_COLOR.ok).toBe('--ink');
    expect(TONE_COLOR.warn).toBe('--muted');
    expect(TONE_COLOR.muted).toBe('--muted');
  });
});
