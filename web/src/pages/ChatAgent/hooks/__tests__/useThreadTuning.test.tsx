/**
 * A thread may hold its own reasoning effort and fast mode over what Settings
 * gives its model, and a row with neither follows Settings. A pick on a live
 * thread moves the cached row and PATCHes exactly what was picked, null
 * included, which returns the thread to Settings. A thread not created yet
 * holds its picks for the create. A pick the new row does not hold, made while
 * the create was out or lost when the create failed and the legacy send minted
 * the thread, is PATCHed once the row is read.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders, createTestQueryClient } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import type { Thread } from '@/types/api';
import type { updateThread as updateThreadApi } from '../../utils/api';
import { useThreadTuning } from '../useThreadTuning';

const mocks = vi.hoisted(() => ({
  getThread: vi.fn(),
  updateThread: vi.fn<typeof updateThreadApi>(),
  toast: vi.fn(),
  // DALTON-HOOK(antigravity-series): configurable Dalton build & shared-list/features/user state for deferral/flush & non-Dalton parity tests
  isDaltonBuild: false,
  sharedEnabled: false,
  sharedWorkspaces: undefined as Array<{ workspace: { workspace_id: string } }> | undefined,
  sharedError: false,
  featuresMode: 'default' as 'default' | 'loading' | 'error' | 'resolved',
  featuresData: [] as Array<{ key: string; enabled: boolean }>,
  currentUser: null as { user_id: string; organization?: { id: string; name: string } | null } | null,
  userError: false,
}));

vi.mock('../../utils/api', () => ({
  getThread: mocks.getThread,
  updateThread: mocks.updateThread,
}));

vi.mock('@/components/ui/use-toast', () => ({
  toast: mocks.toast,
}));

// DALTON-HOOK(antigravity-series): mock brand build flag and shared workspaces query for tuning write-permission tests
vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  get isDaltonBuild() {
    return mocks.isDaltonBuild;
  },
}));

vi.mock('@/hooks/useSharedWorkspaces', () => ({
  useSharedWorkspaces: () => ({
    enabled: mocks.sharedEnabled,
    data: mocks.sharedWorkspaces !== undefined ? { workspaces: mocks.sharedWorkspaces } : undefined,
    isLoading: mocks.sharedEnabled && mocks.sharedWorkspaces === undefined && !mocks.sharedError,
    isError: mocks.sharedError,
  }),
}));

// DALTON-HOOK(antigravity-series): mock features and user queries to verify unresolved deferral on first-read failure/loading
vi.mock('@/api/features', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/features')>();
  return {
    ...actual,
    getFeatures: () => {
      if (mocks.featuresMode === 'loading') return new Promise(() => {});
      if (mocks.featuresMode === 'error') return Promise.reject(new Error('features 500'));
      if (mocks.featuresMode === 'resolved') return Promise.resolve(mocks.featuresData);
      return actual.getFeatures();
    },
  };
});

vi.mock('@/hooks/useUser', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useUser')>();
  return {
    ...actual,
    useUser: (opts?: { enabled?: boolean }) => {
      if (mocks.featuresMode === 'default') return actual.useUser(opts);
      return {
        user: mocks.currentUser,
        isLoading: Boolean(opts?.enabled) && mocks.currentUser === null && !mocks.userError,
        isError: mocks.userError,
        refetch: vi.fn(),
      };
    },
  };
});

const THREAD = 'thread-1';

function row(tuning: { reasoning_effort?: string | null; fast_mode?: boolean | null } = {}, id = THREAD): Thread {
  return { thread_id: id, ...tuning } as unknown as Thread;
}

type Props = Parameters<typeof useThreadTuning>[0];

function setup(props: Partial<Props> = {}, cached: Thread | null = row()) {
  const queryClient = createTestQueryClient();
  if (cached) queryClient.setQueryData(queryKeys.threads.detail(THREAD), cached);
  mocks.getThread.mockResolvedValue(cached ?? row());
  const initial: Props = { threadId: THREAD, ...props };
  const view = renderHookWithProviders((p: Props = initial) => useThreadTuning(p), { queryClient });
  return { ...view, queryClient };
}

describe('useThreadTuning', () => {
  beforeEach(() => {
    mocks.getThread.mockReset();
    mocks.updateThread.mockReset();
    mocks.toast.mockReset();
    // DALTON-HOOK(antigravity-series): reset Dalton build and shared-workspaces/features/user defaults between tests
    mocks.isDaltonBuild = false;
    mocks.sharedEnabled = false;
    mocks.sharedWorkspaces = undefined;
    mocks.sharedError = false;
    mocks.featuresMode = 'default';
    mocks.featuresData = [];
    mocks.currentUser = null;
    mocks.userError = false;
  });

  describe('the value it reads', () => {
    it("is the row's own, and null where the row follows Settings", () => {
      const { result } = setup({}, row({ reasoning_effort: 'low', fast_mode: null }));
      expect(result.current.tuning).toEqual({ reasoningEffort: 'low', fastMode: null });
    });

    it('follows Settings for a row from a server that predates the fields', () => {
      expect(setup({}, row()).result.current.tuning).toEqual({ reasoningEffort: null, fastMode: null });
    });

    it('claims nothing on a live thread until its row is read', () => {
      mocks.getThread.mockReturnValue(new Promise(() => {}));
      const queryClient = createTestQueryClient();
      const { result } = renderHookWithProviders(() => useThreadTuning({ threadId: THREAD }), { queryClient });
      expect(result.current.tuning).toEqual({ reasoningEffort: undefined, fastMode: undefined });
    });

    it('follows Settings on a thread not created yet', () => {
      const { result } = setup({ threadId: '__default__' }, null);
      expect(result.current.tuning).toEqual({ reasoningEffort: null, fastMode: null });
      expect(result.current.toSend).toEqual({});
    });
  });

  describe('a pick on a live thread', () => {
    it('is saved on the row as picked, null included, and a send carries nothing', async () => {
      mocks.updateThread.mockResolvedValueOnce(row({ reasoning_effort: 'high' }));
      const { result, queryClient } = setup({}, row({ fast_mode: true }));

      await act(async () => { expect(await result.current.setReasoningEffort('high')).toBe(true); });
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, { reasoning_effort: 'high' });
      expect(result.current.tuning.reasoningEffort).toBe('high');

      mocks.updateThread.mockResolvedValueOnce(row({ fast_mode: null }));
      await act(async () => { await result.current.setFastMode(null); });
      expect(mocks.updateThread).toHaveBeenLastCalledWith(THREAD, { fast_mode: null });
      expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.fast_mode).toBeNull();
      expect(result.current.tuning.fastMode).toBeNull();
      expect(result.current.toSend).toEqual({});
    });

    it('puts the row back, and says so, when the server refuses it', async () => {
      mocks.updateThread.mockRejectedValue(Object.assign(new Error('400'), { response: { status: 400 } }));
      const { result } = setup({}, row({ reasoning_effort: 'low' }));

      let saved: boolean | undefined;
      await act(async () => { saved = await result.current.setReasoningEffort('high'); });
      expect(saved).toBe(false);
      expect(result.current.tuning.reasoningEffort).toBe('low');
      expect(mocks.toast).toHaveBeenCalledWith({
        description: "Couldn't change this thread's reasoning effort",
        variant: 'destructive',
      });
    });
  });

  describe('a thread not created yet', () => {
    it('holds a pick for the create, and only a pick', async () => {
      const { result } = setup({ threadId: '__default__' }, null);
      await act(async () => { await result.current.setReasoningEffort('high'); });
      await act(async () => { await result.current.setFastMode(false); });
      expect(result.current.tuning).toEqual({ reasoningEffort: 'high', fastMode: false });
      expect(result.current.toSend).toEqual({ reasoningEffortOverride: 'high', fastModeOverride: false });

      await act(async () => { await result.current.setReasoningEffort(null); });
      expect(result.current.toSend).toEqual({ fastModeOverride: false });
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    it("takes a navigation's picks, and a new navigation replaces them", () => {
      const { result, rerender } = setup({
        threadId: '__default__',
        landing: { key: 'a', reasoningEffort: 'low', fastMode: true },
      }, null);
      expect(result.current.toSend).toEqual({ reasoningEffortOverride: 'low', fastModeOverride: true });

      rerender({ threadId: '__default__', landing: { key: 'b', reasoningEffort: null, fastMode: null } });
      expect(result.current.toSend).toEqual({});
    });
  });

  describe('once the first send has created the thread', () => {
    it('saves nothing when the create stored the pick', async () => {
      const { result, rerender } = setup({ threadId: '__default__' }, null);
      await act(async () => { await result.current.setReasoningEffort('high'); });

      mocks.getThread.mockResolvedValue(row({ reasoning_effort: 'high' }));
      rerender({ threadId: THREAD });
      await waitFor(() => expect(mocks.getThread).toHaveBeenCalled());
      await act(async () => {});
      expect(result.current.tuning.reasoningEffort).toBe('high');
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    it('saves a pick the new row does not hold, once', async () => {
      // The create failed and the legacy send minted the thread, which keeps
      // no level of a send's.
      const { result, rerender } = setup({ threadId: '__default__' }, null);
      await act(async () => { await result.current.setFastMode(true); });

      mocks.getThread.mockResolvedValueOnce(row()).mockResolvedValue(row({ fast_mode: true }));
      mocks.updateThread.mockResolvedValue(row({ fast_mode: true }));
      rerender({
        threadId: THREAD,
        createdTuning: { threadId: THREAD, reasoningEffort: null, fastMode: null },
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        fast_mode: true,
        expected_fast_mode: null,
      }));
      await waitFor(() => expect(result.current.tuning.fastMode).toBe(true));

      rerender({
        threadId: THREAD,
        createdTuning: { threadId: THREAD, reasoningEffort: null, fastMode: null },
      });
      await act(async () => {});
      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
    });

    it('leaves a pick behind when the composer moves to another thread', async () => {
      const { result, rerender, queryClient } = setup({}, row({ reasoning_effort: 'low' }));
      mocks.updateThread.mockReturnValue(new Promise(() => {}));
      act(() => { void result.current.setReasoningEffort('high'); });

      queryClient.setQueryData(queryKeys.threads.detail('thread-2'), row({}, 'thread-2'));
      rerender({ threadId: 'thread-2' });
      expect(result.current.tuning.reasoningEffort).toBeNull();
    });
  });

  // DALTON-HOOK(antigravity-series): shared-list failure recovery flush on personal threads (Dalton build) & upstream parity on non-Dalton builds
  describe('shared-workspace deferral and recovery (Dalton build vs non-Dalton build)', () => {
    it('flushes effort and fast-mode PATCHes once a failed shared-list read recovers and confirms a personal workspace, persisting on reopen', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = undefined;
      mocks.sharedError = true;

      let serverRow = row({ reasoning_effort: 'low', fast_mode: false });
      mocks.updateThread.mockImplementation(async (_id, updates) => {
        serverRow = { ...serverRow, ...updates } as Thread;
        return serverRow;
      });

      const { result, rerender, unmount } = setup({ workspaceId: 'ws-personal' }, serverRow);

      await act(async () => {
        await result.current.setReasoningEffort('high');
        await result.current.setFastMode(true);
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.tuning).toEqual({ reasoningEffort: 'high', fastMode: true });

      // DALTON-HOOK(antigravity-series): shared list recovers and confirms ws-personal is personal -> flush deferred PATCHes
      mocks.sharedWorkspaces = [];
      mocks.sharedError = false;
      rerender({ threadId: THREAD, workspaceId: 'ws-personal' });

      // DALTON-HOOK(model-picker-races): deferred recovery flushes carry expected_* CAS guards
      await waitFor(() => {
        expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
          reasoning_effort: 'high',
          expected_reasoning_effort: 'low',
        });
        expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
          fast_mode: true,
          expected_fast_mode: false,
        });
      });
      expect(mocks.updateThread).toHaveBeenCalledTimes(2);
      unmount();

      // Reopen the thread with a fresh QueryClient reading the persisted server row.
      mocks.getThread.mockResolvedValue(serverRow);
      const reopened = renderHookWithProviders(
        () => useThreadTuning({ threadId: THREAD, workspaceId: 'ws-personal' }),
        { queryClient: createTestQueryClient() },
      );
      await waitFor(() => {
        expect(reopened.result.current.tuning).toEqual({ reasoningEffort: 'high', fastMode: true });
      });
    });

    // DALTON-HOOK(antigravity-series): non-Dalton builds must keep exact origin/dalton/main behavior — shared threads skip PATCH, personal threads PATCH immediately even when shared list fails
    it('keeps origin/dalton/main behavior on non-Dalton builds: skips PATCH on shared threads and PATCHes personal threads immediately when list fails', async () => {
      mocks.isDaltonBuild = false;
      mocks.sharedEnabled = true;

      // Confirmed shared workspace -> skips PATCH (matches origin/dalton/main useSharedEntry).
      mocks.sharedWorkspaces = [{ workspace: { workspace_id: 'ws-shared' } }];
      mocks.sharedError = false;
      const sharedView = setup(
        { workspaceId: 'ws-shared' },
        row({ reasoning_effort: 'low', fast_mode: false }),
      );
      await act(async () => {
        await sharedView.result.current.setReasoningEffort('high');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(sharedView.result.current.tuning.reasoningEffort).toBe('high');
      sharedView.unmount();

      // Personal thread when shared list read fails -> immediate PATCH (matches origin/dalton/main).
      mocks.sharedWorkspaces = undefined;
      mocks.sharedError = true;

      mocks.updateThread
        .mockResolvedValueOnce(row({ reasoning_effort: 'high', fast_mode: false }))
        .mockResolvedValueOnce(row({ reasoning_effort: 'high', fast_mode: true }));

      const { result } = setup(
        { workspaceId: 'ws-personal' },
        row({ reasoning_effort: 'low', fast_mode: false }),
      );

      await act(async () => {
        await result.current.setReasoningEffort('high');
        await result.current.setFastMode(true);
      });

      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, { reasoning_effort: 'high' });
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, { fast_mode: true });
      expect(mocks.updateThread).toHaveBeenCalledTimes(2);
      expect(result.current.tuning).toEqual({ reasoningEffort: 'high', fastMode: true });
    });

    // DALTON-HOOK(antigravity-series): Carbon #1 — thread.shared skips PATCH immediately and /features or /users/me failure defers PATCH in Dalton builds
    it('skips PATCH on thread.shared and defers PATCH when /features or /users/me fails in Dalton builds', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = false;
      mocks.featuresMode = 'error';

      const sharedRow = {
        ...row({ reasoning_effort: 'low', fast_mode: false }),
        shared: { started_by: { user_id: 'u-1', name: 'Alice' } },
      } as unknown as Thread;
      const sharedView = setup({ workspaceId: 'ws-shared' }, sharedRow);

      await act(async () => {
        await sharedView.result.current.setReasoningEffort('high');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(sharedView.result.current.tuning.reasoningEffort).toBe('high');
      sharedView.unmount();

      // /features returns shared_workspaces:true while /users/me fails -> must defer PATCH until resolved.
      mocks.featuresMode = 'resolved';
      mocks.featuresData = [{ key: 'shared_workspaces', enabled: true }];
      mocks.currentUser = null;
      mocks.userError = true;

      const userFailedView = setup(
        { workspaceId: 'ws-personal' },
        row({ reasoning_effort: 'low', fast_mode: false }),
      );
      await waitFor(() => expect(userFailedView.queryClient.getQueryData(queryKeys.features.list())).toBeDefined());

      await act(async () => {
        await userFailedView.result.current.setReasoningEffort('high');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(userFailedView.result.current.tuning.reasoningEffort).toBe('high');
    });

    // DALTON-HOOK(antigravity-series): Item 6 — switching effort or fast mode back to cached values while permission is unresolved must still persist the explicit pick once confirmed personal
    it('persists effort and fast-mode picks switched back to stale cached values while shared list is unresolved once confirmed personal', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = undefined;
      mocks.sharedError = false;

      mocks.updateThread
        .mockResolvedValueOnce(row({ reasoning_effort: 'low', fast_mode: false }))
        .mockResolvedValueOnce(row({ reasoning_effort: 'low', fast_mode: false }));

      const { result, rerender, queryClient } = setup(
        { workspaceId: 'ws-personal' },
        row({ reasoning_effort: 'low', fast_mode: false }),
      );

      // User changes effort & fast mode away from cache, then explicitly switches back to cached values while shared list is still loading.
      await act(async () => {
        await result.current.setReasoningEffort('high');
        await result.current.setFastMode(true);
        await result.current.setReasoningEffort('low');
        await result.current.setFastMode(false);
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.tuning).toEqual({ reasoningEffort: 'low', fastMode: false });

      // DALTON-HOOK(model-picker-races): when cached row has not moved externally, deferred flush carries expected_* CAS guards
      mocks.sharedWorkspaces = [];
      rerender({ threadId: THREAD, workspaceId: 'ws-personal' });

      await waitFor(() => {
        expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
          reasoning_effort: 'low',
          expected_reasoning_effort: 'low',
        });
        expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
          fast_mode: false,
          expected_fast_mode: false,
        });
      });
      expect(mocks.updateThread).toHaveBeenCalledTimes(2);
    });

    // DALTON-HOOK(model-picker-races): unresolved deferred tuning flush abandons on mid-wait external update and yields silently on 409
    it('abandons unresolved deferred tuning flush when another tab updated the row or server returns 409', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = undefined;
      mocks.sharedError = false;

      // Case A: mid-wait detail update from Tab B (low/false -> medium/true) before permission resolves.
      const viewA = setup(
        { workspaceId: 'ws-personal' },
        row({ reasoning_effort: 'low', fast_mode: false }),
      );
      await act(async () => {
        await viewA.result.current.setReasoningEffort('high');
        await viewA.result.current.setFastMode(true);
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      act(() => {
        viewA.queryClient.setQueryData(
          queryKeys.threads.detail(THREAD),
          row({ reasoning_effort: 'medium', fast_mode: true }),
        );
      });
      mocks.sharedWorkspaces = [];
      viewA.rerender({ threadId: THREAD, workspaceId: 'ws-personal' });
      await waitFor(() => {
        expect(viewA.result.current.tuning).toEqual({ reasoningEffort: 'medium', fastMode: true });
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      viewA.unmount();

      // Case B: cache stayed stale low -> flush sends expected_reasoning_effort: 'low' -> server 409 -> no toast, refetches medium.
      mocks.sharedWorkspaces = undefined;
      mocks.toast.mockClear();
      const viewB = setup(
        { workspaceId: 'ws-personal' },
        row({ reasoning_effort: 'low', fast_mode: false }),
      );
      await act(async () => {
        await viewB.result.current.setReasoningEffort('high');
      });

      mocks.updateThread.mockRejectedValueOnce(
        Object.assign(new Error('Conflict'), { response: { status: 409 } }),
      );
      mocks.getThread.mockResolvedValueOnce(row({ reasoning_effort: 'medium', fast_mode: false }));
      mocks.sharedWorkspaces = [];
      viewB.rerender({ threadId: THREAD, workspaceId: 'ws-personal' });

      await waitFor(() => {
        expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
          reasoning_effort: 'high',
          expected_reasoning_effort: 'low',
        });
      });
      await waitFor(() => {
        expect(viewB.result.current.tuning.reasoningEffort).toBe('medium');
      });
      expect(mocks.toast).not.toHaveBeenCalled();
      viewB.unmount();
    });

    // DALTON-HOOK(antigravity-series): R3 — when an effort PATCH fails while a non-Antigravity model PATCH is queued behind it, roll back the cached effort immediately before reread
    it('rolls back a failed reasoning_effort PATCH immediately while a non-Antigravity llm_model PATCH is queued behind it', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = [];
      mocks.sharedError = false;

      const { useThreadFieldSave } = await import('../useThreadFieldSave');
      let rejectEffort!: (err: Error) => void;
      let resolveModel!: (t: Thread) => void;
      mocks.updateThread
        .mockImplementationOnce(() => new Promise<Thread>((_res, rej) => { rejectEffort = rej; }))
        .mockImplementationOnce(() => new Promise<Thread>((res) => { resolveModel = res; }));

      const queryClient = createTestQueryClient();
      const initialRow = { thread_id: THREAD, llm_model: 'gpt-6', reasoning_effort: 'low', fast_mode: null } as unknown as Thread;
      queryClient.setQueryData(queryKeys.threads.detail(THREAD), initialRow);
      mocks.getThread.mockResolvedValue(initialRow);

      const { result } = renderHookWithProviders(() => ({
        tuning: useThreadTuning({ threadId: THREAD, workspaceId: 'ws-personal' }),
        saveModel: useThreadFieldSave({
          threadId: THREAD,
          field: 'llm_model',
          failedMessage: 'failed',
          onFailed: () => {},
        }),
      }), { queryClient });

      let effortP!: Promise<boolean>;
      let modelP!: Promise<boolean>;
      act(() => {
        effortP = result.current.tuning.setReasoningEffort('high');
        modelP = result.current.saveModel({ threadId: THREAD, value: 'claude-opus', before: 'gpt-6' });
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));

      // Effort PATCH fails while non-Antigravity model PATCH is still pending.
      await act(async () => {
        rejectEffort(new Error('500'));
        expect(await effortP).toBe(false);
      });

      // Before model PATCH settles or any reread happens, cached effort and tuning must already roll back to 'low'.
      expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.reasoning_effort).toBe('low');
      expect(result.current.tuning.tuning.reasoningEffort).toBe('low');

      await act(async () => {
        resolveModel({ thread_id: THREAD, llm_model: 'claude-opus', reasoning_effort: 'low', fast_mode: null } as unknown as Thread);
        expect(await modelP).toBe(true);
      });
    });

    // DALTON-HOOK(antigravity-series): R5a — queue two real mutations (effort PATCH then Antigravity model PATCH with reasoning_effort: null) and reject both sequentially; must restore server-confirmed 'low', not failed 'high'
    it('does not write back a failed optimistic effort into held when a queued Antigravity switch also fails and calls effortClear', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = [];
      mocks.sharedError = false;

      const { useThreadFieldSave } = await import('../useThreadFieldSave');
      let rejectEffort!: (err: Error) => void;
      let rejectModel!: (err: Error) => void;
      mocks.updateThread
        .mockImplementationOnce(() => new Promise<Thread>((_res, rej) => { rejectEffort = rej; }))
        .mockImplementationOnce(() => new Promise<Thread>((_res, rej) => { rejectModel = rej; }));

      const queryClient = createTestQueryClient();
      const initialRow = { thread_id: THREAD, llm_model: 'gpt-6', reasoning_effort: 'low', fast_mode: null } as unknown as Thread;
      queryClient.setQueryData(queryKeys.threads.detail(THREAD), initialRow);
      mocks.getThread.mockResolvedValue(initialRow);

      const { result } = renderHookWithProviders(() => ({
        tuning: useThreadTuning({ threadId: THREAD, workspaceId: 'ws-personal' }),
        saveModel: useThreadFieldSave({
          threadId: THREAD,
          field: 'llm_model',
          failedMessage: 'failed',
          onFailed: () => {},
        }),
      }), { queryClient });

      let effortP!: Promise<boolean>;
      let modelP!: Promise<boolean>;
      act(() => {
        effortP = result.current.tuning.setReasoningEffort('high');
        // Real queued Antigravity switch: clears local effort optimistically and queues a real llm_model+reasoning_effort PATCH behind effort='high'.
        const effortClear = result.current.tuning.clearLocalEffort();
        modelP = result.current.saveModel({
          threadId: THREAD,
          value: 'gemini-4-neon-medium-oauth',
          before: 'gpt-6',
          extraUpdates: { reasoning_effort: null },
        }).then((saved) => {
          if (saved) effortClear.confirm();
          else effortClear();
          return saved;
        });
      });
      expect(result.current.tuning.tuning.reasoningEffort).toBeNull();
      // Serialized scope: only the first mutation (effort='high') is on the wire initially.
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));
      expect(mocks.updateThread).toHaveBeenNthCalledWith(1, THREAD, { reasoning_effort: 'high' });

      // 1) First real mutation (effort='high') fails -> serialized scope advances and sends the second real mutation.
      await act(async () => {
        rejectEffort(new Error('500 effort'));
        expect(await effortP).toBe(false);
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      expect(mocks.updateThread).toHaveBeenNthCalledWith(2, THREAD, {
        llm_model: 'gemini-4-neon-medium-oauth',
        reasoning_effort: null,
      });

      // 2) Second real mutation (Antigravity model switch) also fails -> real onSettled invokes effortClear().
      await act(async () => {
        rejectModel(new Error('500 model'));
        expect(await modelP).toBe(false);
      });

      // Must fall back to the server-confirmed 'low' (both tuning display and cached thread detail), NOT the failed optimistic 'high'.
      expect(result.current.tuning.tuning.reasoningEffort).toBe('low');
      expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.reasoning_effort).toBe('low');
      expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.llm_model).toBe('gpt-6');
    });

    // DALTON-HOOK(model-picker-races): R2 Codex-H2 — when thread detail was still unread at pick time during unresolved permissions, cancel automatic effort & fast_mode flush instead of overwriting Tab B
    it('cancels unresolved deferred effort and fast_mode flush when thread detail arrives after user selection', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = undefined;
      mocks.sharedError = false;

      let resolveDetail!: (t: Thread) => void;
      const slowDetail = new Promise<Thread>((res) => { resolveDetail = res; });
      const queryClient = createTestQueryClient();
      mocks.getThread.mockReturnValueOnce(slowDetail);

      const { result, rerender } = renderHookWithProviders(
        (p: { threadId: string; workspaceId: string } = { threadId: THREAD, workspaceId: 'ws-personal' }) =>
          useThreadTuning(p),
        { queryClient },
      );

      // Detail is still loading (undefined) while permissions are unresolved; User A picks effort='high' and fast_mode=true.
      expect(result.current.tuning.reasoningEffort).toBeUndefined();
      expect(result.current.tuning.fastMode).toBeUndefined();
      await act(async () => {
        await result.current.setReasoningEffort('high');
        await result.current.setFastMode(true);
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // Detail returns late with Tab B's effort='low' & fast_mode=false, then permissions resolve to personal.
      const tabBRow = { thread_id: THREAD, llm_model: 'gpt-6', reasoning_effort: 'low', fast_mode: false } as unknown as Thread;
      await act(async () => {
        resolveDetail(tabBRow);
        await slowDetail;
      });
      mocks.sharedWorkspaces = [];
      rerender({ threadId: THREAD, workspaceId: 'ws-personal' });

      await waitFor(() => expect(result.current.tuning.reasoningEffort).toBe('low'));
      expect(result.current.tuning.fastMode).toBe(false);
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(model-picker-races): R3 Codex #3 — post-create tuning repatch carries create-request CAS guards (Dalton & Core), yields on Tab B drift or 409, preserves local pick on 503, and keeps local when old value is unknown
    it('R3 Codex #3: guards post-create tuning repatch with create-request values, yields on drift/409, preserves on 503, and keeps local when createdTuning is unknown', async () => {
      // Test with Core build (VITE_DALTON_UI=false) as well as Dalton semantics.
      mocks.isDaltonBuild = false;

      // 1) Tab B saved medium/true before A's detail read -> A does NOT PATCH and follows Tab B.
      const viewDrift = setup({ threadId: '__default__' }, null);
      await act(async () => {
        await viewDrift.result.current.setReasoningEffort('low');
        await viewDrift.result.current.setFastMode(false);
      });
      mocks.getThread.mockResolvedValueOnce(row({ reasoning_effort: 'medium', fast_mode: true }));
      viewDrift.rerender({
        threadId: THREAD,
        createdTuning: { threadId: THREAD, reasoningEffort: null, fastMode: null },
      });
      await waitFor(() => expect(viewDrift.result.current.tuning).toEqual({
        reasoningEffort: 'medium',
        fastMode: true,
      }));
      expect(mocks.updateThread).not.toHaveBeenCalled();
      viewDrift.unmount();

      // 2) Create stored high/false; A changed to low/true while waiting; conditional PATCH hits 409 -> silently follows Tab B's medium/false.
      mocks.updateThread.mockClear();
      mocks.toast.mockClear();
      const view409 = setup({ threadId: '__default__' }, null);
      await act(async () => {
        await view409.result.current.setReasoningEffort('low');
      });
      mocks.getThread
        .mockResolvedValueOnce(row({ reasoning_effort: 'high', fast_mode: false }))
        .mockResolvedValueOnce(row({ reasoning_effort: 'medium', fast_mode: false }));
      mocks.updateThread.mockRejectedValueOnce(
        Object.assign(new Error('Conflict'), { response: { status: 409 } }),
      );
      view409.rerender({
        threadId: THREAD,
        createdTuning: { threadId: THREAD, reasoningEffort: 'high', fastMode: false },
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        reasoning_effort: 'low',
        expected_reasoning_effort: 'high',
      }));
      await waitFor(() => expect(view409.result.current.tuning.reasoningEffort).toBe('medium'));
      expect(mocks.toast).not.toHaveBeenCalled();
      view409.unmount();

      // 3) Post-create conditional PATCH hits 503 -> preserves user's local pick ('low' / true).
      mocks.updateThread.mockClear();
      const view503 = setup({ threadId: '__default__' }, null);
      await act(async () => {
        await view503.result.current.setReasoningEffort('low');
        await view503.result.current.setFastMode(true);
      });
      mocks.getThread.mockResolvedValue(row({ reasoning_effort: null, fast_mode: null }));
      mocks.updateThread
        .mockRejectedValueOnce(Object.assign(new Error('503'), { response: { status: 503 } }))
        .mockRejectedValueOnce(Object.assign(new Error('503'), { response: { status: 503 } }));
      view503.rerender({
        threadId: THREAD,
        createdTuning: { threadId: THREAD, reasoningEffort: null, fastMode: null },
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        reasoning_effort: 'low',
        expected_reasoning_effort: null,
      }));
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        fast_mode: true,
        expected_fast_mode: null,
      }));
      await act(async () => {});
      expect(view503.result.current.tuning).toEqual({ reasoningEffort: 'low', fastMode: true });
      view503.unmount();

      // 4) createdTuning is null (old value cannot be confirmed) -> no PATCH, keeps user's pick locally.
      mocks.updateThread.mockClear();
      const viewUnknown = setup({ threadId: '__default__' }, null);
      await act(async () => {
        await viewUnknown.result.current.setReasoningEffort('low');
        await viewUnknown.result.current.setFastMode(true);
      });
      mocks.getThread.mockResolvedValueOnce(row({ reasoning_effort: 'medium', fast_mode: false }));
      viewUnknown.rerender({ threadId: THREAD, createdTuning: null });
      await waitFor(() => expect(mocks.getThread).toHaveBeenCalled());
      await act(async () => {});
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(viewUnknown.result.current.tuning).toEqual({ reasoningEffort: 'low', fastMode: true });
      viewUnknown.unmount();
    });

    // DALTON-HOOK(model-picker-races): R3 Codex #6 & Grok #1 — deferred permission-recovery tuning flush hitting 503 or network error preserves user's unsent effort and fast_mode selections
    it('R3 Codex #6 / Grok #1: preserves unsent effort and fast_mode picks when deferred permission-recovery flush hits 503 or network error', async () => {
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = undefined;
      mocks.sharedError = false;

      const view = setup(
        { workspaceId: 'ws-personal' },
        row({ reasoning_effort: 'low', fast_mode: false }),
      );
      await act(async () => {
        await view.result.current.setReasoningEffort('high');
        await view.result.current.setFastMode(true);
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      mocks.updateThread
        .mockRejectedValueOnce(Object.assign(new Error('503'), { response: { status: 503 } }))
        .mockRejectedValueOnce(new TypeError('Failed to fetch'));
      mocks.getThread.mockResolvedValue(row({ reasoning_effort: 'low', fast_mode: false }));

      mocks.sharedWorkspaces = [];
      view.rerender({ threadId: THREAD, workspaceId: 'ws-personal' });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        reasoning_effort: 'high',
        expected_reasoning_effort: 'low',
      }));
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        fast_mode: true,
        expected_fast_mode: false,
      }));
      await act(async () => {});

      expect(view.result.current.tuning).toEqual({ reasoningEffort: 'high', fastMode: true });
      view.unmount();
    });

    // DALTON-HOOK(model-picker-races): R4 Codex #3 — temporary optimistic cache writes while a background repatch/deferred PATCH is in flight MUST NOT clear held; delayed 503/network rejection preserves effort & fast_mode, and server confirmation clears held
    it('R4 Codex #3: keeps held across mid-flight optimistic cache renders so delayed 503/network rejection preserves effort and fast_mode, and clears held only after server confirmation', async () => {
      // 1) Post-create repatch with DELAYED rejection (Core & Dalton): optimistic cache write triggers mid-flight re-renders before rejection.
      mocks.isDaltonBuild = false;
      let rejectEffort!: (err: unknown) => void;
      let rejectFast!: (err: unknown) => void;
      const pendingEffort = new Promise<Thread>((_res, rej) => { rejectEffort = rej; });
      const pendingFast = new Promise<Thread>((_res, rej) => { rejectFast = rej; });
      mocks.updateThread
        .mockReturnValueOnce(pendingEffort)
        .mockReturnValueOnce(pendingFast);

      const initialServerRow = row({ reasoning_effort: null, fast_mode: null });
      const viewPostCreate = setup({ threadId: '__default__' }, null);
      mocks.getThread.mockResolvedValue(initialServerRow);
      await act(async () => {
        await viewPostCreate.result.current.setReasoningEffort('low');
        await viewPostCreate.result.current.setFastMode(true);
      });

      const createdBaseline = { threadId: THREAD, reasoningEffort: null, fastMode: null };
      viewPostCreate.rerender({ threadId: THREAD, createdTuning: createdBaseline });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));

      // Verify onMutate wrote temporary optimistic values into cache AND force mid-flight re-render while PATCHes are still pending.
      await waitFor(() => {
        const cached = viewPostCreate.queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD));
        expect(cached?.reasoning_effort).toBe('low');
        expect(cached?.fast_mode).toBe(true);
      });
      viewPostCreate.rerender({ threadId: THREAD, createdTuning: createdBaseline });
      expect(viewPostCreate.result.current.tuning).toEqual({ reasoningEffort: 'low', fastMode: true });

      // Now reject both delayed requests (503 + network error); cache rolls back & refetches null/null.
      await act(async () => {
        rejectEffort(Object.assign(new Error('503'), { response: { status: 503 } }));
        rejectFast(new TypeError('Failed to fetch'));
        await Promise.resolve();
      });
      await waitFor(() => {
        const cached = viewPostCreate.queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD));
        expect(cached?.reasoning_effort).toBeNull();
        expect(cached?.fast_mode).toBeNull();
      });

      // Local tuning selections MUST stay preserved despite mid-flight optimistic cache renders + rollback!
      expect(viewPostCreate.result.current.tuning).toEqual({ reasoningEffort: 'low', fastMode: true });
      viewPostCreate.unmount();

      // 2) Deferred permission-recovery flush with DELAYED rejection after mid-flight optimistic renders.
      mocks.updateThread.mockClear();
      mocks.isDaltonBuild = true;
      mocks.sharedEnabled = true;
      mocks.sharedWorkspaces = undefined;
      mocks.sharedError = false;

      let rejectDefEffort!: (err: unknown) => void;
      let rejectDefFast!: (err: unknown) => void;
      const pendingDefEffort = new Promise<Thread>((_res, rej) => { rejectDefEffort = rej; });
      const pendingDefFast = new Promise<Thread>((_res, rej) => { rejectDefFast = rej; });
      mocks.updateThread
        .mockReturnValueOnce(pendingDefEffort)
        .mockReturnValueOnce(pendingDefFast);

      const oldRow = row({ reasoning_effort: 'low', fast_mode: false });
      mocks.getThread.mockResolvedValue(oldRow);

      const viewDeferred = setup({ workspaceId: 'ws-personal' }, oldRow);
      await act(async () => {
        await viewDeferred.result.current.setReasoningEffort('high');
        await viewDeferred.result.current.setFastMode(true);
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      mocks.sharedWorkspaces = [];
      viewDeferred.rerender({ threadId: THREAD, workspaceId: 'ws-personal' });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));

      // Confirm temporary optimistic cache values landed and trigger mid-flight re-render while pending.
      await waitFor(() => {
        const cached = viewDeferred.queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD));
        expect(cached?.reasoning_effort).toBe('high');
        expect(cached?.fast_mode).toBe(true);
      });
      viewDeferred.rerender({ threadId: THREAD, workspaceId: 'ws-personal' });

      // Reject both delayed requests -> cache rolls back to low/false.
      await act(async () => {
        rejectDefEffort(Object.assign(new Error('503'), { response: { status: 503 } }));
        rejectDefFast(new TypeError('Failed to fetch'));
        await Promise.resolve();
      });
      await waitFor(() => {
        const cached = viewDeferred.queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD));
        expect(cached?.reasoning_effort).toBe('low');
        expect(cached?.fast_mode).toBe(false);
      });

      // Preserved!
      expect(viewDeferred.result.current.tuning).toEqual({ reasoningEffort: 'high', fastMode: true });

      // 3) Once a subsequent save succeeds on the server, held clears so future Tab B updates take effect.
      mocks.updateThread.mockClear();
      const confirmedHighRow = row({ reasoning_effort: 'high', fast_mode: false });
      mocks.getThread.mockResolvedValue(confirmedHighRow);
      let resolveSave!: (t: Thread) => void;
      mocks.updateThread.mockReturnValueOnce(new Promise<Thread>((res) => { resolveSave = res; }));
      let saveP!: Promise<boolean>;
      act(() => {
        saveP = viewDeferred.result.current.setReasoningEffort('high');
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));
      await act(async () => {
        resolveSave(confirmedHighRow);
        expect(await saveP).toBe(true);
      });
      act(() => {
        viewDeferred.queryClient.setQueryData(
          queryKeys.threads.detail(THREAD),
          row({ reasoning_effort: 'medium', fast_mode: false }),
        );
      });
      await waitFor(() => expect(viewDeferred.result.current.tuning.reasoningEffort).toBe('medium'));
      viewDeferred.unmount();
    });
  });
});
