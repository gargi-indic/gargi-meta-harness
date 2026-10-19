import { describe, test, expect } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from './routes';

describe('AppRoutes', () => {
  test('renders campaigns route', () => {
    // Basic rendering check
    const { container } = render(
      <MemoryRouter initialEntries={['/campaigns']}>
        <AppRoutes />
      </MemoryRouter>
    );
    expect(container).toBeDefined();
  });
});
