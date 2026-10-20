import { Routes, Route, Navigate } from 'react-router-dom';
import React, { Suspense } from 'react';
import { Page } from './Page';
import { CampaignsPage } from '../screens/campaigns/CampaignsPage';
import { BoardPage } from '../screens/board/BoardPage';
import { InboxPage } from '../screens/inbox/InboxPage';

// Lazy load DevCanvasPage so it can be tree-shaken in prod
const DevCanvasPage = import.meta.env.DEV ? React.lazy(() => import('../canvas/DevCanvasPage')) : null;

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/campaigns" replace />} />
      <Route path="/campaigns" element={<CampaignsPage />} />
      <Route path="/campaigns/:owner/:repo" element={<CampaignsPage />} />
      <Route path="/board" element={<BoardPage />} />
      <Route path="/inbox" element={<InboxPage />} />
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
