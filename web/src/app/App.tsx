import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { Header } from './Header';
import { AppRoutes } from './routes';
import { createQueryClient, useInvalidateOnEvents } from '../api/queries';

const queryClient = createQueryClient();

function EventInvalidator() {
  useInvalidateOnEvents();
  return null;
}

function Layout() {
  return (
    <>
      <Header />
      <AppRoutes />
    </>
  );
}

// A data router (rather than BrowserRouter) is required for useBlocker in the playbook editor.
const router = createBrowserRouter([{ path: '*', element: <Layout /> }]);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <EventInvalidator />
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
