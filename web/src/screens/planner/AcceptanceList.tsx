import { useEffect, useRef } from 'react';
import { Button } from '../../ui/Button';

export interface AcceptanceListProps {
  items: string[];
  onChange: (items: string[]) => void;
}

type Slot = { index: number; part: 'text' | 'up' | 'down' | 'remove' };

/** The acceptance criteria of one issue: add, remove and reorder, all with ordinary buttons. */
export function AcceptanceList({ items, onChange }: AcceptanceListProps) {
  const root = useRef<HTMLFieldSetElement>(null);
  const focus = useRef<Slot | null>(null);

  useEffect(() => {
    const slot = focus.current;
    if (!slot) return;
    focus.current = null;
    const at = (part: string) => root.current?.querySelector<HTMLButtonElement | HTMLInputElement>(`[data-slot="${slot.index}-${part}"]`);
    // A moved item that reached an end has a disabled button there: fall back to its text field.
    const target = at(slot.part);
    (target && !target.disabled ? target : at('text'))?.focus();
  });

  const move = (from: number, to: number, part: Slot['part']) => {
    if (to < 0 || to >= items.length) return;
    const next = [...items];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    focus.current = { index: to, part };
    onChange(next);
  };

  const remove = (index: number) => {
    focus.current = items.length > 1 ? { index: Math.min(index, items.length - 2), part: 'text' } : null;
    onChange(items.filter((_, i) => i !== index));
  };

  const add = () => {
    focus.current = { index: items.length, part: 'text' };
    onChange([...items, '']);
  };

  return (
    <fieldset className="planner-fieldset" ref={root}>
      <legend className="field-label">Acceptance criteria</legend>
      {items.length === 0 && <p className="empty mono">No criteria.</p>}
      <ol className="ac-list">
        {items.map((item, i) => (
          <li key={i} className="ac-row">
            <div className="ac-line">
              <span className="mono ac-num">{i + 1}</span>
              <input
                className="field-input ac-input"
                aria-label={`Criterion ${i + 1}`}
                data-slot={`${i}-text`}
                value={item}
                onChange={(e) => onChange(items.map((v, j) => (j === i ? e.target.value : v)))}
              />
            </div>
            <div className="ac-buttons">
              <Button
                aria-label={`Move criterion ${i + 1} up`}
                data-slot={`${i}-up`}
                disabled={i === 0}
                onClick={() => move(i, i - 1, 'up')}
              >
                Up
              </Button>
              <Button
                aria-label={`Move criterion ${i + 1} down`}
                data-slot={`${i}-down`}
                disabled={i === items.length - 1}
                onClick={() => move(i, i + 1, 'down')}
              >
                Down
              </Button>
              <Button aria-label={`Remove criterion ${i + 1}`} data-slot={`${i}-remove`} onClick={() => remove(i)}>
                Remove
              </Button>
            </div>
          </li>
        ))}
      </ol>
      <Button onClick={add}>Add criterion</Button>
    </fieldset>
  );
}
