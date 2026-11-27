import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import {
  advanceShareRevalidationGate,
  classifyShareForbidden,
  clearSharedFileBodies,
  createAbortError,
  createShareFileEpoch,
  createShareRevalidationGate,
  getStaleThreadFilesGate,
  isShareDowngradeActive,
  isShareDownloadHandled,
  isSharedFileBodyQueryKey,
  isShareRevalidated,
  runGuardedShareCardDownload,
  runGuardedShareFileRead,
  shouldUnmountShareView,
  type ShareForbiddenSource,
} from '../shareFileGuard';
import { downloadTarget } from '@/pages/ChatAgent/utils/fileRefResolver';

describe('shareFileGuard', () => {
  it('classifies all six 403 sources into files vs download', () => {
    const fileSources: ShareForbiddenSource[] = ['list', 'read', 'serve', 'resolve', 'media'];
    for (const source of fileSources) {
      expect(classifyShareForbidden(source)).toBe('files');
    }
    expect(classifyShareForbidden('download')).toBe('download');
  });

  it('aborts epoch signal, flips isCurrent to false on revoke, and discards late reads with AbortError', async () => {
    const epoch = createShareFileEpoch();
    expect(epoch.isCurrent()).toBe(true);
    expect(epoch.signal.aborted).toBe(false);

    let resolveSlow!: (value: string) => void;
    let capturedSignal: AbortSignal | undefined;
    const onFilesForbidden = vi.fn<() => void>();
    const onDownloadForbidden = vi.fn<() => void>();
    const onDiscard = vi.fn<(val: string) => void>();

    const pending = runGuardedShareFileRead({
      epoch,
      source: 'serve',
      read: (signal) => {
        capturedSignal = signal;
        return new Promise<string>((resolve) => {
          resolveSlow = resolve;
        });
      },
      onFilesForbidden,
      onDownloadForbidden,
      onDiscard,
    });

    expect(capturedSignal).toBe(epoch.signal);
    epoch.revoke();
    // Idempotent second revoke
    epoch.revoke();
    expect(epoch.isCurrent()).toBe(false);
    expect(epoch.signal.aborted).toBe(true);

    // Late resolution must be discarded and reject with AbortError
    resolveSlow('blob:late-url');
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(onDiscard).toHaveBeenCalledWith('blob:late-url');
    expect(onFilesForbidden).not.toHaveBeenCalled();
    expect(onDownloadForbidden).not.toHaveBeenCalled();
  });

  it('routes 403 errors to files vs download callbacks according to source', async () => {
    const epoch = createShareFileEpoch();
    const onFilesForbidden = vi.fn<() => void>();
    const onDownloadForbidden = vi.fn<() => void>();
    const err403 = Object.assign(new Error('Forbidden'), { response: { status: 403 } });

    await expect(
      runGuardedShareFileRead({
        epoch,
        source: 'download',
        read: async () => {
          throw err403;
        },
        onFilesForbidden,
        onDownloadForbidden,
      }),
    ).rejects.toBe(err403);
    expect(onDownloadForbidden).toHaveBeenCalledTimes(1);
    expect(onFilesForbidden).not.toHaveBeenCalled();

    await expect(
      runGuardedShareFileRead({
        epoch,
        source: 'read',
        read: async () => {
          throw err403;
        },
        onFilesForbidden,
        onDownloadForbidden,
      }),
    ).rejects.toBe(err403);
    expect(onFilesForbidden).toHaveBeenCalledTimes(1);
    expect(onDownloadForbidden).toHaveBeenCalledTimes(1);
  });

  it('clearSharedFileBodies cancels and removes only adapter: scoped file bodies while keeping real workspace bodies', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 5 * 60_000 } },
    });

    const adapterKey1 = queryKeys.workspaceFiles.body('adapter:mount-1', 'notes.md', 'text');
    const adapterKey2 = queryKeys.workspaceFiles.body('adapter:mount-2', 'report.html', 'full');
    const workspaceKey = queryKeys.workspaceFiles.body('ws-real-42', 'notes.md', 'text');
    const otherKey = ['share', 'metadata', 'tok-1'];

    queryClient.setQueryData(adapterKey1, { text: 'shared secret' });
    queryClient.setQueryData(adapterKey2, { text: 'shared html' });
    queryClient.setQueryData(workspaceKey, { text: 'owner workspace doc' });
    queryClient.setQueryData(otherKey, { kind: 'thread' });

    expect(isSharedFileBodyQueryKey(adapterKey1)).toBe(true);
    expect(isSharedFileBodyQueryKey(workspaceKey)).toBe(false);
    expect(isSharedFileBodyQueryKey(otherKey)).toBe(false);

    const cancelSpy = vi.spyOn(queryClient, 'cancelQueries');
    await clearSharedFileBodies(queryClient);

    expect(cancelSpy).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData(adapterKey1)).toBeUndefined();
    expect(queryClient.getQueryData(adapterKey2)).toBeUndefined();
    expect(
      queryClient
        .getQueryCache()
        .findAll({ predicate: (q) => isSharedFileBodyQueryKey(q.queryKey) }),
    ).toHaveLength(0);

    // Real workspace body and unrelated caches remain intact
    expect(queryClient.getQueryData(workspaceKey)).toEqual({ text: 'owner workspace doc' });
    expect(queryClient.getQueryData(otherKey)).toEqual({ kind: 'thread' });
  });

  it('clears a recorded 403 downgrade only when a strictly newer refreshStamp affirms allow_files=true', () => {
    const record = { token: 'tok-1', stamp: 1000 };
    expect(isShareDowngradeActive(record, 'tok-1', 1000, true)).toBe(true);
    expect(isShareDowngradeActive(record, 'tok-1', 2000, false)).toBe(true);
    expect(isShareDowngradeActive(record, 'tok-1', 2000, true)).toBe(false);
    expect(isShareDowngradeActive(record, 'tok-other', 1000, true)).toBe(false);
  });

  it('unmounts thread and public file views on revocation while gating stale allow_files=true until fresh success', () => {
    expect(shouldUnmountShareView({ kind: 'thread' }, true)).toBe(true);
    expect(shouldUnmountShareView({ kind: 'file', access: 'public' }, true)).toBe(true);
    expect(shouldUnmountShareView({ kind: 'file', access: 'owner' }, true)).toBe(false);
    expect(shouldUnmountShareView({ kind: 'app' }, true)).toBe(false);
    expect(shouldUnmountShareView({ kind: 'thread' }, false)).toBe(false);

    const initial = createShareRevalidationGate(
      'k1',
      { isFetching: true, isSuccess: true, isError: false, dataUpdatedAt: 500 },
      1000,
    );
    expect(isShareRevalidated(initial, 'k1', { isFetching: true, isSuccess: true, isError: false, dataUpdatedAt: 500 })).toBe(false);
    expect(
      getStaleThreadFilesGate({ kind: 'thread', permissions: { allow_files: true } }, { isFetching: true, isError: false }, false),
    ).toBe('loading');

    // Failed initial revalidation must NOT mark revalidated and must return 'failed'
    const afterFail = advanceShareRevalidationGate(
      initial,
      'k1',
      { isFetching: false, isSuccess: false, isError: true, dataUpdatedAt: 500 },
      1100,
    );
    expect(afterFail.revalidated).toBe(false);
    expect(
      getStaleThreadFilesGate({ kind: 'thread', permissions: { allow_files: true } }, { isFetching: false, isError: true }, false),
    ).toBe('failed');

    // Fresh success marks revalidated; subsequent error still allows last good view
    const afterSuccess = advanceShareRevalidationGate(
      afterFail,
      'k1',
      { isFetching: false, isSuccess: true, isError: false, dataUpdatedAt: 1200 },
      1200,
    );
    expect(afterSuccess.revalidated).toBe(true);
    expect(
      getStaleThreadFilesGate({ kind: 'thread', permissions: { allow_files: true } }, { isFetching: false, isError: true }, true),
    ).toBe('allow');
  });

  it('marks download 403 and AbortError as handled while leaving other 403/500 errors unhandled', async () => {
    const epoch = createShareFileEpoch();
    const onFilesForbidden = vi.fn<() => void>();
    const onDownloadForbidden = vi.fn<() => void>();
    const download403 = Object.freeze(Object.assign(new Error('File download not permitted'), { response: { status: 403 } }));
    const read403 = Object.assign(new Error('File access not permitted'), { response: { status: 403 } });
    const server500 = Object.assign(new Error('Internal error'), { response: { status: 500 } });

    await expect(
      runGuardedShareFileRead({
        epoch,
        source: 'download',
        read: async () => {
          throw download403;
        },
        onFilesForbidden,
        onDownloadForbidden,
      }),
    ).rejects.toBe(download403);

    expect(isShareDownloadHandled(download403)).toBe(true);
    expect(isShareDownloadHandled(createAbortError())).toBe(true);

    await expect(
      runGuardedShareFileRead({
        epoch,
        source: 'read',
        read: async () => {
          throw read403;
        },
        onFilesForbidden,
        onDownloadForbidden,
      }),
    ).rejects.toBe(read403);

    expect(isShareDownloadHandled(read403)).toBe(false);
    expect(isShareDownloadHandled(server500)).toBe(false);
  });

  it('runGuardedShareCardDownload shares one epoch across resolve and save and aborts on revoke or resolve 403', async () => {
    // 1. Happy path shares the exact same AbortSignal across both steps
    const okEpoch = createShareFileEpoch();
    const signals: AbortSignal[] = [];
    const resolveOk = vi.fn<
      (candidates: string[], recentWrites: string[], signal: AbortSignal) => Promise<{ status: string; path?: string | null }>
    >(async (_c, _w, signal) => {
      signals.push(signal);
      return { status: 'resolved', path: 'work/report.md' };
    });
    const downloadOk = vi.fn<(resolvedPath: string, signal: AbortSignal) => Promise<void>>(async (_p, signal) => {
      signals.push(signal);
    });

    await expect(
      runGuardedShareCardDownload({
        epoch: okEpoch,
        path: 'report.md',
        recentWrites: [],
        resolveTarget: downloadTarget,
        resolveFile: resolveOk,
        downloadFile: downloadOk,
        onFilesForbidden: vi.fn<() => void>(),
        onDownloadForbidden: vi.fn<() => void>(),
      }),
    ).resolves.toBe('saved');
    expect(signals).toHaveLength(2);
    expect(signals[0]).toBe(okEpoch.signal);
    expect(signals[1]).toBe(okEpoch.signal);

    // 2. Revoking during resolve aborts cleanly without entering downloadFile
    const revokedEpoch = createShareFileEpoch();
    let resolveLate!: (val: { status: string; path?: string | null }) => void;
    const downloadNever = vi.fn<(resolvedPath: string, signal: AbortSignal) => Promise<void>>();
    const pending = runGuardedShareCardDownload({
      epoch: revokedEpoch,
      path: 'report.md',
      recentWrites: [],
      resolveTarget: downloadTarget,
      resolveFile: () =>
        new Promise((resolve) => {
          resolveLate = resolve;
        }),
      downloadFile: downloadNever,
      onFilesForbidden: vi.fn<() => void>(),
      onDownloadForbidden: vi.fn<() => void>(),
    });
    revokedEpoch.revoke();
    resolveLate({ status: 'resolved', path: 'work/report.md' });
    await expect(pending).resolves.toBe('aborted');
    expect(downloadNever).not.toHaveBeenCalled();

    // 3. Resolve returning 403 revokes epoch via onFilesForbidden and never falls through to downloadFile
    const forbiddenEpoch = createShareFileEpoch();
    const onFilesForbidden = vi.fn<() => void>(() => forbiddenEpoch.revoke());
    const downloadAfter403 = vi.fn<(resolvedPath: string, signal: AbortSignal) => Promise<void>>();
    await expect(
      runGuardedShareCardDownload({
        epoch: forbiddenEpoch,
        path: 'report.md',
        recentWrites: [],
        resolveTarget: downloadTarget,
        resolveFile: async () => {
          throw Object.assign(new Error('Forbidden'), { response: { status: 403 } });
        },
        downloadFile: downloadAfter403,
        onFilesForbidden,
        onDownloadForbidden: vi.fn<() => void>(),
      }),
    ).resolves.toBe('aborted');
    expect(onFilesForbidden).toHaveBeenCalledTimes(1);
    expect(downloadAfter403).not.toHaveBeenCalled();
  });
});
