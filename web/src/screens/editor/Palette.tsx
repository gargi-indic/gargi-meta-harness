import { Button } from '../../ui/Button';

export interface PaletteProps {
  onAddNode: (kind: string) => void;
}

const nodeKinds = [
  'agent',
  'review',
  'check',
  'gate',
  'branch',
  'parallel',
  'join',
  'decompose',
  'merge',
  'escalate',
  'notify',
];

export function Palette({ onAddNode }: PaletteProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {nodeKinds.map((kind) => (
        <Button
          key={kind}
          onClick={() => onAddNode(kind)}
          style={{
            width: '100%',
            justifyContent: 'center',
            height: 48,
            fontFamily: '"JetBrains Mono", monospace',
            textTransform: 'uppercase',
            fontSize: 12
          }}
        >
          {kind}
        </Button>
      ))}
    </div>
  );
}
