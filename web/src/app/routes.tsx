import { Routes, Route, Navigate } from 'react-router-dom';
import React, { Suspense } from 'react';
import { Page } from './Page';

// Lazy load DevCanvasPage so it can be tree-shaken in prod
const DevCanvasPage = import.meta.env.DEV ? React.lazy(() => import('../canvas/DevCanvasPage')) : null;

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/campaigns" replace />} />
      <Route path="/campaigns" element={<Page title="Campaigns" />} />
      <Route path="/board" element={<Page title="Board" />} />
      <Route path="/inbox" element={<Page title="Inbox" />} />
      <Route path="/runs" element={<Page title="Runs" />} />
      <Route path="/playbooks" element={<Page title="Playbooks" />} />

      {import.meta.env.DEV && DevCanvasPage && (
        <Route path="/dev/canvas" element={
          <Suspense fallback={<div>Loading canvas...</div>}>
            <DevCanvasPage />
          </Suspense>
        } />
      )}

      <Route path="*" element={<Page title="Not found" />} />
    </Routes>
  );
}
