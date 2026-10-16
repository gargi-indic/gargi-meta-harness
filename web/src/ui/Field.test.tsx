import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { Field } from './Field';

describe('Field', () => {
  it('renders label and input', () => {
    render(<Field label="Email" type="email" />);
    const input = screen.getByLabelText('Email');
    expect(input).toHaveAttribute('type', 'email');
    expect(input).not.toHaveAttribute('aria-invalid');
  });

  it('renders error state correctly', () => {
    render(<Field label="Name" error="Required" />);
    const input = screen.getByLabelText('Name');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent('Required');
    expect(input).toHaveAttribute('aria-describedby', screen.getByRole('alert').id);
  });
});
