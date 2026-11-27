import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import {
  advanceShareRevalidationGate,
  classifyShareForbidden,
  clearSharedFileBodies,
  createShareFileEpoch,
  createShareRevalidationGate,
  getStaleThreadFilesGate,
  isShareDowngradeActive,
  isSharedFileBodyQueryKey,
  isShareRevalidated,
  runGuardedShareFileRead,
  shouldUnmountShareView,
  type ShareForbiddenSource,
} from '../shareFileGuard';

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
});
