import { BrowserRouter } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { Header } from './Header';
import { AppRoutes } from './routes';
import { createQueryClient } from '../api/queries';

const queryClient = createQueryClient();

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Header />
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
