import { ReactNode } from 'react';

export function Page({ title, children }: { title: string, children?: ReactNode }) {
  return (
    <div className="container">
      <h1>{title}</h1>
      {children || <p className="empty mt-16">Not built yet.</p>}
    </div>
  );
}
