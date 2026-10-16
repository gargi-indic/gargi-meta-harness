import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { DataTable } from './DataTable';

describe('DataTable', () => {
  it('renders rows and numeric cells', () => {
    const cols = [
      { key: 'name', header: 'Name' },
      { key: 'age', header: 'Age', numeric: true }
    ];
    const rows = [{ id: '1', name: 'Alice', age: 30 }];
    render(<DataTable columns={cols} rows={rows} rowKey={r => r.id} />);

    expect(screen.getByText('Name')).toBeInTheDocument();
    const ageHeader = screen.getByText('Age');
    expect(ageHeader).toHaveClass('num', 'mono');

    expect(screen.getByText('Alice')).toBeInTheDocument();
    const ageCell = screen.getByText('30');
    expect(ageCell).toHaveClass('num', 'mono');
  });

  it('renders empty state', () => {
    render(<DataTable columns={[]} rows={[]} rowKey={() => ''} />);
    expect(screen.getByText('No rows')).toBeInTheDocument();
  });
});
