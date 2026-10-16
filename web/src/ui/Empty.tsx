import { ReactNode } from 'react';

export interface EmptyProps {
  children: ReactNode;
}

export function Empty({ children }: EmptyProps) {
  return <div className="empty">{children}</div>;
}
