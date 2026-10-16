import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from './routes';
import { App } from './App';

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

describe('App', () => {
  let esConstructorMock: any;
  beforeEach(() => {
    esConstructorMock = vi.fn(function(this: any) {
      this.addEventListener = vi.fn();
      this.removeEventListener = vi.fn();
      this.close = vi.fn();
    });
    vi.stubGlobal('EventSource', esConstructorMock);
  });

  it('mounts EventInvalidator which creates exactly one EventSource for /api/events', () => {
    render(<App />);
    expect(esConstructorMock).toHaveBeenCalledTimes(1);
    expect(esConstructorMock).toHaveBeenCalledWith('/api/events');
  });
});
