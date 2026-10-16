import { ReactNode } from 'react';

export interface PanelProps {
  title?: ReactNode;
  children: ReactNode;
}

export function Panel({ title, children }: PanelProps) {
  return (
    <section className="panel">
      {title && <h2 className="panel-title">{title}</h2>}
      <div>{children}</div>
    </section>
  );
}
