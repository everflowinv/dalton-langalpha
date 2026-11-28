/**
 * DALTON-HOOK(team-sharing): verifies useChartComments with the real useTeamReadOnly
 * hook when the workspaces.detail cache starts empty (e.g., mobile MarketView on Home
 * or a workspace whose detail query has not been cached yet).
 */
import { waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createTestQueryClient, renderHookWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import type { ChartCommentsHost } from '@/pages/ChatAgent/components/viewers/comments/chart/ChartComments';
import { useChartComments } from '../useChartComments';

const mockApiGet = vi.hoisted(() => vi.fn());

vi.mock('@/config/daltonAuth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/config/daltonAuth')>();
  return {
    ...actual,
    isDaltonAuth: true,
    DALTON_AUTH_MODE: 'local',
  };
});

vi.mock('@/api/client', () => ({
  api: {
    defaults: { baseURL: 'http://localhost:8000' },
    get: (...args: unknown[]) => mockApiGet(...args),
  },
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({
      ...actual.useCapabilities(),
      artifactCommentsEnabled: true,
      hasCapability: () => false,
    }),
  };
});

vi.mock('@/hooks/useCallerIdentity', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCallerIdentity')>();
  return {
    ...actual,
    useCallerIdentity: () => ({
      ...actual.useCallerIdentity(),
      tier: 'internal',
      status: 'ready',
      isPlatform: false,
      isMultiUserAuth: true,
      isInternal: true,
      isAdmin: false,
      isExternal: false,
      canActAsOwner: true,
      isConfirming: false,
    }),
  };
});

vi.mock('@/pages/ChatAgent/components/viewers/html/comments/CommentTools', () => ({
  CommentTools: () => null,
}));

const setMode = vi.fn();
const host = {
  ui: { modeTab: 'chart-tab', setMode },
  tabId: 'chart-tab',
  active: true,
} as unknown as ChartCommentsHost;

describe('useChartComments with real useTeamReadOnly on empty detail cache', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    setMode.mockClear();
  });

  it('shows chart comment entry immediately for an owner when the loaded Home workspace record is passed and detail cache is empty', () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.user.me(), {
      user_id: 'u-owner-1',
      email: 'owner@dalton.test',
      name: 'Owner',
    });

    const { result } = renderHookWithProviders(
      () =>
        useChartComments(host, true, {
          workspaceId: 'ws-home-1',
          chartId: 'NVDA:1day',
          workspace: { user_id: 'u-owner-1', name: 'Home' },
        }),
      { queryClient },
    );

    expect(queryClient.getQueryData(queryKeys.workspaces.detail('ws-home-1'))).toBeUndefined();
    expect(result.current.tools).toBeTruthy();
    expect(result.current.mode).toBe(true);
    expect(mockApiGet).not.toHaveBeenCalledWith('/api/v1/workspaces/ws-home-1');
  });

  it('subscribes to workspace detail on empty cache and unlocks chart comments once owner record loads', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.user.me(), {
      user_id: 'u-owner-1',
      email: 'owner@dalton.test',
      name: 'Owner',
    });

    let resolveDetail!: (value: { data: Record<string, unknown> }) => void;
    mockApiGet.mockImplementation((url: string) => {
      if (url === '/api/v1/workspaces/ws-other-1') {
        return new Promise((resolve) => {
          resolveDetail = resolve;
        });
      }
      return Promise.resolve({ data: {} });
    });

    const { result } = renderHookWithProviders(
      () =>
        useChartComments(host, true, {
          workspaceId: 'ws-other-1',
          chartId: 'AAPL:1day',
        }),
      { queryClient },
    );

    // Fail-closed while the workspace detail fetch is still pending.
    expect(result.current.tools).toBeFalsy();
    expect(result.current.mode).toBe(false);

    resolveDetail({
      data: {
        workspace_id: 'ws-other-1',
        user_id: 'u-owner-1',
        name: 'Research',
        is_home: false,
      },
    });

    await waitFor(() => {
      expect(result.current.tools).toBeTruthy();
    });
    expect(result.current.mode).toBe(true);
    expect(mockApiGet).toHaveBeenCalledWith('/api/v1/workspaces/ws-other-1');
  });

  it('keeps chart comments hidden after workspace detail loads a colleague workspace', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.user.me(), {
      user_id: 'u-owner-1',
      email: 'owner@dalton.test',
      name: 'Owner',
    });
    mockApiGet.mockImplementation((url: string) => {
      if (url === '/api/v1/workspaces/ws-colleague-1') {
        return Promise.resolve({
          data: {
            workspace_id: 'ws-colleague-1',
            user_id: 'u-colleague-2',
            name: 'Colleague WS',
            is_home: false,
          },
        });
      }
      return Promise.resolve({ data: {} });
    });

    const { result } = renderHookWithProviders(
      () =>
        useChartComments(host, true, {
          workspaceId: 'ws-colleague-1',
          chartId: 'AAPL:1day',
        }),
      { queryClient },
    );

    await waitFor(() => {
      expect(queryClient.getQueryData(queryKeys.workspaces.detail('ws-colleague-1'))).toBeTruthy();
    });
    expect(result.current.tools).toBeFalsy();
    expect(result.current.mode).toBe(false);
  });
});
