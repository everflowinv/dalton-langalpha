/**
 * The thread owns its model. The hook holds what the composer shows: the
 * row's model while the catalog carries it, else the mode default, following the row
 * when it moves. A pick is saved on the row, never on the account: the cached
 * detail and the model move first, and only the latest pick's failure puts
 * them back, with a toast. PATCHes on one thread go out one at a time, in
 * pick order. A thread not created yet has no row, so its pick is held for
 * the first send to carry. The default offer follows a pick and stays
 * dismissed for that thread.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders, createTestQueryClient } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { userSessionStorage } from '@/lib/userStorage';
import type { Thread } from '@/types/api';
import { useThreadFieldSave } from '../useThreadFieldSave';
import { CLAUDE_CODE } from '@/lib/claudeCode';
// DALTON-HOOK(antigravity-series): buildSeriesCatalog & catalog types for Antigravity series tests
import { buildSeriesCatalog, type AntigravitySeriesCatalog } from '@/lib/antigravitySeries';
import { useThreadModel } from '../useThreadModel';

const mocks = vi.hoisted(() => ({
  getThread: vi.fn(),
  updateThread: vi.fn(),
  toast: vi.fn(),
  defaultModel: 'model-default' as string | null,
  /** The Default a Chief of Staff turn starts on, where it is not the account's. */
  chiefOfStaffDefault: null as string | null,
  /** The thread the Default was last asked for, where the row follows it. */
  catalogModelNames: new Set<string>(),
  metadata: undefined as Record<string, { sdk?: string; provider?: string }> | undefined,
  // DALTON-HOOK(antigravity-series): series catalog + rawMetadata + Dalton build/auth & shared workspace / features / user query mocks
  isDaltonBuild: false,
  isDaltonAuth: false,
  seriesCatalog: { series: [], bySeriesId: {}, bySeriesPrefix: {}, byVariantKey: {} } as AntigravitySeriesCatalog,
  rawMetadata: undefined as Record<string, { provider?: string }> | undefined,
  sharedEnabled: true,
  sharedEntry: null as { workspace: { workspace_id: string } } | null | undefined,
  sharedError: false,
  featuresMode: 'default' as 'default' | 'loading' | 'error' | 'resolved',
  featuresData: [] as Array<{ key: string; enabled: boolean }>,
  currentUser: null as { user_id: string; organization?: { id: string; name: string } | null } | null,
  userError: false,
  /** The server's row, which a read returns unless a test says otherwise. */
  server: null as unknown,
}));

// A save the server accepts moves its row, so a read after it finds the save.
vi.mock('../../utils/api', () => ({
  getThread: mocks.getThread,
  updateThread: (...args: unknown[]) => Promise.resolve(mocks.updateThread(...args)).then((saved: unknown) => {
    mocks.server = saved;
    return saved;
  }),
}));

vi.mock('@/hooks/useSlotDefaultModels', () => ({
  useDefaultModel: () => mocks.defaultModel,
}));

vi.mock('@/hooks/useTurnModels', () => ({
  useTurnDefaultModel: (home: boolean) => (home && mocks.chiefOfStaffDefault) || mocks.defaultModel,
}));

// No sign-in has lapsed here; that hint has its own test.
vi.mock('@/hooks/useLapsedSignIn', () => ({ useLapsedSignIn: () => () => null }));
vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    catalogModelNames: mocks.catalogModelNames,
    metadata: mocks.metadata,
    // DALTON-HOOK(antigravity-series): expose seriesCatalog, rawApiResponse, and customModels
    seriesCatalog: mocks.seriesCatalog,
    rawApiResponse: mocks.rawMetadata ? { model_metadata: mocks.rawMetadata } : undefined,
    customModels: [],
  }),
}));
// DALTON-HOOK(antigravity-series): mock isDaltonBuild, isDaltonAuth, useSharedWorkspaces, getFeatures, and useUser to simulate Dalton vs non-Dalton builds and loading/error/shared/personal states
vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  get isDaltonBuild() {
    return mocks.isDaltonBuild;
  },
}));

vi.mock('@/config/daltonAuth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/daltonAuth')>()),
  get isDaltonAuth() {
    return mocks.isDaltonAuth;
  },
}));

vi.mock('@/hooks/useSharedWorkspaces', () => ({
  useSharedEntry: (_workspaceId?: string | null) => mocks.sharedEntry,
  useSharedWorkspaces: () => {
    if (!mocks.sharedEnabled || mocks.sharedEntry === undefined) {
      return {
        enabled: mocks.sharedEnabled,
        data: undefined,
        isLoading: mocks.sharedEnabled && !mocks.sharedError,
        isError: mocks.sharedEnabled && mocks.sharedError,
      };
    }
    return {
      enabled: true,
      data: { workspaces: mocks.sharedEntry ? [mocks.sharedEntry] : [] },
      isLoading: false,
      isError: false,
    };
  },
}));

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

vi.mock('@/components/ui/use-toast', () => ({
  toast: mocks.toast,
}));

const THREAD = 'thread-1';
const PICK_FAILED = { description: "Couldn't change this thread's model", variant: 'destructive' };

