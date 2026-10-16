import { Routes, Route, Navigate } from 'react-router-dom';
import { Page } from './Page';

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/campaigns" replace />} />
      <Route path="/campaigns" element={<Page title="Campaigns" />} />
      <Route path="/board" element={<Page title="Board" />} />
      <Route path="/inbox" element={<Page title="Inbox" />} />
      <Route path="/runs" element={<Page title="Runs" />} />
      <Route path="/playbooks" element={<Page title="Playbooks" />} />
      <Route path="*" element={<Page title="Not found" />} />
    </Routes>
  );
}
