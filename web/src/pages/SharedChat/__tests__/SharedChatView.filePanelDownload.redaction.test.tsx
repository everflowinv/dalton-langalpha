import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { renderWithProviders } from '@/test/utils';
import type { toast as ToastFn } from '@/components/ui/use-toast';
import type {
  downloadSharedFile as DownloadSharedFileFn,
  getSharedFiles as GetSharedFilesFn,
  readSharedFile as ReadSharedFileFn,
  replaySharedThread as ReplaySharedThreadFn,
  resolveSharedFile as ResolveSharedFileFn,
  servedBytes as ServedBytesFn,
  servedObjectUrl as ServedObjectUrlFn,
  SharedThreadMetadata,
} from '../api';

const h = vi.hoisted(() => ({
  replayMock: vi.fn<typeof ReplaySharedThreadFn>(),
  getSharedFilesMock: vi.fn<typeof GetSharedFilesFn>(),
  readSharedFileMock: vi.fn<typeof ReadSharedFileFn>(),
  resolveSharedFileMock: vi.fn<typeof ResolveSharedFileFn>(),
  downloadSharedFileMock: vi.fn<typeof DownloadSharedFileFn>(),
  servedObjectUrlMock: vi.fn<typeof ServedObjectUrlFn>(),
  servedBytesMock: vi.fn<typeof ServedBytesFn>(),
  toastSpy: vi.fn<typeof ToastFn>(),
}));

vi.mock('react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router')>();
  return {
    ...actual,
    Link: ({ children, to }: { children?: React.ReactNode; to: string }) => <a href={to}>{children}</a>,
  };
});

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ isLoggedIn: false, isInitialized: true, userId: null }),
}));

vi.mock('../../../contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}));

vi.mock('@/hooks/useWorkspace', () => ({
  useWorkspace: () => ({ data: undefined }),
}));

vi.mock('../../ChatAgent/hooks/usePTCDispatchStatus', () => ({
  DispatchStatusProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('../../ChatAgent/components/FilePanelMemo', () => ({
  memoMimeForName: () => null,
  useAddToMemo: () => vi.fn<() => Promise<void>>(async () => undefined),
  useWorkspaceMemoIndex: () => new Map(),
  useMemoStaleCheck: () => ({ status: null, sandboxText: null, refresh: () => {} }),
  MemoStaleBanner: () => null,
  MemoDiffModal: () => null,
}));

vi.mock('../../ChatAgent/components/SandboxSettingsPanel', () => ({
  SandboxSettingsContent: () => null,
}));

vi.mock('@/components/ui/use-toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/ui/use-toast')>();
  return {
    ...actual,
    toast: (...args: Parameters<typeof ToastFn>) => {
      h.toastSpy(...args);
      return actual.toast(...args);
    },
  };
});

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    replaySharedThread: (...args: Parameters<typeof ReplaySharedThreadFn>) => h.replayMock(...args),
    getSharedFiles: (...args: Parameters<typeof GetSharedFilesFn>) => h.getSharedFilesMock(...args),
    readSharedFile: (...args: Parameters<typeof ReadSharedFileFn>) => h.readSharedFileMock(...args),
    resolveSharedFile: (...args: Parameters<typeof ResolveSharedFileFn>) => h.resolveSharedFileMock(...args),
    downloadSharedFile: (...args: Parameters<typeof DownloadSharedFileFn>) => h.downloadSharedFileMock(...args),
    servedObjectUrl: (...args: Parameters<typeof ServedObjectUrlFn>) => h.servedObjectUrlMock(...args),
    servedBytes: (...args: Parameters<typeof ServedBytesFn>) => h.servedBytesMock(...args),
  };
});

import SharedChatView from '../SharedChatView';
import * as sharedImageCache from '@/lib/dalton/sharedImageCache';

function makeMetadata(permissions: Record<string, unknown>): SharedThreadMetadata {
  return {
    kind: 'thread',
    thread_id: 'thread-fp-1',
    title: 'Shared Research',
    msg_type: 'chat',
    created_at: '2026-11-27T00:00:00Z',
    updated_at: '2026-11-27T00:00:00Z',
    workspace_name: 'Research',
    permissions,
  };
}

