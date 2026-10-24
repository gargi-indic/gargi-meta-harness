import { describe, it, expect, afterEach } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { AcceptanceList } from './AcceptanceList';

function Harness({ initial }: { initial: string[] }) {
  const [items, setItems] = useState(initial);
  return (
    <>
      <AcceptanceList items={items} onChange={setItems} />
      <output data-testid="items">{JSON.stringify(items)}</output>
    </>
  );
}

const items = () => JSON.parse(screen.getByTestId('items').textContent ?? '[]') as string[];

describe('AcceptanceList (keyboard only)', () => {
  afterEach(cleanup);

  it('reorders, removes and adds criteria with Tab, Enter and Space', async () => {
    render(<Harness initial={['one', 'two', 'three']} />);
    const user = userEvent.setup();

    // Tab: criterion 1 text, Up (disabled, skipped), Down
    await user.tab();
    expect(screen.getByLabelText('Criterion 1')).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Move criterion 1 down' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(items()).toEqual(['two', 'one', 'three']);
    // focus follows the moved item, so the same key moves it again
    expect(screen.getByRole('button', { name: 'Move criterion 2 down' })).toHaveFocus();
    await user.keyboard(' ');
    expect(items()).toEqual(['two', 'three', 'one']);

    // the item is last now: its Down button is disabled, focus falls back to its text field
    expect(screen.getByLabelText('Criterion 3')).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Move criterion 3 up' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(items()).toEqual(['two', 'one', 'three']);

    // remove criterion 2 from the keyboard
    screen.getByRole('button', { name: 'Remove criterion 2' }).focus();
    await user.keyboard('{Enter}');
    expect(items()).toEqual(['two', 'three']);

    // add one and type into it
    screen.getByRole('button', { name: 'Add criterion' }).focus();
    await user.keyboard('{Enter}');
    expect(items()).toEqual(['two', 'three', '']);
    expect(screen.getByLabelText('Criterion 3')).toHaveFocus();
    await user.keyboard('four');
    expect(items()).toEqual(['two', 'three', 'four']);
  });

  it('disables Up on the first and Down on the last criterion', () => {
    render(<Harness initial={['one', 'two']} />);
    expect(screen.getByRole('button', { name: 'Move criterion 1 up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move criterion 2 down' })).toBeDisabled();
  });
});