function row(llm_model: string | null): Thread {
  return { thread_id: THREAD, llm_model } as unknown as Thread;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

type Props = Parameters<typeof useThreadModel>[0];

function setup(
  props: Partial<Props> = {},
  cached: Thread | null = row('model-thread'),
  queryClient = createTestQueryClient(),
) {
  if (cached) queryClient.setQueryData(queryKeys.threads.detail(THREAD), cached);
  mocks.server = cached ?? row(null);
  mocks.getThread.mockImplementation(async () => mocks.server);
  const initial: Props = { threadId: THREAD, isLoading: false, ...props };
  const view = renderHookWithProviders(
    (p: Props = initial) => useThreadModel(p),
    { queryClient },
  );
  return { ...view, queryClient };
}

function cachedModel(queryClient: ReturnType<typeof createTestQueryClient>) {
  return queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.llm_model;
}

const failure = () => Object.assign(new Error('400'), { response: { status: 400 } });

describe('useThreadModel', () => {
  beforeEach(() => {
    mocks.getThread.mockReset();
    mocks.updateThread.mockReset();
    mocks.toast.mockReset();
    mocks.defaultModel = 'model-default';
    mocks.chiefOfStaffDefault = null;
    mocks.catalogModelNames = new Set();
    mocks.metadata = undefined;
    // DALTON-HOOK(antigravity-series): reset isDaltonBuild, isDaltonAuth, seriesCatalog, rawMetadata, sharedEnabled, sharedEntry, sharedError, featuresMode, and user state between tests
    mocks.isDaltonBuild = false;
    mocks.isDaltonAuth = false;
    mocks.seriesCatalog = buildSeriesCatalog([]);
    mocks.rawMetadata = undefined;
    mocks.sharedEnabled = true;
    mocks.sharedEntry = null;
    mocks.sharedError = false;
    mocks.featuresMode = 'default';
    mocks.featuresData = [];
    mocks.currentUser = null;
    mocks.userError = false;
    sessionStorage.clear();
  });

  describe('the model it holds', () => {
    it("opens on the thread's model from its row", () => {
      const { result } = setup();
      expect(result.current.model).toBe('model-thread');
    });

    // The account default may be a Claude Code model the Chief of Staff's
    // turn would not run, which the server reads as unset there.
    it('says the Claude Code Default is held only on a Chief of Staff thread that follows the Default, not one that holds the model it starts on', () => {
      mocks.defaultModel = 'model-code';
      mocks.chiefOfStaffDefault = 'model-deployment';
      mocks.catalogModelNames = new Set(['model-code', 'model-deployment']);
      mocks.metadata = { 'model-code': { sdk: CLAUDE_CODE } };
      expect(setup({ chiefOfStaff: true }, row(null)).result.current.heldDefault).toEqual({ model: 'model-code', fallback: 'model-deployment' });
      expect(setup({ chiefOfStaff: true }, row('model-deployment')).result.current.heldDefault).toBeNull();
      const retiredPin = setup({ chiefOfStaff: true }, row('model-retired')).result.current;
      expect(retiredPin.retired).toEqual({ model: 'model-retired', fallback: 'model-deployment' });
      expect(retiredPin.heldDefault).toBeNull();
    });

    it("opens a Chief of Staff thread with no model of its own on the Default its turn starts on", () => {
      mocks.chiefOfStaffDefault = 'model-deployment';
      expect(setup({ chiefOfStaff: true }, row(null)).result.current.model).toBe('model-deployment');
      expect(setup({ chiefOfStaff: true }, row('model-default')).result.current.model).toBe('model-default');
      expect(setup({}, row(null)).result.current.model).toBe('model-default');
    });

    // The server clears a model that left the catalog and resolves the
    // Default itself, so a thread whose row names no model, or one that left,
    // names none until a pick, which the row then carries.
    it.each([true, false])('sends no model on a thread whose row names none, until a pick (Chief of Staff: %s)', async (chiefOfStaff) => {
      mocks.updateThread.mockImplementation((_id: string, patch: { llm_model: string }) => row(patch.llm_model));
      const { result } = setup({ chiefOfStaff }, row(null));
      expect(result.current).toMatchObject({ model: 'model-default', sendModel: null });
      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current).toMatchObject({ model: 'model-beta', sendModel: 'model-beta' });
    });

    it.each([true, false])("shows the Default over a model that left the catalog but sends none, until a pick (Chief of Staff: %s)", async (chiefOfStaff) => {
      mocks.chiefOfStaffDefault = 'model-code';
      mocks.catalogModelNames = new Set(['model-default', 'model-code', 'model-beta']);
      mocks.updateThread.mockImplementation((_id: string, patch: { llm_model: string }) => row(patch.llm_model));
      const { result } = setup({ chiefOfStaff }, row('model-retired'));
      const shown = chiefOfStaff ? 'model-code' : 'model-default';
      expect(result.current).toMatchObject({ model: shown, sendModel: null, retired: { model: 'model-retired', fallback: shown } });
      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current).toMatchObject({ model: 'model-beta', sendModel: 'model-beta' });
    });

    it('sends a pick on a thread whose row names no model while its save is out', () => {
      mocks.updateThread.mockReturnValue(deferred<Thread>().promise);
      const { result } = setup({}, row(null));
      act(() => { void result.current.pickModel('model-beta'); });
      expect(result.current).toMatchObject({ model: 'model-beta', sendModel: 'model-beta' });
    });

    // DALTON-HOOK(agent-switch-model): cover manual-pick-only sendModel behavior on existing threads
    it('omits llm_model for existing threads unless manually picked, while sending initial model before thread creation', async () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-thread', 'model-beta']);
      expect(setup({ chiefOfStaff: true }, row('model-thread')).result.current.sendModel).toBeNull();
      const existing = setup({}, row('model-thread'));
      expect(existing.result.current.sendModel).toBeNull();
      await act(async () => { await existing.result.current.pickModel('model-beta'); });
      expect(existing.result.current.sendModel).toBe('model-beta');
      expect(setup({ chiefOfStaff: true, threadId: '__default__' }, null).result.current.sendModel).toBe('model-default');
    });

    it("falls back to the default when the row's model has left the catalog, and names it", () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-beta']);
      const { result } = setup({}, row('model-retired'));
      expect(result.current.model).toBe('model-default');
      expect(result.current.retired).toEqual({ model: 'model-retired', fallback: 'model-default' });
    });

    it('names no retired model before there is a default to name beside it', () => {
      mocks.catalogModelNames = new Set(['model-beta']);
      mocks.defaultModel = null;
      const { result } = setup({}, row('model-retired'));
      expect(result.current.retired).toBeNull();
    });

    it('follows the row when its model changes', async () => {
      const { result, queryClient } = setup();
      // Let the mount's own read land first, or it would answer over the move.
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      act(() => { queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-moved')); });
      await waitFor(() => expect(result.current.model).toBe('model-moved'));
    });

    it('follows a default change only on a thread with no model of its own', () => {
      const own = setup();
      mocks.defaultModel = 'model-gamma';
      own.rerender({ threadId: THREAD, isLoading: false });
      expect(own.result.current.model).toBe('model-thread');

      mocks.defaultModel = 'model-default';
      const following = setup({}, row(null));
      expect(following.result.current.model).toBe('model-default');
      mocks.defaultModel = 'model-gamma';
      following.rerender({ threadId: THREAD, isLoading: false });
      expect(following.result.current.model).toBe('model-gamma');
    });

    it('names no model until the row is read, so a send cannot store the default over it', async () => {
      const pending = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(pending.promise);
      const { result } = setup({}, null);
      expect(result.current.model).toBeNull();

      await act(async () => { pending.resolve(row('model-thread')); });
      await waitFor(() => expect(result.current.model).toBe('model-thread'));
    });

    it('names no model when the row could not be read', async () => {
      mocks.getThread.mockRejectedValueOnce(failure());
      const { result, queryClient } = setup({}, null);
      await waitFor(() => expect(queryClient.getQueryState(queryKeys.threads.detail(THREAD))?.status).toBe('error'));
      expect(result.current.model).toBeNull();
    });

    it('opens a thread started from another page on the model its first send named', () => {
      const { result } = setup({ threadId: '__default__', initialModel: 'model-beta' }, null);
      expect(result.current.model).toBe('model-beta');
    });
  });

  describe('a pick', () => {
    it('is saved on the thread, moving the model and the cached row first', async () => {
      const answer = deferred<Thread>();
      mocks.updateThread.mockReturnValue(answer.promise);
      const { result, queryClient } = setup();

      let saved!: Promise<boolean>;
      act(() => { saved = result.current.pickModel('model-beta'); });
      expect(result.current.model).toBe('model-beta');
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, { llm_model: 'model-beta' }));
      expect(cachedModel(queryClient)).toBe('model-beta');

      await act(async () => { answer.resolve(row('model-beta')); expect(await saved).toBe(true); });
      expect(cachedModel(queryClient)).toBe('model-beta');
      expect(result.current.model).toBe('model-beta');
      expect(mocks.toast).not.toHaveBeenCalled();
    });

    it('puts the model and the row back, and says so, when the server refuses it', async () => {
      mocks.updateThread.mockRejectedValue(failure());
      const { result, queryClient } = setup();

      let saved: boolean | undefined;
      await act(async () => { saved = await result.current.pickModel('model-beta'); });

      expect(saved).toBe(false);
      expect(result.current.model).toBe('model-thread');
      expect(cachedModel(queryClient)).toBe('model-thread');
      expect(mocks.toast).toHaveBeenCalledWith(PICK_FAILED);
      expect(result.current.offer).toBeNull();
    });

    it('puts a held pick back when there was no row cached to move', async () => {
      // The row never arrives, so the optimistic write has nothing to move.
      mocks.updateThread.mockRejectedValue(failure());
      const { result } = setup({}, null);
      mocks.getThread.mockReturnValue(new Promise(() => {}));

      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.model).toBeNull();
    });

    it('leaves another thread the composer moved to alone when a save fails late', async () => {
      const save = deferred<Thread>();
      mocks.updateThread.mockReturnValue(save.promise);
      const queryClient = createTestQueryClient();
      // The thread the composer leaves stays cached, as it does in the app;
      // the test client's gcTime of 0 would drop it once nothing observes it.
      queryClient.setQueryDefaults(queryKeys.threads.all, { gcTime: Infinity });
      const { result, rerender } = setup({}, row('model-thread'), queryClient);
      // The other thread's own row, which its mount reads again.
      const other = { thread_id: 'thread-2', llm_model: 'model-beta' } as unknown as Thread;
      mocks.getThread.mockImplementation(async (id: string) => (id === 'thread-2' ? other : row('model-thread')));
      let pick!: Promise<boolean>;
      act(() => { pick = result.current.pickModel('model-beta'); });

      queryClient.setQueryData(queryKeys.threads.detail('thread-2'), other);
      rerender({ threadId: 'thread-2', isLoading: false });
      await act(async () => { save.reject(failure()); await pick; });
      // Let that read land, so the result never depends on when it does.
      await waitFor(() => expect(mocks.getThread).toHaveBeenCalledWith('thread-2'));
      await act(async () => { await new Promise((r) => setTimeout(r, 30)); });

      expect(result.current.model).toBe('model-beta');
      expect(cachedModel(queryClient)).toBe('model-thread');
    });

    it('stays with its thread when the composer moves to another whose row is unread too', async () => {
      // Both rows unread seed the same null, so only the move can drop the pick.
      mocks.updateThread.mockResolvedValue(row('model-beta'));
      const { result, rerender } = setup({}, null);
      mocks.getThread.mockReturnValue(new Promise(() => {}));
      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.model).toBe('model-beta');

      rerender({ threadId: 'thread-2', isLoading: false });
      expect(result.current.model).toBeNull();
    });

    it('before the thread exists is held for the first send', async () => {
      const { result } = setup({ threadId: '__default__' }, null);
      let saved: boolean | undefined;
      await act(async () => { saved = await result.current.pickModel('model-beta'); });

      expect(saved).toBe(true);
      expect(result.current.model).toBe('model-beta');
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(mocks.getThread).not.toHaveBeenCalled();
      expect(result.current.offer).toEqual({ model: 'model-beta', defaultModel: 'model-default' });
    });
  });

  describe('picks that overlap', () => {
    // Fallback switch to F, then a menu pick of Y, then F's save fails: the
    // stale failure must not put the composer back on the old model while
    // the row says Y, or the next send would store the old model.
    it('leave the later pick standing when an earlier one fails behind it', async () => {
      const f = deferred<Thread>();
      const y = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(f.promise).mockReturnValueOnce(y.promise);
      const { result, queryClient } = setup();

      let fSaved!: Promise<boolean>;
      act(() => { fSaved = result.current.pickModel('model-fallback'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-fallback'));
      act(() => { void result.current.pickModel('model-y'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-y'));

      await act(async () => { f.reject(failure()); expect(await fSaved).toBe(false); });
      expect(result.current.model).toBe('model-y');
      expect(cachedModel(queryClient)).toBe('model-y');
      expect(mocks.toast).not.toHaveBeenCalled();

      await waitFor(() => expect(mocks.updateThread).toHaveBeenLastCalledWith(THREAD, { llm_model: 'model-y' }));
      await act(async () => { y.resolve(row('model-y')); });
      expect(result.current.model).toBe('model-y');
      expect(cachedModel(queryClient)).toBe('model-y');
    });

    // X (slow, fails), Y, X again: a value compare alone would see the first
    // X's failure as the latest pick's and revert to the model before it.
    it('keep a re-pick of the same model when its first save fails', async () => {
      const x1 = deferred<Thread>();
      const y = deferred<Thread>();
      const x2 = deferred<Thread>();
      mocks.updateThread
        .mockReturnValueOnce(x1.promise)
        .mockReturnValueOnce(y.promise)
        .mockReturnValueOnce(x2.promise);
      const { result, queryClient } = setup();

      act(() => { void result.current.pickModel('model-x'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-x'));
      act(() => { void result.current.pickModel('model-y'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-y'));
      act(() => { void result.current.pickModel('model-x'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-x'));

      // One PATCH at a time, in pick order.
      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
      await act(async () => { x1.reject(failure()); });
      expect(result.current.model).toBe('model-x');
      expect(cachedModel(queryClient)).toBe('model-x');

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      expect(mocks.updateThread).toHaveBeenLastCalledWith(THREAD, { llm_model: 'model-y' });
      await act(async () => { y.resolve(row('model-y')); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(3));
      expect(mocks.updateThread).toHaveBeenLastCalledWith(THREAD, { llm_model: 'model-x' });
      await act(async () => { x2.resolve(row('model-x')); });

      expect(result.current.model).toBe('model-x');
      expect(cachedModel(queryClient)).toBe('model-x');
      expect(mocks.toast).not.toHaveBeenCalled();
    });

    it('put back what the server holds when the latest fails behind one it accepted', async () => {
      const x = deferred<Thread>();
      const y = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(x.promise).mockReturnValueOnce(y.promise);
      const { result, queryClient } = setup();
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      act(() => { void result.current.pickModel('model-x'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-x'));
      act(() => { void result.current.pickModel('model-y'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-y'));

      // What the read-back after the failure finds: the pick it accepted.
      mocks.getThread.mockResolvedValue(row('model-x'));
      await act(async () => { x.resolve(row('model-x')); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      await act(async () => { y.reject(failure()); });

      expect(result.current.model).toBe('model-x');
      expect(cachedModel(queryClient)).toBe('model-x');
      expect(mocks.toast).toHaveBeenCalledTimes(1);
    });

    it('reads the row back after a failure, since the save may have landed', async () => {
      const lost = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(lost.promise);
      const { result, queryClient } = setup();
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      act(() => { void result.current.pickModel('model-beta'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-beta'));
      // The PATCH committed, but its response never arrived.
      const readBack = deferred<Thread>();
      mocks.getThread.mockReturnValue(readBack.promise);
      await act(async () => { lost.reject(new Error('Network Error')); });
      expect(result.current.model).toBe('model-thread');

      await act(async () => { readBack.resolve(row('model-beta')); });
      await waitFor(() => expect(result.current.model).toBe('model-beta'));
      expect(cachedModel(queryClient)).toBe('model-beta');
    });

    it('put back the row from before them all when every one fails', async () => {
      const x = deferred<Thread>();
      const y = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(x.promise).mockReturnValueOnce(y.promise);
      const { result, queryClient } = setup();

      act(() => { void result.current.pickModel('model-x'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-x'));
      act(() => { void result.current.pickModel('model-y'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-y'));

      await act(async () => { x.reject(failure()); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      await act(async () => { y.reject(failure()); });

      expect(result.current.model).toBe('model-thread');
      expect(cachedModel(queryClient)).toBe('model-thread');
      expect(mocks.toast).toHaveBeenCalledTimes(1);
    });
  });

  describe('the default offer', () => {
    it('follows a pick while the composer shows it, until dismissed for the thread', async () => {
      mocks.updateThread.mockResolvedValue(row('model-beta'));
      const { result, queryClient } = setup();
      expect(result.current.offer).toBeNull();

      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.offer).toEqual({ model: 'model-beta', defaultModel: 'model-default' });

      // The thread moving off the pick takes the offer with it.
      act(() => { queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-gamma')); });
      await waitFor(() => expect(result.current.model).toBe('model-gamma'));
      expect(result.current.offer).toBeNull();

      await act(async () => { await result.current.pickModel('model-beta'); });
      act(() => { result.current.dismissOffer(); });
      expect(result.current.offer).toBeNull();
      expect(userSessionStorage.getItem(`thread-model-offer-dismissed:${THREAD}`)).not.toBeNull();

      await act(async () => { await result.current.pickModel('model-gamma'); });
      expect(result.current.offer).toBeNull();
    });

    it('offers the model a thread started from another page was sent with', () => {
      const picked = setup({ threadId: '__default__', initialModel: 'model-beta' }, null);
      expect(picked.result.current.offer).toEqual({ model: 'model-beta', defaultModel: 'model-default' });

      const unpicked = setup({ threadId: '__default__', initialModel: 'model-default' }, null);
      expect(unpicked.result.current.offer).toBeNull();
    });

    it('stays with its thread when the composer moves to another', async () => {
      // The market panel keeps one composer across threads. The next thread
      // holding the same model must not inherit the offer made on this one.
      mocks.updateThread.mockResolvedValue(row('model-beta'));
      const { result, queryClient, rerender } = setup();
      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.offer).not.toBeNull();

      queryClient.setQueryData(queryKeys.threads.detail('thread-2'), { thread_id: 'thread-2', llm_model: 'model-beta' });
      rerender({ threadId: 'thread-2', isLoading: false });
      expect(result.current.model).toBe('model-beta');
      expect(result.current.offer).toBeNull();
    });

    it('is not made for a pick of the default itself', async () => {
      mocks.updateThread.mockResolvedValue(row('model-default'));
      const { result } = setup();
      await act(async () => { await result.current.pickModel('model-default'); });
      expect(result.current.offer).toBeNull();
    });
  });

  // DALTON-HOOK(ui-model-pick)
  describe('a thread opened from another page with a picked model (UI-MODEL-1)', () => {
    it('keeps the pick when the new thread row is read back naming the default model', async () => {
      // The home composer navigates to the new thread with its pick as
      // initialModel; the row is not read yet.
      const queryClient = createTestQueryClient();
      const { result, rerender } = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        queryClient,
      );
      expect(result.current.model).toBe('model-beta');

      // During the first turn the row is read back still naming the default.
      act(() => { queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-beta' });
      expect(result.current.model).toBe('model-beta');

      // The turn ends and the row catches up with the pick.
      act(() => { queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-beta')); });
      rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      expect(result.current.model).toBe('model-beta');
    });

    it('keeps a pick on an existing thread when a stale reread during the turn names the old model', async () => {
      const save = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(save.promise);
      const { result, rerender, queryClient } = setup({ threadId: THREAD, isLoading: false }, row('model-default'));
      let picked!: Promise<boolean>;
      act(() => { picked = result.current.pickModel('model-beta'); });
      expect(result.current.model).toBe('model-beta');

      // A reread started before the save landed returns the old model mid-turn.
      act(() => { queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      rerender({ threadId: THREAD, isLoading: true });
      expect(result.current.model).toBe('model-beta');

      // The turn ends; the composer keeps the pick across the turn-end reread.
      mocks.server = row('model-beta');
      rerender({ threadId: THREAD, isLoading: false });
      expect(result.current.model).toBe('model-beta');
      await act(async () => { save.resolve(row('model-beta')); await picked; });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-beta'));
      expect(result.current.model).toBe('model-beta');
    });

    it('holds the pick on the turn-end render until the reread settles, following a different server model only on success and keeping the pick on failure', async () => {
      // 1. Turn-end render before reread returns -> still the picked model;
      //    reread returns the picked model -> still the picked model.
      const rereadPicked = deferred<Thread>();
      const qc1 = createTestQueryClient();
      const first = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        qc1,
      );
      await waitFor(() => expect(qc1.isFetching()).toBe(0));
      act(() => { qc1.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      first.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-beta' });
      expect(first.result.current.model).toBe('model-beta');

      mocks.getThread.mockReturnValueOnce(rereadPicked.promise);
      first.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      // On the turn-end render itself, before the reread resolves:
      expect(first.result.current.model).toBe('model-beta');

      await act(async () => {
        await new Promise((r) => setTimeout(r, 5));
        rereadPicked.resolve(row('model-beta'));
      });
      await waitFor(() => expect(cachedModel(qc1)).toBe('model-beta'));
      expect(first.result.current.model).toBe('model-beta');

      // 2. Reread returns a different model -> follows the server model.
      const rereadOther = deferred<Thread>();
      const qc2 = createTestQueryClient();
      const second = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        qc2,
      );
      await waitFor(() => expect(qc2.isFetching()).toBe(0));
      act(() => { qc2.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      second.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-beta' });

      mocks.getThread.mockReturnValueOnce(rereadOther.promise);
      second.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      expect(second.result.current.model).toBe('model-beta');

      await act(async () => {
        await new Promise((r) => setTimeout(r, 5));
        rereadOther.resolve(row('model-gamma'));
      });
      await waitFor(() => expect(second.result.current.model).toBe('model-gamma'));

      // 3. Reread fails -> keeps the picked model instead of jumping to the stale cache.
      const rereadFailed = deferred<Thread>();
      const qc3 = createTestQueryClient();
      const third = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        qc3,
      );
      await waitFor(() => expect(qc3.isFetching()).toBe(0));
      act(() => { qc3.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      third.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-beta' });

      mocks.getThread.mockReturnValueOnce(rereadFailed.promise);
      third.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      expect(third.result.current.model).toBe('model-beta');

      await act(async () => {
        await new Promise((r) => setTimeout(r, 5));
        rereadFailed.reject(failure());
      });
      await waitFor(() => expect(qc3.isFetching()).toBe(0));
      expect(third.result.current.model).toBe('model-beta');
    });

    it('lands on the thread row model when two consecutive picks during a turn both fail', async () => {
      const x = deferred<Thread>();
      const y = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(x.promise).mockReturnValueOnce(y.promise);
      const { result, queryClient } = setup({ isLoading: true }, row('model-thread'));
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      act(() => { void result.current.pickModel('model-x'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-x'));
      act(() => { void result.current.pickModel('model-y'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-y'));

      await act(async () => { x.reject(failure()); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      await act(async () => { y.reject(failure()); });

      expect(result.current.model).toBe('model-thread');
      expect(cachedModel(queryClient)).toBe('model-thread');
    });

    it('switches to the default model when the current model is retired mid-turn', async () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-beta']);
      mocks.updateThread.mockResolvedValue(row('model-beta'));
      const { result, rerender } = setup({ threadId: THREAD, isLoading: true }, row('model-default'));

      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.model).toBe('model-beta');

      mocks.catalogModelNames = new Set(['model-default']);
      rerender({ threadId: THREAD, isLoading: true });

      expect(result.current.model).toBe('model-default');
      expect(result.current.retired).toEqual({ model: 'model-beta', fallback: 'model-default' });
    });

    it("shows thread B's model when switching from running thread A to thread B whose row arrives later", async () => {
      mocks.updateThread.mockResolvedValue(row('model-alpha'));
      const bRow = deferred<Thread>();
      const threadB = { thread_id: 'thread-2', llm_model: 'model-beta' } as unknown as Thread;
      const { result, rerender, queryClient } = setup({ threadId: THREAD, isLoading: true }, row('model-thread'));
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      await act(async () => { await result.current.pickModel('model-alpha'); });
      expect(result.current.model).toBe('model-alpha');

      mocks.getThread.mockImplementation((id: string) => (
        id === 'thread-2' ? bRow.promise : Promise.resolve(row('model-alpha'))
      ));
      rerender({ threadId: 'thread-2', isLoading: true });
      expect(result.current.model).toBeNull();

      await act(async () => { bRow.resolve(threadB); });
      await waitFor(() => expect(result.current.model).toBe('model-beta'));
    });

    it('keeps the pick when a title-only setQueryData lands after the turn ends before the reread returns', async () => {
      const reread = deferred<Thread>();
      const queryClient = createTestQueryClient();
      const { result, rerender } = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        queryClient,
      );
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      act(() => { queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-beta' });
      expect(result.current.model).toBe('model-beta');

      mocks.getThread.mockReturnValueOnce(reread.promise);
      rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      expect(result.current.model).toBe('model-beta');

      // A title event patches the cached detail row before the turn-end reread returns;
      // llm_model on the cached row is still the stale default.
      act(() => {
        queryClient.setQueryData<Thread>(queryKeys.threads.detail(THREAD), (current) => (
          current ? { ...current, title: 'Generated title' } : current
        ));
      });
      rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      expect(result.current.model).toBe('model-beta');

      await act(async () => { reread.resolve(row('model-beta')); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-beta'));
      expect(result.current.model).toBe('model-beta');
    });

    it('keeps the pick when the turn-end reread is cancelled or fails', async () => {
      // 1. Cancelled (e.g. another field write cancels the outgoing reread and updates dataUpdatedAt).
      const rereadCancelled = deferred<Thread>();
      const qc1 = createTestQueryClient();
      const first = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        qc1,
      );
      await waitFor(() => expect(qc1.isFetching()).toBe(0));
      act(() => { qc1.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      first.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-beta' });

      mocks.getThread.mockReturnValueOnce(rereadCancelled.promise);
      first.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      await waitFor(() => expect(qc1.isFetching()).toBe(1));

      await act(async () => {
        await qc1.cancelQueries({ queryKey: queryKeys.threads.detail(THREAD) });
        qc1.setQueryData(queryKeys.threads.detail(THREAD), {
          ...row('model-default'),
          subagents_allowed: false,
        } as Thread);
      });
      first.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      expect(first.result.current.model).toBe('model-beta');

      // 2. Failed.
      const rereadFailed = deferred<Thread>();
      const qc2 = createTestQueryClient();
      const second = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        qc2,
      );
      await waitFor(() => expect(qc2.isFetching()).toBe(0));
      act(() => { qc2.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      second.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-beta' });

      mocks.getThread.mockReturnValueOnce(rereadFailed.promise);
      second.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-beta' });
      await act(async () => { rereadFailed.reject(failure()); });
      await waitFor(() => expect(qc2.isFetching()).toBe(0));
      expect(second.result.current.model).toBe('model-beta');
    });

    it('follows the row back to the original model after an idle pick saves and another tab reverts it', async () => {
      mocks.updateThread.mockResolvedValue(row('model-beta'));
      const { result, queryClient } = setup({ threadId: THREAD, isLoading: false }, row('model-default'));
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.model).toBe('model-beta');
      expect(cachedModel(queryClient)).toBe('model-beta');

      // Another tab sets the thread back to model-default and the detail cache refreshes.
      act(() => { queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-default')); });
      await waitFor(() => expect(result.current.model).toBe('model-default'));
    });

    it('defers saving a pick made during thread creation until the first turn ends and its reread returns the first-send model', async () => {
      mocks.updateThread.mockResolvedValue(row('model-beta'));
      const queryClient = createTestQueryClient();
      const { result, rerender } = setup(
        { threadId: '__default__', isLoading: false },
        null,
        queryClient,
      );

      // First pick before sending, carried by the request that creates the thread.
      await act(async () => { await result.current.pickModel('model-alpha'); });
      rerender({ threadId: '__default__', isLoading: true });

      // Second pick while the thread is still being created (no liveThreadId yet).
      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.model).toBe('model-beta');
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // The thread id arrives while the first turn is still running; no save yet.
      mocks.server = row('model-alpha');
      rerender({ threadId: THREAD, isLoading: true });
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('model-beta');

      // First turn ends; reread returns model-alpha, then the deferred save writes model-beta.
      const turnEndReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(turnEndReread.promise);
      rerender({ threadId: THREAD, isLoading: false });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('model-beta');

      await act(async () => { turnEndReread.resolve(row('model-alpha')); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, { llm_model: 'model-beta' }));
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-beta'));
      expect(result.current.model).toBe('model-beta');
    });
  });

  describe('a confirmed pick on a thread whose row is not cached yet (UI-MODEL-1 r3)', () => {
    it('keeps the confirmed pick instead of falling back to initialModel', async () => {
      // Opened from another page with model-beta; the row has not been read.
      mocks.updateThread.mockResolvedValue(row('model-gamma'));
      const queryClient = createTestQueryClient();
      const { result } = setup(
        { threadId: THREAD, isLoading: true, initialModel: 'model-beta' },
        null,
        queryClient,
      );
      expect(result.current.model).toBe('model-beta');

      await act(async () => { await result.current.pickModel('model-gamma'); });
      expect(result.current.model).toBe('model-gamma');
    });
  });

  describe('when a turn ends', () => {
    it('rereads the row, since the send stored its model there', () => {
      const { rerender, queryClient } = setup({ isLoading: true });
      const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
      rerender({ threadId: THREAD, isLoading: false });
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.threads.detail(THREAD) });
    });

    it('leaves the reread to a pick still saving, which reads the row once it settles', async () => {
      const answer = deferred<Thread>();
      mocks.updateThread.mockReturnValue(answer.promise);
      const { result, rerender, queryClient } = setup({ isLoading: true });
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      const reads = mocks.getThread.mock.calls.length;
      act(() => { void result.current.pickModel('model-beta'); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalled());

      rerender({ threadId: THREAD, isLoading: false });
      // A read now would land over the pick.
      expect(mocks.getThread).toHaveBeenCalledTimes(reads);

      await act(async () => { answer.resolve(row('model-beta')); });
      await waitFor(() => expect(mocks.getThread).toHaveBeenCalledTimes(reads + 1));
      expect(result.current.model).toBe('model-beta');
    });

    it("finds a model the turn cleared while the subagents switch was saving", async () => {
      // The flip's save writes only its own field, so the model the turn
      // refused as removed stays cached unless the turn-end reread survives.
      const flip = deferred<Thread>();
      mocks.updateThread.mockReturnValue(flip.promise);
      const queryClient = createTestQueryClient();
      queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-thread'));
      mocks.server = row('model-thread');
      mocks.getThread.mockImplementation(async () => mocks.server);
      const { result, rerender } = renderHookWithProviders(
        (p: Props = { threadId: THREAD, isLoading: true }) => ({
          thread: useThreadModel(p),
          flip: useThreadFieldSave({
            threadId: THREAD,
            field: 'subagents_allowed',
            failedMessage: 'flip failed',
            onFailed: () => {},
          }),
        }),
        { queryClient },
      );
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      act(() => { void result.current.flip({ threadId: THREAD, value: false, before: null }); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalled());
      rerender({ threadId: THREAD, isLoading: false });

      await act(async () => { flip.resolve({ ...row(null), subagents_allowed: false } as Thread); });
      await waitFor(() => expect(cachedModel(queryClient)).toBeNull());
      expect(result.current.thread.model).toBe('model-default');
    });

    it('holds the reread until the save of every field has settled', async () => {
      const pick = deferred<Thread>();
      const flip = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(pick.promise).mockReturnValueOnce(flip.promise);
      const queryClient = createTestQueryClient();
      queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-thread'));
      mocks.server = row('model-thread');
      mocks.getThread.mockImplementation(async () => mocks.server);
      const { result, rerender } = renderHookWithProviders(
        (p: Props = { threadId: THREAD, isLoading: true }) => ({
          thread: useThreadModel(p),
          flip: useThreadFieldSave({
            threadId: THREAD,
            field: 'subagents_allowed',
            failedMessage: 'flip failed',
            onFailed: () => {},
          }),
        }),
        { queryClient },
      );
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      const reads = mocks.getThread.mock.calls.length;
      const cachedSwitch = () => queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.subagents_allowed;

      act(() => { void result.current.thread.pickModel('model-beta'); });
      act(() => { void result.current.flip({ threadId: THREAD, value: false, before: null }); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      rerender({ threadId: THREAD, isLoading: false });

      // The server has not taken the flip yet, so a read now would undo it.
      await act(async () => { pick.resolve(row('model-beta')); });
      expect(mocks.getThread).toHaveBeenCalledTimes(reads);
      expect(cachedSwitch()).toBe(false);

      await act(async () => { flip.resolve({ ...row('model-beta'), subagents_allowed: false } as Thread); });
      await waitFor(() => expect(mocks.getThread).toHaveBeenCalledTimes(reads + 1));
      expect(cachedSwitch()).toBe(false);
      expect(result.current.thread.model).toBe('model-beta');
    });

    it('keeps the reread when a later pick starts before the one it waited on settles', async () => {
      const first = deferred<Thread>();
      const second = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
      const { result, rerender, queryClient } = setup({ isLoading: true });
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      act(() => { void result.current.pickModel('model-x'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-x'));
      rerender({ threadId: THREAD, isLoading: false });
      // The second pick's optimistic write would clear the stale mark.
      act(() => { void result.current.pickModel('model-y'); });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('model-y'));

      await act(async () => { first.resolve(row('model-x')); });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      // The turn stored subagents on the row; the pick's answer carries it,
      // but the save writes only the model.
      await act(async () => { second.resolve({ ...row('model-y'), subagents_allowed: false } as Thread); });
      await waitFor(() => expect(
        queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.subagents_allowed,
      ).toBe(false));
      expect(result.current.model).toBe('model-y');
    });

    // DALTON-HOOK(agent-switch-model): cover turn-end thread model refresh and manualPick reset
    it('refreshes server-switched model at turn end and clears manual pick so next send does not overwrite it', async () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-thread', 'model-beta', 'model-agent']);
      mocks.server = row('model-thread');
      mocks.getThread.mockImplementation(async () => mocks.server);
      const { result, rerender, queryClient } = setup({ isLoading: true }, row('model-thread'));
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      // No manual pick on existing thread -> sendModel stays null.
      expect(result.current.sendModel).toBeNull();

      // Suppose the agent switched the thread model mid-turn on the server.
      mocks.server = row('model-agent');
      rerender({ threadId: THREAD, isLoading: false });

      await waitFor(() => expect(result.current.model).toBe('model-agent'));
      expect(result.current.sendModel).toBeNull();

      // User manually picks model-beta before sending -> sendModel includes it for that turn, then clears at turn end.
      await act(async () => { await result.current.pickModel('model-beta'); });
      expect(result.current.sendModel).toBe('model-beta');
      rerender({ threadId: THREAD, isLoading: true });
      mocks.server = row('model-beta');
      rerender({ threadId: THREAD, isLoading: false });
      await waitFor(() => expect(result.current.sendModel).toBeNull());
    });

    // DALTON-HOOK(agent-switch-model): keep manualPick when a turn ends while llm_model PATCH is still in flight
    it('keeps manualPick at turn end while an llm_model PATCH is in flight, then clears on a subsequent idle turn end', async () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-thread', 'model-beta']);
      const patchAnswer = deferred<Thread>();
      mocks.updateThread.mockReturnValue(patchAnswer.promise);
      const { result, rerender, queryClient } = setup({ isLoading: true }, row('model-thread'));
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      act(() => {
        void result.current.pickModel('model-beta');
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, { llm_model: 'model-beta' }));
      expect(result.current.sendModel).toBe('model-beta');

      // Turn 1 ends before the PATCH resolves -> sendModel must still carry model-beta.
      rerender({ threadId: THREAD, isLoading: false });
      expect(result.current.sendModel).toBe('model-beta');

      // PATCH resolves and Turn 2 ends with no PATCH in flight -> manualPick clears.
      await act(async () => {
        patchAnswer.resolve(row('model-beta'));
      });
      rerender({ threadId: THREAD, isLoading: true });
      rerender({ threadId: THREAD, isLoading: false });
      await waitFor(() => expect(result.current.sendModel).toBeNull());
    });
  });

  // DALTON-HOOK(antigravity-series): atomic PATCH + intra-series banner + shared-workspace 403 guard
  describe('Antigravity series and shared workspace picks', () => {
    beforeEach(() => {
      mocks.isDaltonBuild = true;
    });

    const NEON_CATALOG = buildSeriesCatalog([
      {
        series_id: 'gemini-4-neon',
        series_prefix: 'gemini-4-neon-',
        display_name: 'Gemini 4 Neon',
        qualified_display_name: 'Gemini 4 Neon (Antigravity)',
        provider: 'antigravity-oauth',
        default_effort: 'high',
        default_model: 'gemini-4-neon-high-oauth',
        efforts: ['low', 'medium', 'high'],
        variants: {
          low: 'gemini-4-neon-low-oauth',
          medium: 'gemini-4-neon-medium-oauth',
          high: 'gemini-4-neon-high-oauth',
        },
      },
    ]);
    const NEON_RAW_META = {
      'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth' },
    };

    it('sends one atomic PATCH clearing reasoning_effort and avoids reopening the default banner on intra-series effort switch', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.defaultModel = 'gemini-4-neon-high-oauth';
      mocks.updateThread.mockResolvedValueOnce({
        thread_id: THREAD,
        llm_model: 'gemini-4-neon-low-oauth',
        reasoning_effort: null,
      });

      const cached = {
        thread_id: THREAD,
        llm_model: 'gemini-4-neon-medium-oauth',
        reasoning_effort: 'high',
      } as unknown as Thread;
      const { result } = setup({ workspaceId: 'ws-personal' }, cached);

      let firstPick!: Promise<boolean>;
      act(() => {
        firstPick = result.current.pickModel('gemini-4-neon-low-oauth');
      });
      await act(async () => { await firstPick; });

      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        reasoning_effort: null,
      });
      expect(result.current.offer).toBeNull();
    });

    it('does not reopen the default banner when switching variants within the same Antigravity series away from another model', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.defaultModel = 'other-model';
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-medium-oauth'));
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));

      const { result } = setup({ workspaceId: 'ws-personal' }, row('other-model'));
      act(() => {
        void result.current.pickModel('gemini-4-neon-medium-oauth');
      });
      await waitFor(() => expect(result.current.offer).toEqual({
        model: 'gemini-4-neon-medium-oauth',
        defaultModel: 'other-model',
      }));

      act(() => { result.current.dismissOffer(); });
      expect(result.current.offer).toBeNull();

      act(() => {
        void result.current.pickModel('gemini-4-neon-low-oauth');
      });
      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-low-oauth'));
      expect(result.current.offer).toBeNull();
    });

    it('never issues PATCH on a shared workspace thread (nor while shared status is loading), updating local sendModel instead', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };
      const { result } = setup({ workspaceId: 'ws-shared' }, row('gemini-4-neon-medium-oauth'));

      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });

      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      mocks.sharedEntry = undefined;
      act(() => {
        void result.current.pickModel('gemini-4-neon-low-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.sendModel).toBe('gemini-4-neon-low-oauth');
    });

    // DALTON-HOOK(antigravity-series): preserve loading-time pick and flush via PATCH once confirmed personal
    it('preserves a pick made while shared workspace list is loading, flushes PATCH once confirmed personal, and uses it on the next message', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = undefined;
      mocks.updateThread.mockResolvedValueOnce({
        thread_id: THREAD,
        llm_model: 'gemini-4-neon-high-oauth',
        reasoning_effort: null,
      });

      const cached = {
        thread_id: THREAD,
        llm_model: 'gemini-4-neon-medium-oauth',
        reasoning_effort: 'high',
      } as unknown as Thread;
      const { result, rerender, queryClient } = setup({ workspaceId: 'ws-personal' }, cached);

      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });

      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // Shared workspace list finishes loading and confirms personal workspace (null)
      mocks.sharedEntry = null;
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-high-oauth',
        reasoning_effort: null,
      });
      expect(cachedModel(queryClient)).toBe('gemini-4-neon-high-oauth');
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): keep loading-time pick on send-with-message path once confirmed shared
    it('keeps a loading-time pick on the send-with-message path without PATCH once confirmed shared workspace', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = undefined;

      const { result, rerender } = setup(
        { workspaceId: 'ws-shared' },
        row('gemini-4-neon-medium-oauth'),
      );

      act(() => {
        void result.current.pickModel('gemini-4-neon-low-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });

      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-low-oauth');

      // DALTON-HOOK(antigravity-series): turn end on confirmed shared workspace keeps sendModel on the local pick
      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.sendModel).toBe('gemini-4-neon-low-oauth');
    });

    // DALTON-HOOK(antigravity-series): retired old thread model must not clear a valid pick made while shared list loads
    it('preserves a valid loading-time pick on a thread with a retired saved model, then flushes PATCH and sends it once confirmed personal', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = undefined;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));

      const { result, rerender, queryClient } = setup(
        { workspaceId: 'ws-personal' },
        row('model-retired'),
      );
      expect(result.current.retired).toEqual({ model: 'model-retired', fallback: 'model-default' });

      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // DALTON-HOOK(antigravity-series): confirmed personal workspace flushes PATCH and preserves sendModel
      mocks.sharedEntry = null;
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-high-oauth',
      });
      expect(cachedModel(queryClient)).toBe('gemini-4-neon-high-oauth');
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): late thread detail arrival must not overwrite a pending loading-time pick before shared list resolves
    it('keeps a loading-time pick as displayed and sent model when thread detail arrives before shared workspace list resolves', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = undefined;
      const detail = deferred<Thread>();
      mocks.getThread.mockReturnValue(detail.promise);
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      const queryClient = createTestQueryClient();
      const { result, rerender } = renderHookWithProviders(
        (p: Props = { threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' }) => useThreadModel(p),
        { queryClient },
      );
      expect(result.current.model).toBeNull();
      expect(result.current.sendModel).toBeNull();

      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // DALTON-HOOK(antigravity-series): thread detail resolves with old model before shared list returns
      await act(async () => {
        detail.resolve(row('gemini-4-neon-medium-oauth'));
        await detail.promise;
      });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('gemini-4-neon-medium-oauth'));

      // Sending before shared list resolves still uses the newly picked model
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // DALTON-HOOK(antigravity-series): once shared list resolves to personal, PATCH flushes with the new pick
      mocks.sharedEntry = null;
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-high-oauth',
      });
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): B1 — first-turn detail readback must preserve an unsent pick made before the thread id returned
    it('preserves an unsent pre-id pick on a shared workspace across first-turn detail readback without issuing PATCH', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };
      const queryClient = createTestQueryClient();
      const { result, rerender } = setup(
        { threadId: '__default__', isLoading: false, workspaceId: 'ws-shared' },
        null,
        queryClient,
      );

      // Pick the first-send model before sending, then start turn 1.
      await act(async () => { await result.current.pickModel('gemini-4-neon-medium-oauth'); });
      rerender({ threadId: '__default__', isLoading: true, workspaceId: 'ws-shared' });

      // Pick another model before the new thread id arrives.
      await act(async () => { await result.current.pickModel('gemini-4-neon-high-oauth'); });
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // Thread id returns mid-turn while the server row still holds the first-send model.
      mocks.server = row('gemini-4-neon-medium-oauth');
      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');

      // First turn ends and detail readback returns the first-send model -> unsent pick must survive without PATCH.
      const turnEndReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(turnEndReread.promise);
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });

      await act(async () => {
        turnEndReread.resolve(row('gemini-4-neon-medium-oauth'));
        await turnEndReread.promise;
      });
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));

      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): B2 — once thread detail confirms a shared local pick, stop overriding later detail changes from another tab
    it('stops overriding a shared thread after detail confirms the local pick so another tab update takes effect', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };

      const { result, rerender, queryClient } = setup(
        { workspaceId: 'ws-shared' },
        row('gemini-4-neon-medium-oauth'),
      );

      // Tab A picks High locally (no PATCH on shared workspace).
      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // Tab A sends a turn and the turn-end detail readback confirms High on the server.
      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });
      mocks.server = row('gemini-4-neon-high-oauth');
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('gemini-4-neon-high-oauth'));
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBeNull();

      // Later, Tab B sends with Low and Tab A's thread detail updates to Low -> Tab A follows Low.
      act(() => {
        queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('gemini-4-neon-low-oauth'));
      });
      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-low-oauth'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): B3 — first shared-list read failure must not be treated as confirmed personal (no PATCH, keep pending pick)
    it('does not treat first shared-list read failure as personal, skips PATCH, and preserves the pending pick', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      // Shared workspace list is loading on first read (no cached data).
      mocks.sharedEntry = undefined;
      mocks.sharedError = false;

      const { result, rerender, queryClient } = setup(
        { workspaceId: 'ws-shared' },
        row('gemini-4-neon-medium-oauth'),
      );

      // User picks High while the shared workspace list is loading.
      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // Shared workspace list request fails with no cache -> must NOT flush a 403 PATCH and must keep the pick.
      mocks.sharedEntry = undefined;
      mocks.sharedError = true;
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      await act(async () => {});

      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // Even if detail refreshes with the old model while still errored, the unsent pick stays intact.
      act(() => {
        queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('gemini-4-neon-medium-oauth'));
      });
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): B2 follow-up — clearing both deferredSharedPick and manualPick when detail confirms a pick while shared list is still loading
    it('clears deferredSharedPick and manualPick when detail confirms a pick while shared list is still loading so another tab update takes effect', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      // Shared workspace list is still loading (not errored).
      mocks.sharedEntry = undefined;
      mocks.sharedError = false;

      const { result, rerender, queryClient } = setup(
        { workspaceId: 'ws-shared' },
        row('gemini-4-neon-medium-oauth'),
      );

      // Tab A picks High before the shared list resolves.
      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // Tab A sends High and detail confirms High while the shared list is still loading.
      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });
      mocks.server = row('gemini-4-neon-high-oauth');
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      await waitFor(() => expect(cachedModel(queryClient)).toBe('gemini-4-neon-high-oauth'));
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBeNull();

      // Tab B later saves Low; detail updates to Low and Tab A follows it.
      act(() => {
        queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('gemini-4-neon-low-oauth'));
      });
      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-low-oauth'));
      expect(result.current.sendModel).toBeNull();

      // DALTON-HOOK(antigravity-series): shared list finally confirms shared — old High pick must stay cleared and not overwrite Low
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      await act(async () => {});

      expect(result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): Carbon #1 — treat thread.shared as confirmed shared immediately even when features/account failed
    it('treats thread.shared as confirmed shared without PATCH even when features read failed and sharedWorkspaces.enabled is false', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEnabled = false;
      mocks.featuresMode = 'error';

      const sharedThread = {
        ...row('gemini-4-neon-medium-oauth'),
        shared: { started_by: { user_id: 'u-1', name: 'Alice' } },
      } as unknown as Thread;

      const { result } = setup({ workspaceId: 'ws-shared' }, sharedThread);

      await act(async () => {
        await result.current.pickModel('gemini-4-neon-high-oauth');
      });

      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): Carbon #1 — features or /users/me loading/failure must not treat workspaceId threads as confirmed personal
    it('defers PATCH without losing pending pick when /features fails or /users/me fails/loads while shared_workspaces is enabled', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);

      // Case A: /api/v1/features first-read failed -> sharedWorkspaces.enabled is false, must NOT PATCH.
      mocks.sharedEnabled = false;
      mocks.featuresMode = 'error';
      const failedFeatures = setup({ workspaceId: 'ws-shared' }, row('gemini-4-neon-medium-oauth'));
      await waitFor(() => expect(failedFeatures.queryClient.isFetching()).toBe(0));

      act(() => {
        void failedFeatures.result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(failedFeatures.result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(failedFeatures.result.current.sendModel).toBe('gemini-4-neon-high-oauth');
      failedFeatures.unmount();

      // DALTON-HOOK(antigravity-series): Case B — /features returned shared_workspaces:true, /users/me failed -> must NOT PATCH, keeps pick
      mocks.sharedEnabled = false;
      mocks.featuresMode = 'resolved';
      mocks.featuresData = [{ key: 'shared_workspaces', enabled: true }];
      mocks.currentUser = null;
      mocks.userError = true;

      const failedUser = setup({ workspaceId: 'ws-shared' }, row('gemini-4-neon-medium-oauth'));
      await waitFor(() => expect(failedUser.queryClient.getQueryData(queryKeys.features.list())).toBeDefined());

      act(() => {
        void failedUser.result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(failedUser.result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(failedUser.result.current.sendModel).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): Carbon #2 — unsent pick matching stale cached thread.llm_model must NOT clear manualPick/sendModel before sending
    it('keeps sendModel set when a user on a shared thread picks back to the stale cached model before sending', () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };

      const { result } = setup({ workspaceId: 'ws-shared' }, row('gemini-4-neon-medium-oauth'));

      // User picks Low, then changes back to Medium before sending (server may already be on High from Tab B).
      act(() => {
        void result.current.pickModel('gemini-4-neon-low-oauth');
      });
      expect(result.current.sendModel).toBe('gemini-4-neon-low-oauth');

      act(() => {
        void result.current.pickModel('gemini-4-neon-medium-oauth');
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.model).toBe('gemini-4-neon-medium-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-medium-oauth');
    });

    // DALTON-HOOK(antigravity-series): Carbon #3 — consumed shared pick releases on turn-end reread even when fresh.llm_model differs, while preserving any newer mid-turn pick
    it('releases a consumed shared pick when turn-end reread returns another model, and preserves a newer mid-turn pick', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };

      const { result, rerender } = setup({ workspaceId: 'ws-shared' }, row('gemini-4-neon-medium-oauth'));

      // Tab A picks High and sends.
      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });

      // Before Tab A's turn-end reread lands, Tab B starts a turn with Low (or Tab A's turn failed before saving High).
      const reread1 = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(reread1.promise);
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });

      await act(async () => {
        reread1.resolve(row('gemini-4-neon-low-oauth'));
        await reread1.promise;
      });

      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-low-oauth'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // DALTON-HOOK(antigravity-series): if the user picks a newer model mid-turn, Turn 2 ending must NOT clear that newer unsent pick
      act(() => {
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });
      act(() => {
        void result.current.pickModel('gemini-4-neon-medium-oauth');
      });

      const reread2 = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(reread2.promise);
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });

      await act(async () => {
        reread2.resolve(row('gemini-4-neon-high-oauth'));
        await reread2.promise;
      });

      expect(result.current.model).toBe('gemini-4-neon-medium-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-medium-oauth');
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): non-Dalton builds must keep exact origin/dalton/main behavior — shared threads skip PATCH, personal threads PATCH immediately even if /features fails or isDaltonAuth is set
    it('keeps origin/dalton/main behavior on non-Dalton builds: skips PATCH on shared threads and PATCHes personal threads immediately', async () => {
      mocks.isDaltonBuild = false;
      mocks.catalogModelNames = new Set(['model-default', 'model-thread', 'model-beta']);

      // 1) Shared workspaces enabled + confirmed shared thread -> no PATCH, local sendModel kept.
      mocks.sharedEnabled = true;
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };
      const sharedView = setup({ workspaceId: 'ws-shared' }, row('model-thread'));

      await act(async () => {
        expect(await sharedView.result.current.pickModel('model-beta')).toBe(true);
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(sharedView.result.current.model).toBe('model-beta');
      expect(sharedView.result.current.sendModel).toBe('model-beta');
      sharedView.unmount();

      // 2) Personal thread with /features failure -> immediate PATCH (not deferred).
      mocks.sharedEnabled = false;
      mocks.sharedEntry = null;
      mocks.featuresMode = 'error';
      mocks.updateThread.mockResolvedValueOnce(row('model-beta'));

      const personalView = setup({ workspaceId: 'ws-personal' }, row('model-thread'));
      await act(async () => {
        expect(await personalView.result.current.pickModel('model-beta')).toBe(true);
      });
      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, { llm_model: 'model-beta' });
      expect(cachedModel(personalView.queryClient)).toBe('model-beta');
      expect(personalView.result.current.model).toBe('model-beta');
      personalView.unmount();

      // DALTON-HOOK(antigravity-series): 3) LangAlpha brand with Dalton auth enabled before thread detail arrives -> still PATCHes immediately (Dalton detail-wait is gated by isDaltonBuild)
      mocks.isDaltonAuth = true;
      mocks.sharedEnabled = false;
      mocks.featuresMode = 'default';
      mocks.getThread.mockReturnValue(new Promise(() => {}));
      mocks.updateThread.mockResolvedValueOnce(row('model-beta'));

      const authPersonalView = setup({ workspaceId: 'ws-personal' }, null);
      await act(async () => {
        expect(await authPersonalView.result.current.pickModel('model-beta')).toBe(true);
      });
      expect(mocks.updateThread).toHaveBeenCalledTimes(2);
      expect(mocks.updateThread).toHaveBeenLastCalledWith(THREAD, { llm_model: 'model-beta' });
    });
  });
});
