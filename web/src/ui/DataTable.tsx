import { ReactNode } from 'react';
import { Empty } from './Empty';

export interface Column<T> {
  key: string;
  header: ReactNode;
  numeric?: boolean;
  render?: (row: T) => ReactNode;
}

export interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  empty?: ReactNode;
}

export function DataTable<T>({ columns, rows, rowKey, empty = <Empty>No rows</Empty> }: DataTableProps<T>) {
  if (rows.length === 0) {
    return <>{empty}</>;
  }

  return (
    <table>
      <thead>
        <tr>
          {columns.map(col => (
            <th key={col.key} className={col.numeric ? 'num mono' : ''}>{col.header}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map(row => (
          <tr key={rowKey(row)}>
            {columns.map(col => {
              const content = col.render ? col.render(row) : (row as any)[col.key];
              return (
                <td key={col.key} className={col.numeric ? 'num mono' : ''}>
                  {content}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