describe('SharedChatView real FilePanel download 403 handling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    h.getSharedFilesMock.mockResolvedValue({ path: '.', files: ['report.md'], source: 'live' });
    h.readSharedFileMock.mockResolvedValue({
      path: 'report.md',
      content: '# Quarterly Findings\n\nRevenue grew 24% YoY.',
      mime: 'text/markdown',
      offset: 0,
      limit: 1000,
      truncated: false,
    });
    h.replayMock.mockImplementation(async (_token, onEvent) => {
      onEvent?.({ event: 'user_message', turn_index: 0, content: 'Summarize Q3' });
      onEvent?.({
        event: 'message_chunk',
        turn_index: 0,
        role: 'assistant',
        content_type: 'text',
        content: 'See the report in the workspace panel.',
      });
      onEvent?.({ event: 'replay_done', thread_id: 'thread-fp-1' });
    });
  });

  afterEach(() => {
    sharedImageCache.revokeSharedImageScope('tok-real-fp');
  });

  it('keeps the open file preview intact in real FilePanel when downloading from the file tree returns 403', async () => {
    const revokeSpy = vi.spyOn(sharedImageCache, 'revokeSharedImageScope');
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const onPermissionForbidden = vi.fn<() => void>();
    const err403 = Object.assign(new Error('File download not permitted'), { response: { status: 403 } });
    h.downloadSharedFileMock.mockRejectedValueOnce(err403);

    try {
      const { container, rerender } = renderWithProviders(
        <SharedChatView
          shareToken="tok-real-fp"
          metadata={makeMetadata({ allow_files: true, allow_download: true })}
          refreshStamp={1000}
          onPermissionForbidden={onPermissionForbidden}
        />,
      );

      expect(await screen.findByText('See the report in the workspace panel.')).toBeInTheDocument();

      // Open the real FilePanel and preview report.md
      const toggleBtn = container.querySelector('button[data-panel-toggle="header"]');
      expect(toggleBtn).not.toBeNull();
      fireEvent.click(toggleBtn!);

      const treeFile = await screen.findByText('report.md');
      fireEvent.click(treeFile);

      expect(await screen.findByText('Revenue grew 24% YoY.')).toBeInTheDocument();

      // Re-open the file tree column beside the open preview, then download report.md from the tree
      let fileTreeRow = container.querySelector<HTMLElement>('[data-row-path="report.md"]');
      if (!fileTreeRow) {
        const treeToggle = container.querySelector<HTMLButtonElement>('button[aria-pressed="false"].file-panel-icon-btn');
        expect(treeToggle).not.toBeNull();
        fireEvent.click(treeToggle!);
        await waitFor(() => {
          fileTreeRow = container.querySelector<HTMLElement>('[data-row-path="report.md"]');
          expect(fileTreeRow).not.toBeNull();
        });
      }
      fireEvent.contextMenu(fileTreeRow!);

      const downloadMenuItem = await screen.findByRole('menuitem', { name: /Download|下载/ });
      fireEvent.click(downloadMenuItem);

      await waitFor(() => {
        expect(h.downloadSharedFileMock).toHaveBeenCalledTimes(1);
        expect(onPermissionForbidden).toHaveBeenCalledTimes(1);
      });

      // Only the download-not-permitted toast is shown (no duplicate generic downloadFailed toast)
      expect(h.toastSpy).toHaveBeenCalledTimes(1);
      expect(h.toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          description: expect.stringMatching(/Downloads are turned off for this share\.|当前分享未开放下载/),
        }),
      );
      expect(consoleErrorSpy).not.toHaveBeenCalled();

      // The open preview MUST stay intact and NOT be replaced by "Access denied"
      expect(screen.getByText('Revenue grew 24% YoY.')).toBeInTheDocument();
      expect(screen.queryByText(/Access denied|访问被拒绝/i)).not.toBeInTheDocument();

      // Metadata revalidation confirms allow_files=true, allow_download=false
      rerender(
        <SharedChatView
          shareToken="tok-real-fp"
          metadata={makeMetadata({ allow_files: true, allow_download: false })}
          refreshStamp={2000}
          onPermissionForbidden={onPermissionForbidden}
        />,
      );

      expect(screen.getByText('Revenue grew 24% YoY.')).toBeInTheDocument();
      expect(screen.queryByText(/Access denied|访问被拒绝/i)).not.toBeInTheDocument();
      expect(h.replayMock).toHaveBeenCalledTimes(1);
      expect(revokeSpy).not.toHaveBeenCalled();
    } finally {
      revokeSpy.mockRestore();
      consoleErrorSpy.mockRestore();
    }
  });
});
