import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { Header } from './Header';

describe('Header', () => {
  it('toggles menu and active links', () => {
    render(
      <MemoryRouter initialEntries={['/inbox']}>
        <Header />
        <Routes>
          <Route path="/*" element={<div />} />
        </Routes>
      </MemoryRouter>
    );

    const menuBtn = screen.getByRole('button', { name: 'Menu' });
    const nav = screen.getByRole('navigation', { name: 'Main' });

    expect(menuBtn).toHaveAttribute('aria-expanded', 'false');
    expect(nav).toHaveAttribute('data-open', 'false');

    fireEvent.click(menuBtn);
    expect(menuBtn).toHaveAttribute('aria-expanded', 'true');
    expect(nav).toHaveAttribute('data-open', 'true');

    // Active link has aria-current="page"
    const inboxLink = screen.getByText('Inbox');
    expect(inboxLink).toHaveAttribute('aria-current', 'page');

    // Click a link to close the menu
    const boardLink = screen.getByText('Board');
    fireEvent.click(boardLink);
    expect(menuBtn).toHaveAttribute('aria-expanded', 'false');
    expect(nav).toHaveAttribute('data-open', 'false');
  });
});
