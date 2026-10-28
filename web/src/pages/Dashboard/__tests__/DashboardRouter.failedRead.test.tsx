import { Suspense } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';

// The real usePreferences, so the observer's view of a failed read is the one
// the router sees in the app.
const getPreferences = vi.fn(() => Promise.reject(new Error('503')));

vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => false }));
vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock('../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/api')>()),
  getPreferences: () => getPreferences(),
}));
vi.mock('@/hooks/usePreferences', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/usePreferences')>();
  return actual;
});
vi.mock('../Dashboard', () => ({ __esModule: true, default: () => <div data-testid="classic-dashboard" /> }));
vi.mock('../DashboardCustom', () => ({ __esModule: true, default: () => <div data-testid="custom-dashboard" /> }));

import DashboardRouter from '../DashboardRouter';

describe('DashboardRouter with a failing preferences read', () => {
  it('renders the page instead of waiting on read after read', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { getByTestId } = await act(async () => render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <Suspense fallback={<div data-testid="route-loading" />}>
            <DashboardRouter />
          </Suspense>
        </MemoryRouter>
      </QueryClientProvider>,
    ));
    // Long enough for a loop to show itself: each lap is one failed read.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 300));
    });
    expect(getByTestId('classic-dashboard')).toBeInTheDocument();
    // The suspended read, then the page's own read on mount, and no more.
    expect(getPreferences.mock.calls.length).toBeLessThanOrEqual(2);
  });
});
