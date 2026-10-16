import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from './routes';

describe('AppRoutes', () => {
  it('redirects / to campaigns', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <AppRoutes />
      </MemoryRouter>
    );
    expect(screen.getByRole('heading', { name: 'Campaigns' })).toBeInTheDocument();
  });

  it('renders known route', () => {
    render(
      <MemoryRouter initialEntries={['/playbooks']}>
        <AppRoutes />
      </MemoryRouter>
    );
    expect(screen.getByRole('heading', { name: 'Playbooks' })).toBeInTheDocument();
  });

  it('renders not found for unknown route', () => {
    render(
      <MemoryRouter initialEntries={['/something-else']}>
        <AppRoutes />
      </MemoryRouter>
    );
    expect(screen.getByRole('heading', { name: 'Not found' })).toBeInTheDocument();
  });
});
