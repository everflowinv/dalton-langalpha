import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient } from '@tanstack/react-query';
import { Route, Routes } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import * as sharedImageCache from '@/lib/dalton/sharedImageCache';
import { isSharedFileBodyQueryKey } from '@/lib/dalton/shareFileGuard';
import type {
  getSharedMetadata as GetSharedMetadataFn,
  replaySharedThread as ReplaySharedThreadFn,
  getSharedFiles as GetSharedFilesFn,
  SharedThreadMetadata,
} from '../api';

const h = vi.hoisted(() => ({
  getSharedMetadata: vi.fn<typeof GetSharedMetadataFn>(),
  replaySharedThread: vi.fn<typeof ReplaySharedThreadFn>(),
  getSharedFiles: vi.fn<typeof GetSharedFilesFn>(),
}));

vi.mock('@/config/hostMode', () => ({
  HOST_MODE: 'platform',
  isPlatformMode: true,
  APP_ENTRY_PATH: '/app',
}));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ isInitialized: true, isLoggedIn: false, userId: null }),
}));

vi.mock('../../../contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}));

vi.mock('../../ChatAgent/hooks/usePTCDispatchStatus', () => ({
  DispatchStatusProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('../../ChatAgent/components/FilePanel', () => ({
  default: () => <div data-testid="file-panel" />,
}));

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    getSharedMetadata: (...args: Parameters<typeof GetSharedMetadataFn>) => h.getSharedMetadata(...args),
    replaySharedThread: (...args: Parameters<typeof ReplaySharedThreadFn>) => h.replaySharedThread(...args),
    getSharedFiles: (...args: Parameters<typeof GetSharedFilesFn>) => h.getSharedFiles(...args),
  };
});

import SharePage from '../SharePage';

const TOKEN = 'tok-page-revocation';

function makeThreadMetadata(allowFiles: boolean): SharedThreadMetadata {
  return {
    kind: 'thread',
    thread_id: 'thread-page-1',
    title: 'Shared Research',
    msg_type: 'chat',
    created_at: '2026-11-27T00:00:00Z',
    updated_at: '2026-11-27T00:00:00Z',
    workspace_name: 'Research',
    permissions: { allow_files: allowFiles, allow_download: allowFiles },
  };
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retryDelay: 0,
        gcTime: 5 * 60_000,
      },
    },
  });
}

function renderSharePage(queryClient: QueryClient, token = TOKEN) {
  return renderWithProviders(
    <Routes>
      <Route path="/s/:shareToken" element={<SharePage />} />
    </Routes>,
    { route: `/s/${token}`, queryClient },
  );
}

describe('SharePage revocation unmount and initial revalidation gating', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.getSharedFiles.mockResolvedValue({ path: '.', files: [], source: 'live' });
    h.replaySharedThread.mockImplementation(async (_token, onEvent) => {
      onEvent?.({ event: 'user_message', turn_index: 0, content: 'Sensitive prompt' });
      onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Sensitive replay body' });
      onEvent?.({ event: 'replay_done', thread_id: 'thread-page-1' });
    });
  });

  afterEach(() => {
    sharedImageCache.revokeSharedImageScope(TOKEN);
  });

  it('unmounts SharedChatView and revokes caches when metadata revalidation returns 404', async () => {
    const revokeSpy = vi.spyOn(sharedImageCache, 'revokeSharedImageScope');
    const queryClient = makeQueryClient();
    const adapterKey = queryKeys.workspaceFiles.body('adapter:mount-404', 'secret.md', 'text');
    queryClient.setQueryData(adapterKey, { text: 'cached file body' });

    h.getSharedMetadata.mockResolvedValueOnce(makeThreadMetadata(true));
    renderSharePage(queryClient);

    expect(await screen.findByText('Sensitive replay body')).toBeInTheDocument();

    h.getSharedMetadata.mockRejectedValueOnce(
      Object.assign(new Error('Shared link not found (404)'), { response: { status: 404 } }),
    );
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.share.all });
    });

    expect(await screen.findByText("You don't have access, or this link is unavailable")).toBeInTheDocument();
    expect(screen.queryByText('Sensitive replay body')).not.toBeInTheDocument();
    expect(revokeSpy).toHaveBeenCalledWith(TOKEN);
    expect(
      queryClient.getQueryCache().findAll({ predicate: (q) => isSharedFileBodyQueryKey(q.queryKey) }),
    ).toHaveLength(0);

    revokeSpy.mockRestore();
  });

  it('unmounts SharedChatView and revokes caches when metadata revalidation returns 401 members_only', async () => {
    const revokeSpy = vi.spyOn(sharedImageCache, 'revokeSharedImageScope');
    const queryClient = makeQueryClient();

    h.getSharedMetadata.mockResolvedValueOnce(makeThreadMetadata(true));
    renderSharePage(queryClient);

    expect(await screen.findByText('Sensitive replay body')).toBeInTheDocument();

    h.getSharedMetadata.mockRejectedValueOnce(
      Object.assign(new Error('Members only (401)'), {
        response: { status: 401, data: { detail: { type: 'members_only' } } },
      }),
    );
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.share.all });
    });

    expect(await screen.findByText(/members|成员/i)).toBeInTheDocument();
    expect(screen.queryByText('Sensitive replay body')).not.toBeInTheDocument();
    expect(revokeSpy).toHaveBeenCalledWith(TOKEN);

    revokeSpy.mockRestore();
  });

  it('shows failed page with Retry and never paints stale cached allow_files=true when initial revalidation fails with 500, while keeping view on failure after an initial success', async () => {
    const queryClient = makeQueryClient();
    const metaKey = queryKeys.share.metadata(TOKEN, null, false, null);
    queryClient.setQueryData(metaKey, makeThreadMetadata(true), { updatedAt: Date.now() - 10_000 });

    const serverError = Object.assign(new Error('Internal Server Error'), { response: { status: 500 } });
    // Initial revalidation fails across all 3 attempts (initial + 2 retries)
    h.getSharedMetadata.mockRejectedValue(serverError);

    renderSharePage(queryClient);

    // Must land on failed page with Retry and never mount SharedChatView or trigger replay
    expect(await screen.findByText("Couldn't load this link")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.queryByText('Sensitive replay body')).not.toBeInTheDocument();
    expect(h.replaySharedThread).not.toHaveBeenCalled();

    // Retry succeeds -> mounts SharedChatView
    h.getSharedMetadata.mockResolvedValueOnce(makeThreadMetadata(true));
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Sensitive replay body')).toBeInTheDocument();

    // Subsequent 500 failure after a successful mount revalidation keeps the last good view
    h.getSharedMetadata.mockRejectedValue(serverError);
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.share.all });
    });
    await waitFor(() => {
      expect(screen.getByText('Sensitive replay body')).toBeInTheDocument();
    });
    expect(screen.queryByText("Couldn't load this link")).not.toBeInTheDocument();
  });
});
