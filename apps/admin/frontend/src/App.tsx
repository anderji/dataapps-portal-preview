/**
 * Root: theme, query client, toaster, router. The session gate sits above the router's pages so
 * no page renders (or fetches) before the gate has admitted the principal.
 */
import { useState } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ThemeProvider, Toaster } from '@keboola/design';
import { keboola } from '@keboola/brand-registry';
import { ApiError } from './api/client';
import { config } from './config';
import { SessionGate } from './components/SessionGate';
import { Shell } from './components/Shell';
import { OverviewPage } from './pages/OverviewPage';
import { AppsPage } from './pages/AppsPage';
import { DiscoveryPage } from './pages/DiscoveryPage';
import { UsersPage } from './pages/UsersPage';
import { UserEditPage } from './pages/UserEditPage';
import { GroupsPage } from './pages/GroupsPage';
import { GroupEditPage } from './pages/GroupEditPage';
import { LocalGroupsPage } from './pages/LocalGroupsPage';
import { LocalGroupEditPage } from './pages/LocalGroupEditPage';
import { DatasetsPage } from './pages/DatasetsPage';
import { DatasetEditPage } from './pages/DatasetEditPage';
import { GrantsPage } from './pages/GrantsPage';
import { AuditPage } from './pages/AuditPage';
import { NotFoundPage } from './pages/NotFoundPage';

/** 4xx answers are decisions, not glitches: retrying them only delays the message. */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < config.query.retry;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { staleTime: config.query.staleTimeMs, retry: shouldRetry, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

export function AppRoutes() {
  return (
    <SessionGate>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<OverviewPage />} />
          <Route path="apps" element={<AppsPage />} />
          <Route path="discovery" element={<DiscoveryPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="users/:email" element={<UserEditPage />} />
          <Route path="groups" element={<GroupsPage />} />
          <Route path="groups/:groupId" element={<GroupEditPage />} />
          <Route path="local-groups" element={<LocalGroupsPage />} />
          <Route path="local-groups/:localGroupId" element={<LocalGroupEditPage />} />
          <Route path="datasets" element={<DatasetsPage />} />
          <Route path="datasets/:name" element={<DatasetEditPage />} />
          <Route path="grants" element={<GrantsPage />} />
          <Route path="audit" element={<AuditPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </SessionGate>
  );
}

export function App() {
  const [queryClient] = useState(createQueryClient);
  return (
    <ThemeProvider brands={[keboola]} defaultBrandId={keboola.id} defaultScheme="system">
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
