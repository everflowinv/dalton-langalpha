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
import { useThreadTuning } from '../useThreadTuning';

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

// DALTON-HOOK(antigravity-series): mock getFeatures and useUser for Dalton permission-resolution tests
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
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'model-beta',
        expected_llm_model: 'model-alpha',
      }));
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

      // DALTON-HOOK(model-picker-races): deferred flush carries expected_llm_model and expected_reasoning_effort CAS guards
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-high-oauth',
        reasoning_effort: null,
        expected_llm_model: 'gemini-4-neon-medium-oauth',
        expected_reasoning_effort: 'high',
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

      // DALTON-HOOK(model-picker-races): deferred Antigravity flush carries expected_llm_model and explicit expected_reasoning_effort: null CAS guards
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(1));
      expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-high-oauth',
        expected_llm_model: 'model-retired',
        expected_reasoning_effort: null,
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

      // DALTON-HOOK(model-picker-races): Codex-H2 — because thread detail was unread when the deferred pick was staged, cancel automatic flush when permissions resolve instead of unconditionally overwriting the arrived server model
      mocks.sharedEntry = null;
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-medium-oauth'));
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(result.current.sendModel).toBeNull();
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

    // DALTON-HOOK(antigravity-series): Item 1 — pre-id pick on first turn survives detail readback and keeps sendModel until saved on personal & unresolved workspaces
    it('preserves a pre-id pick across first-turn detail readback on personal and unresolved workspaces without dropping sendModel before save settles', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;

      const patchDeferred = deferred<Thread>();
      mocks.updateThread.mockReturnValueOnce(patchDeferred.promise);
      const qcPersonal = createTestQueryClient();
      const personal = setup(
        { threadId: '__default__', isLoading: false, workspaceId: 'ws-personal' },
        null,
        qcPersonal,
      );

      await act(async () => { await personal.result.current.pickModel('gemini-4-neon-medium-oauth'); });
      personal.rerender({ threadId: '__default__', isLoading: true, workspaceId: 'ws-personal' });
      // User changes choice before thread ID arrives.
      await act(async () => { await personal.result.current.pickModel('gemini-4-neon-high-oauth'); });
      expect(personal.result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(personal.result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // Thread ID arrives with first-send model + saved effort; turn ends while reread is in flight.
      const firstRow = {
        thread_id: THREAD,
        llm_model: 'gemini-4-neon-medium-oauth',
        reasoning_effort: 'high',
      } as unknown as Thread;
      mocks.server = firstRow;
      personal.rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-personal' });
      await waitFor(() => expect(qcPersonal.isFetching()).toBe(0));

      const turnEndReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(turnEndReread.promise);
      personal.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      // DALTON-HOOK(antigravity-series): sendModel must NOT be cleared at turn end while pre-id pick awaits reread & PATCH
      expect(personal.result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(personal.result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      await act(async () => {
        turnEndReread.resolve(firstRow);
        await turnEndReread.promise;
      });
      // DALTON-HOOK(model-picker-races): first-turn pre-id background repatch carries CAS conditions
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-high-oauth',
        reasoning_effort: null,
        expected_llm_model: 'gemini-4-neon-medium-oauth',
        expected_reasoning_effort: 'high',
      }));
      await act(async () => {
        patchDeferred.resolve({ thread_id: THREAD, llm_model: 'gemini-4-neon-high-oauth', reasoning_effort: null } as unknown as Thread);
      });
      expect(personal.result.current.model).toBe('gemini-4-neon-high-oauth');
      personal.unmount();

      // DALTON-HOOK(antigravity-series): unresolved workspace preserves pre-id pick across reread without PATCH, then flushes once confirmed personal
      mocks.sharedEntry = undefined;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      const qcUnresolved = createTestQueryClient();
      const unresolved = setup(
        { threadId: '__default__', isLoading: false, workspaceId: 'ws-personal' },
        null,
        qcUnresolved,
      );
      await act(async () => { await unresolved.result.current.pickModel('gemini-4-neon-medium-oauth'); });
      unresolved.rerender({ threadId: '__default__', isLoading: true, workspaceId: 'ws-personal' });
      await act(async () => { await unresolved.result.current.pickModel('gemini-4-neon-high-oauth'); });
      mocks.server = row('gemini-4-neon-medium-oauth');
      unresolved.rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-personal' });
      await waitFor(() => expect(qcUnresolved.isFetching()).toBe(0));

      const unresolvedReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(unresolvedReread.promise);
      unresolved.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      await act(async () => {
        unresolvedReread.resolve(row('gemini-4-neon-medium-oauth'));
        await unresolvedReread.promise;
      });
      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
      expect(unresolved.result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(unresolved.result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      mocks.sharedEntry = null;
      unresolved.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(2));
      expect(unresolved.result.current.model).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): Item 2 — after an applied local pick on a shared thread is confirmed (even via structural sharing / same cached value), Tab B updates must not be overridden
    it('yields to another tab detail update on a shared thread after the applied local pick is sent and confirmed via dataUpdatedAt', async () => {
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
        row('gemini-4-neon-high-oauth'),
      );

      // Tab A explicitly switches Low -> High (matching cached High), then sends.
      act(() => {
        void result.current.pickModel('gemini-4-neon-low-oauth');
        void result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });
      mocks.server = row('gemini-4-neon-high-oauth');
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      await waitFor(() => expect(result.current.sendModel).toBeNull());

      // Tab B later sends with Low -> Tab A must follow Low instead of pinning High.
      act(() => {
        queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('gemini-4-neon-low-oauth'));
      });
      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-low-oauth'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): Item 3 — shared-list first-read failure defers PATCH until retry succeeds, then skips PATCH (shared) or flushes PATCH (personal)
    it('defers PATCH on first shared-list read failure and decides only after retry succeeds (shared skips, personal flushes)', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);

      // Case 1: first read fails, retry confirms shared -> zero PATCHes, local pick kept.
      mocks.sharedEntry = undefined;
      mocks.sharedError = true;
      const sharedView = setup({ workspaceId: 'ws-shared' }, row('gemini-4-neon-medium-oauth'));
      act(() => { void sharedView.result.current.pickModel('gemini-4-neon-high-oauth'); });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };
      mocks.sharedError = false;
      sharedView.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      await act(async () => {});
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(sharedView.result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(sharedView.result.current.sendModel).toBe('gemini-4-neon-high-oauth');
      sharedView.unmount();

      // DALTON-HOOK(antigravity-series): Case 2 — first read fails, retry confirms personal -> flushes deferred PATCH
      mocks.sharedEntry = undefined;
      mocks.sharedError = true;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      const personalView = setup({ workspaceId: 'ws-personal' }, row('gemini-4-neon-medium-oauth'));
      act(() => { void personalView.result.current.pickModel('gemini-4-neon-high-oauth'); });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      mocks.sharedEntry = null;
      mocks.sharedError = false;
      personalView.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      // DALTON-HOOK(model-picker-races): deferred Antigravity flush carries expected_llm_model and explicit expected_reasoning_effort: null CAS guards
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-high-oauth',
        expected_llm_model: 'gemini-4-neon-medium-oauth',
        expected_reasoning_effort: null,
      }));
      expect(personalView.result.current.model).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): Item 4 — a queued message sent with High must NOT clear an unsent Low picked before the queued turn started (shared & personal)
    it('does not clear an unsent Low pick when a queued message sent with High finishes its turn, clearing Low only after Low is sent', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);

      // Shared thread: user picks High (queued), then picks Low before queued High starts.
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };
      const shared = setup({ workspaceId: 'ws-shared' }, row('gemini-4-neon-medium-oauth'));
      act(() => { void shared.result.current.pickModel('gemini-4-neon-high-oauth'); });
      const highSeq = shared.result.current.sendModelSeq;
      expect(highSeq).not.toBeNull();

      act(() => { void shared.result.current.pickModel('gemini-4-neon-low-oauth'); });
      const lowSeq = shared.result.current.sendModelSeq;
      expect(lowSeq).not.toBe(highSeq);
      expect(shared.result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(shared.result.current.sendModel).toBe('gemini-4-neon-low-oauth');

      // Queued message starts with High (highSeq) and ends with server row updated to High.
      shared.rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-shared',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq },
      });
      const highReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(highReread.promise);
      shared.rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-shared',
        activeTurnModel: null,
      });
      await act(async () => {
        highReread.resolve(row('gemini-4-neon-high-oauth'));
        await highReread.promise;
      });
      // DALTON-HOOK(antigravity-series): unsent Low survives High turn-end reread
      expect(shared.result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(shared.result.current.sendModel).toBe('gemini-4-neon-low-oauth');

      // Next turn actually sends Low (lowSeq) -> turn end clears Low.
      shared.rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-shared',
        activeTurnModel: { model: 'gemini-4-neon-low-oauth', pickSeq: lowSeq },
      });
      const lowReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(lowReread.promise);
      shared.rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-shared',
        activeTurnModel: null,
      });
      await act(async () => {
        lowReread.resolve(row('gemini-4-neon-low-oauth'));
        await lowReread.promise;
      });
      await waitFor(() => expect(shared.result.current.sendModel).toBeNull());
      expect(shared.result.current.model).toBe('gemini-4-neon-low-oauth');
      shared.unmount();

      // DALTON-HOOK(antigravity-series): Round 2 Item 3 — personal thread: saved Low survives queued High turn, re-PATCHes Low to the row, and never jumps back to High when sending the next message with Low
      mocks.sharedEntry = null;
      const repatchAnswer = deferred<Thread>();
      mocks.updateThread
        .mockResolvedValueOnce(row('gemini-4-neon-high-oauth'))
        .mockResolvedValueOnce(row('gemini-4-neon-low-oauth'))
        .mockReturnValueOnce(repatchAnswer.promise);
      const personal = setup({ workspaceId: 'ws-personal' }, row('gemini-4-neon-medium-oauth'));
      await act(async () => { await personal.result.current.pickModel('gemini-4-neon-high-oauth'); });
      const personalHighSeq = personal.result.current.sendModelSeq;
      await act(async () => { await personal.result.current.pickModel('gemini-4-neon-low-oauth'); });
      const personalLowSeq = personal.result.current.sendModelSeq;
      expect(personal.result.current.sendModel).toBe('gemini-4-neon-low-oauth');

      // Queued message 1 starts and is admitted with High -> server row moves to High.
      personal.rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: personalHighSeq, admitted: true },
      });
      const personalReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(personalReread.promise);
      personal.rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });
      await act(async () => {
        personalReread.resolve(row('gemini-4-neon-high-oauth'));
        await personalReread.promise;
      });
      // turnEndCheck re-persists Low onto the personal thread row and keeps UI & sendModel on Low.
      // DALTON-HOOK(model-picker-races): background Antigravity repatch carries expected_llm_model and explicit expected_reasoning_effort: null CAS guards
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledTimes(3));
      expect(mocks.updateThread).toHaveBeenLastCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        expected_llm_model: 'gemini-4-neon-high-oauth',
        expected_reasoning_effort: null,
      });
      expect(personal.result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(personal.result.current.sendModel).toBe('gemini-4-neon-low-oauth');

      // Now simulate user sending message 2 with Low even if cached row temporarily still reads High:
      // starting message 2 (unadmitted, then refused before admission) must NEVER jump back to High!
      act(() => {
        personal.queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('gemini-4-neon-high-oauth'));
      });
      personal.rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-low-oauth', pickSeq: personalLowSeq, admitted: false },
      });
      expect(personal.result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(personal.result.current.sendModel).toBe('gemini-4-neon-low-oauth');

      // Repatch settles, then message 2 is admitted with Low and finishes -> UI stays Low and sendModel clears.
      await act(async () => {
        repatchAnswer.resolve(row('gemini-4-neon-low-oauth'));
        await repatchAnswer.promise;
      });
      personal.rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-low-oauth', pickSeq: personalLowSeq, admitted: true },
      });
      const lowTurnReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(lowTurnReread.promise);
      personal.rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });
      await act(async () => {
        lowTurnReread.resolve(row('gemini-4-neon-low-oauth'));
        await lowTurnReread.promise;
      });
      await waitFor(() => expect(personal.result.current.sendModel).toBeNull());
      expect(personal.result.current.model).toBe('gemini-4-neon-low-oauth');
    });

    // DALTON-HOOK(antigravity-series): Round 2 Item 1 — a send refused before server run admission (e.g. 402 budget check) must NOT clear a shared thread manual pick
    it('keeps a shared thread model pick unsent when a send is refused before server admission, clearing it only after a later admitted turn', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = { workspace: { workspace_id: 'ws-shared' } };

      const { result, rerender } = setup(
        { workspaceId: 'ws-shared' },
        row('gemini-4-neon-medium-oauth'),
      );

      act(() => { void result.current.pickModel('gemini-4-neon-high-oauth'); });
      const highSeq = result.current.sendModelSeq;
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      // Send starts (admitted: false) and is refused before server starts a run (e.g. budget 402).
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-shared',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq, admitted: false },
      });
      const refusedReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(refusedReread.promise);
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-shared',
        activeTurnModel: null,
      });
      await act(async () => {
        refusedReread.resolve(row('gemini-4-neon-medium-oauth'));
        await refusedReread.promise;
      });

      // High MUST remain selected & queued for the user's resend (not reverted to server Medium).
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModelSeq).toBe(highSeq);

      // User resends the restored draft; this time server admits the run (admitted: true).
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-shared',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq, admitted: false },
      });
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-shared',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq, admitted: true },
      });
      const admittedReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(admittedReread.promise);
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-shared',
        activeTurnModel: null,
      });
      await act(async () => {
        admittedReread.resolve(row('gemini-4-neon-high-oauth'));
        await admittedReread.promise;
      });
      await waitFor(() => expect(result.current.sendModel).toBeNull());
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
    });

    // DALTON-HOOK(antigravity-series): Item 5 — if the first turn-end detail read fails and a later read returns another tab's Low, the already-sent High must unpin and follow Low
    it('unpins a sent High pick when the first turn-end detail read fails and a subsequent detail read returns another tab Low', async () => {
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

      // Tab A picks High and sends a turn.
      act(() => { void result.current.pickModel('gemini-4-neon-high-oauth'); });
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-high-oauth');

      rerender({ threadId: THREAD, isLoading: true, workspaceId: 'ws-shared' });

      // Turn ends; FIRST detail read FAILS -> High stays held while no fresh read has succeeded yet.
      const failedReread = deferred<Thread>();
      mocks.getThread.mockReturnValueOnce(failedReread.promise);
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-shared' });
      await act(async () => {
        failedReread.reject(failure());
        await failedReread.promise.catch(() => {});
      });
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');

      // DALTON-HOOK(antigravity-series): SECOND detail read succeeds with Tab B's Low -> High unpins and composer follows Low
      mocks.getThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));
      await act(async () => {
        await queryClient.refetchQueries({ queryKey: queryKeys.threads.detail(THREAD) });
      });
      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-low-oauth'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): Q2a — a turn starting without an explicit model override consumes an already-saved personal High pick instead of treating it as unsent and repatching over a server-side switch
    it('treats a personal saved High pick as consumed when a turn starts without an explicit model override', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));

      const { result, rerender } = setup(
        { workspaceId: 'ws-personal', activeTurnModel: null },
        row('gemini-4-neon-medium-oauth'),
      );

      // User picks High on personal thread -> PATCH succeeds and saves High to the row.
      await act(async () => {
        await result.current.pickModel('gemini-4-neon-high-oauth');
      });
      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
      expect(result.current.model).toBe('gemini-4-neon-high-oauth');

      // A widget prompt turn starts without an explicit model override (`model: null`).
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: null, pickSeq: null, admitted: true },
      });

      // Turn ends and the server row has moved to Low (e.g. agent switched model).
      mocks.getThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-low-oauth'));
      // Must NOT repatch High back over Low.
      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
      expect(result.current.sendModel).toBeNull();
    });

    // DALTON-HOOK(antigravity-series): Q2b — real useThreadModel + useThreadTuning integration: pre-id Antigravity pick during first-turn creation must not standalone-PATCH effort=null and must restore effort when the deferred model PATCH fails
    it('restores just-picked effort and prevents premature standalone effort PATCH across null->id creation when a pre-id Antigravity deferred PATCH fails (real model + tuning hooks)', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'model-a',
        'gemini-4-neon-medium-oauth',
      ]);
      mocks.sharedEntry = null;

      const queryClient = createTestQueryClient();
      type CombinedProps = {
        threadId: string;
        isLoading: boolean;
        initialModel: string | null;
        activeTurnModel: { model: string | null; pickSeq?: number | null; admitted?: boolean } | null;
      };
      const initial: CombinedProps = {
        threadId: '__default__',
        isLoading: false,
        initialModel: 'model-a',
        activeTurnModel: null,
      };
      const { result, rerender } = renderHookWithProviders(
        (p: CombinedProps = initial) => ({
          model: useThreadModel({
            threadId: p.threadId,
            isLoading: p.isLoading,
            initialModel: p.initialModel,
            workspaceId: 'ws-personal',
            activeTurnModel: p.activeTurnModel,
          }),
          tuning: useThreadTuning({
            threadId: p.threadId,
            workspaceId: 'ws-personal',
          }),
        }),
        { queryClient },
      );

      // 1) User picks effort='high' before the first send.
      await act(async () => {
        await result.current.tuning.setReasoningEffort('high');
      });
      expect(result.current.tuning.tuning.reasoningEffort).toBe('high');
      expect(result.current.tuning.toSend.reasoningEffortOverride).toBe('high');

      // 2) First turn starts before thread id returns (sent with model-a & reasoningEffortOverride='high').
      rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'model-a',
        activeTurnModel: { model: 'model-a', pickSeq: null, admitted: false },
      });

      // 3) While threadId is still null, user switches to Antigravity (wired just like ChatView/MarketChatPanel).
      await act(async () => {
        const effortClear = result.current.tuning.clearLocalEffort();
        await result.current.model.pickModel('gemini-4-neon-medium-oauth', {
          onSettled: (saved) => {
            if (saved) effortClear.confirm();
            else effortClear();
          },
        });
      });
      expect(result.current.model.model).toBe('gemini-4-neon-medium-oauth');
      expect(result.current.tuning.tuning.reasoningEffort).toBeNull();

      // 4) Thread id arrives mid-turn (null -> THREAD); server created the row with model-a & reasoning_effort: 'high'.
      const createdRow = { ...row('model-a'), reasoning_effort: 'high', fast_mode: null } as Thread;
      mocks.getThread.mockResolvedValue(createdRow);
      rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'model-a',
        activeTurnModel: { model: 'model-a', pickSeq: null, admitted: true },
      });
      await waitFor(() => expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(createdRow));
      // Tuning hook must NOT fire a standalone PATCH { reasoning_effort: null } for the temporary clear!
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // 5) Turn ends; reread returns model-a + reasoning_effort: 'high'; deferred model PATCH fails.
      mocks.updateThread.mockRejectedValueOnce(failure());
      rerender({
        threadId: THREAD,
        isLoading: false,
        initialModel: 'model-a',
        activeTurnModel: null,
      });

      // DALTON-HOOK(model-picker-races): first-turn pre-id background repatch carries expected_llm_model & expected_reasoning_effort
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(
        THREAD,
        {
          llm_model: 'gemini-4-neon-medium-oauth',
          reasoning_effort: null,
          expected_llm_model: 'model-a',
          expected_reasoning_effort: 'high',
        },
      ));
      await waitFor(() => expect(result.current.model.model).toBe('model-a'));
      // Clear handle followed null->THREAD and restored local effort to 'high'; server detail still holds 'high'.
      expect(result.current.tuning.tuning.reasoningEffort).toBe('high');
      expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))?.reasoning_effort).toBe('high');
      expect(mocks.updateThread).toHaveBeenCalledTimes(1);
    });

    // DALTON-HOOK(antigravity-series): Q2b early exits — empty detail reread, thrown detail reread, and reload during reread must all call onSettled(false) and restore local effort
    it.each([
      ['empty detail reread', 'empty'],
      ['rejected detail reread', 'reject'],
      ['reloading while reread is in flight', 'reloading'],
    ] as const)('calls onSettled(false) and restores local effort on %s', async (_label, mode) => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set(['model-default', 'model-a', 'gemini-4-neon-medium-oauth']);
      mocks.sharedEntry = null;

      const queryClient = createTestQueryClient();
      type P = { threadId: string; isLoading: boolean; activeTurnModel: { model: string | null; admitted?: boolean } | null };
      const init: P = { threadId: '__default__', isLoading: false, activeTurnModel: null };
      const { result, rerender } = renderHookWithProviders(
        (p: P = init) => ({
          model: useThreadModel({
            threadId: p.threadId,
            isLoading: p.isLoading,
            initialModel: 'model-a',
            workspaceId: 'ws-personal',
            activeTurnModel: p.activeTurnModel,
          }),
          tuning: useThreadTuning({ threadId: p.threadId, workspaceId: 'ws-personal' }),
        }),
        { queryClient },
      );

      await act(async () => { await result.current.tuning.setReasoningEffort('high'); });
      rerender({ threadId: '__default__', isLoading: true, activeTurnModel: { model: 'model-a', admitted: false } });

      const onSettled = vi.fn();
      await act(async () => {
        const effortClear = result.current.tuning.clearLocalEffort();
        await result.current.model.pickModel('gemini-4-neon-medium-oauth', {
          onSettled: (saved) => {
            onSettled(saved);
            if (saved) effortClear.confirm();
            else effortClear();
          },
        });
      });
      expect(result.current.tuning.tuning.reasoningEffort).toBeNull();

      // Thread id arrives; initial detail fetch succeeds with reasoning_effort='high' (or stays pending for reloading test).
      const initialRow = { ...row('model-a'), reasoning_effort: 'high', fast_mode: null } as Thread;
      mocks.getThread.mockResolvedValueOnce(initialRow);
      rerender({ threadId: THREAD, isLoading: true, activeTurnModel: { model: 'model-a', admitted: true } });
      await waitFor(() => expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(initialRow));

      const turnEndReread = deferred<Thread | undefined>();
      mocks.getThread.mockReturnValueOnce(turnEndReread.promise);
      rerender({ threadId: THREAD, isLoading: false, activeTurnModel: null });

      if (mode === 'empty') {
        await act(async () => {
          turnEndReread.resolve(undefined);
          await turnEndReread.promise;
        });
      } else if (mode === 'reject') {
        await act(async () => {
          turnEndReread.reject(failure());
          await turnEndReread.promise.catch(() => {});
        });
      } else {
        // Reloading / next turn starts before reread resolves.
        rerender({ threadId: THREAD, isLoading: true, activeTurnModel: { model: 'model-a', admitted: false } });
        await act(async () => {
          turnEndReread.resolve(initialRow);
          await turnEndReread.promise;
        });
      }

      await waitFor(() => expect(onSettled).toHaveBeenCalledWith(false));
      expect(onSettled).toHaveBeenCalledTimes(1);
      expect(result.current.tuning.tuning.reasoningEffort).toBe('high');
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): R5b — when a resumed turn starts reading Low (admitted: false) then adopts an old High run via 409 (admitted: true), Low must remain preserved after the High turn ends
    it('preserves a newly picked Low when a turn starts unadmitted on Low and mid-turn adopts an older High duplicate run', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));

      const { result, rerender } = setup(
        { workspaceId: 'ws-personal', activeTurnModel: null },
        row('gemini-4-neon-medium-oauth'),
      );

      // Submission 1 picked High (seq 1).
      await act(async () => { await result.current.pickModel('gemini-4-neon-high-oauth'); });
      const highSeq = result.current.sendModelSeq!;
      // Before submission 2, user switches to Low (seq 2).
      await act(async () => { await result.current.pickModel('gemini-4-neon-low-oauth'); });
      const lowSeq = result.current.sendModelSeq!;
      expect(lowSeq).toBeGreaterThan(highSeq);

      // Submission 2 starts unadmitted with Low (seq 2).
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-low-oauth', pickSeq: lowSeq, admitted: false },
      });
      // Mid-turn, 409 duplicate_request adopts submission 1's High (seq 1).
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq, admitted: true },
      });

      // Adopted High turn ends; server row reports High (written by the old High turn).
      mocks.getThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      // DALTON-HOOK(model-picker-races): background repatch carries expected_llm_model CAS guard
      await waitFor(() => expect(mocks.updateThread).toHaveBeenLastCalledWith(
        THREAD,
        {
          llm_model: 'gemini-4-neon-low-oauth',
          expected_llm_model: 'gemini-4-neon-high-oauth',
          expected_reasoning_effort: null,
        },
      ));
      expect(result.current.model).toBe('gemini-4-neon-low-oauth');
    });

    // DALTON-HOOK(antigravity-series): R5 — when 409 adopts an earlier run whose model is unknown, Low stays unconsumed mid-turn;
    // DALTON-HOOK(model-picker-races): Codex-M1 — because the adopted turn's written model is unknown, if reread returns High (e.g. Tab B switched back), do NOT repatch over Tab B; follow High instead
    it('follows Tab B High without repatching when 409 adopts an earlier run whose model is unknown', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));

      const { result, rerender } = setup(
        { workspaceId: 'ws-personal', activeTurnModel: null },
        row('gemini-4-neon-high-oauth'),
      );

      // User picks & saves Low (seq 1) after submission 1 (which sent model: null on saved High) lost its response.
      await act(async () => { await result.current.pickModel('gemini-4-neon-low-oauth'); });
      const lowSeq = result.current.sendModelSeq!;
      expect(result.current.model).toBe('gemini-4-neon-low-oauth');
      mocks.updateThread.mockClear();

      // Submission 2 starts unadmitted with Low (seq 1), then adopts old run with modelUnknown: true.
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-low-oauth', pickSeq: lowSeq, admitted: false },
      });
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: null, pickSeq: null, admitted: true, modelUnknown: true },
      });
      // Mid-turn, Low is still displayed (not consumed as an implicit saved personal pick).
      expect(result.current.model).toBe('gemini-4-neon-low-oauth');

      // Adopted unknown-model turn finishes; Tab B actively switched back to High, so reread returns High.
      mocks.getThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      await waitFor(() => expect(result.current.model).toBe('gemini-4-neon-high-oauth'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): Q2 — null-origin clearLocal handle binds to the created thread id and stays inactive after switching to another thread (both after creation and directly from null)
    it('binds a pre-id clearLocal handle to the created thread id and ignores switches to other threads (real model + tuning hooks)', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set(['model-default', 'model-a', 'gemini-4-neon-medium-oauth']);
      mocks.sharedEntry = null;

      const OTHER_THREAD = 'thread-other-99';
      const otherRow = { ...row('model-a'), thread_id: OTHER_THREAD, reasoning_effort: 'low', fast_mode: null } as Thread;

      // Subcase A: null -> unrelated existing OTHER_THREAD (createdThreadId: null) must invalidate handle and never PATCH OTHER_THREAD.
      const qcA = createTestQueryClient();
      qcA.setQueryData(queryKeys.threads.detail(OTHER_THREAD), otherRow);
      type P = { threadId: string; isLoading: boolean; createdThreadId: string | null };
      const initA: P = { threadId: '__default__', isLoading: false, createdThreadId: null };
      const viewA = renderHookWithProviders(
        (p: P = initA) => ({
          model: useThreadModel({
            threadId: p.threadId,
            isLoading: p.isLoading,
            initialModel: 'model-a',
            workspaceId: 'ws-personal',
            createdThreadId: p.createdThreadId,
          }),
          tuning: useThreadTuning({
            threadId: p.threadId,
            workspaceId: 'ws-personal',
            createdThreadId: p.createdThreadId,
          }),
        }),
        { queryClient: qcA },
      );

      await act(async () => { await viewA.result.current.tuning.setReasoningEffort('high'); });
      viewA.rerender({ threadId: '__default__', isLoading: true, createdThreadId: null });
      let handleA!: ReturnType<typeof viewA.result.current.tuning.clearLocalEffort>;
      await act(async () => {
        handleA = viewA.result.current.tuning.clearLocalEffort();
        await viewA.result.current.model.pickModel('gemini-4-neon-medium-oauth', {
          onSettled: (saved) => { if (saved) handleA.confirm(); else handleA(); },
        });
      });
      expect(viewA.result.current.tuning.tuning.reasoningEffort).toBeNull();

      // User navigates directly from __default__ to unrelated existing OTHER_THREAD (createdThreadId stays null).
      mocks.getThread.mockResolvedValue(otherRow);
      viewA.rerender({ threadId: OTHER_THREAD, isLoading: false, createdThreadId: null });
      await waitFor(() => expect(viewA.result.current.tuning.tuning.reasoningEffort).toBe('low'));

      // Late restore() / confirm() from the abandoned new-chat handle must be a complete no-op on OTHER_THREAD.
      act(() => {
        handleA();
        handleA.confirm();
      });
      expect(viewA.result.current.tuning.tuning.reasoningEffort).toBe('low');
      expect(mocks.updateThread).not.toHaveBeenCalled();
      viewA.unmount();

      // Subcase B: null -> THREAD (createdThreadId: THREAD) binds handle to THREAD, then switching THREAD -> OTHER_THREAD invalidates it.
      const qcB = createTestQueryClient();
      qcB.setQueryData(queryKeys.threads.detail(OTHER_THREAD), otherRow);
      const createdRow = { ...row('model-a'), reasoning_effort: 'high', fast_mode: null } as Thread;
      const viewB = renderHookWithProviders(
        (p: P = initA) => ({
          model: useThreadModel({
            threadId: p.threadId,
            isLoading: p.isLoading,
            initialModel: 'model-a',
            workspaceId: 'ws-personal',
            createdThreadId: p.createdThreadId,
          }),
          tuning: useThreadTuning({
            threadId: p.threadId,
            workspaceId: 'ws-personal',
            createdThreadId: p.createdThreadId,
          }),
        }),
        { queryClient: qcB },
      );

      await act(async () => { await viewB.result.current.tuning.setReasoningEffort('high'); });
      viewB.rerender({ threadId: '__default__', isLoading: true, createdThreadId: null });
      let handleB!: ReturnType<typeof viewB.result.current.tuning.clearLocalEffort>;
      await act(async () => {
        handleB = viewB.result.current.tuning.clearLocalEffort();
        await viewB.result.current.model.pickModel('gemini-4-neon-medium-oauth', {
          onSettled: (saved) => { if (saved) handleB.confirm(); else handleB(); },
        });
      });

      mocks.getThread.mockResolvedValueOnce(createdRow);
      viewB.rerender({ threadId: THREAD, isLoading: true, createdThreadId: THREAD });
      await waitFor(() => expect(qcB.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(createdRow));
      // Switch away from created THREAD to OTHER_THREAD.
      viewB.rerender({ threadId: OTHER_THREAD, isLoading: false, createdThreadId: null });
      await waitFor(() => expect(viewB.result.current.tuning.tuning.reasoningEffort).toBe('low'));

      act(() => {
        handleB();
        handleB.confirm();
      });
      expect(viewB.result.current.tuning.tuning.reasoningEffort).toBe('low');
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(antigravity-series): Q2 — when deferredSharedPick is dropped because its model left the catalog while permissions are unresolved, settle its own onSettled(false) and restore effort
    it('settles deferredSharedPick onSettled(false) and restores effort when catalog drops the deferred model during permission resolution (real model + tuning hooks)', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set(['model-default', 'model-a', 'gemini-4-neon-medium-oauth']);
      // Simulate unresolved shared-workspace permission check (shared entry still loading).
      mocks.sharedEnabled = true;
      mocks.sharedEntry = undefined;

      const queryClient = createTestQueryClient();
      const existingRow = { ...row('model-a'), reasoning_effort: 'high', fast_mode: null } as Thread;
      queryClient.setQueryData(queryKeys.threads.detail(THREAD), existingRow);
      mocks.getThread.mockResolvedValue(existingRow);

      type P = { threadId: string; isLoading: boolean; tick: number };
      const init: P = { threadId: THREAD, isLoading: false, tick: 0 };
      const { result, rerender } = renderHookWithProviders(
        (p: P = init) => ({
          model: useThreadModel({
            threadId: p.threadId,
            isLoading: p.isLoading,
            workspaceId: 'ws-unresolved',
          }),
          tuning: useThreadTuning({
            threadId: p.threadId,
            workspaceId: 'ws-unresolved',
          }),
        }),
        { queryClient },
      );

      expect(result.current.tuning.tuning.reasoningEffort).toBe('high');
      const onSettled = vi.fn();
      await act(async () => {
        const effortClear = result.current.tuning.clearLocalEffort();
        await result.current.model.pickModel('gemini-4-neon-medium-oauth', {
          onSettled: (saved) => {
            onSettled(saved);
            if (saved) effortClear.confirm();
            else effortClear();
          },
        });
      });
      expect(result.current.model.model).toBe('gemini-4-neon-medium-oauth');
      expect(result.current.tuning.tuning.reasoningEffort).toBeNull();
      expect(onSettled).not.toHaveBeenCalled();

      // Catalog refreshes and removes gemini-4-neon-medium-oauth while permissions are STILL unresolved.
      mocks.catalogModelNames = new Set(['model-default', 'model-a']);
      rerender({ threadId: THREAD, isLoading: false, tick: 1 });

      await waitFor(() => expect(onSettled).toHaveBeenCalledWith(false));
      expect(onSettled).toHaveBeenCalledTimes(1);
      expect(result.current.tuning.tuning.reasoningEffort).toBe('high');
      expect(result.current.model.model).toBe('model-a');
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(model-picker-races): R1-1 — background repatch failing with network/5xx preserves unsent Low pick & sendModel
    it('R1-1: preserves unsent Low pick and sendModel when turn-end background repatch fails with 503', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));

      const { result, rerender } = setup(
        { workspaceId: 'ws-personal', activeTurnModel: null },
        row('gemini-4-neon-medium-oauth'),
      );

      // Step 1: pick High (seq 1) and enqueue M1.
      await act(async () => { await result.current.pickModel('gemini-4-neon-high-oauth'); });
      const highSeq = result.current.sendModelSeq!;
      // Step 2: before M1 starts, switch to Low (seq 2, saved to DB).
      await act(async () => { await result.current.pickModel('gemini-4-neon-low-oauth'); });
      expect(result.current.model).toBe('gemini-4-neon-low-oauth');

      // Step 3: M1 (High, seq 1) starts and is admitted; server row moves back to High.
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq, admitted: true },
      });

      // Step 4: M1 ends -> reread returns High -> conditional repatch fails with 503.
      mocks.server = row('gemini-4-neon-high-oauth');
      mocks.getThread.mockResolvedValue(row('gemini-4-neon-high-oauth'));
      mocks.updateThread.mockRejectedValueOnce(Object.assign(new Error('503'), { response: { status: 503 } }));
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenLastCalledWith(
        THREAD,
        {
          llm_model: 'gemini-4-neon-low-oauth',
          expected_llm_model: 'gemini-4-neon-high-oauth',
          expected_reasoning_effort: null,
        },
      ));
      await act(async () => {});

      // Pill and next-message sendModel MUST stay on Low (never fall back to High).
      expect(result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(result.current.sendModel).toBe('gemini-4-neon-low-oauth');
    });

    // DALTON-HOOK(model-picker-races): R1-2a — when another tab changed the thread model while M1 ran, turn-end skips repatch and follows the other tab's model
    it('R1-2: skips repatch and follows Tab B model when turn-end reread shows an external model change', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));

      const { result, rerender } = setup(
        { workspaceId: 'ws-personal', activeTurnModel: null },
        row('gemini-4-neon-medium-oauth'),
      );

      await act(async () => { await result.current.pickModel('gemini-4-neon-high-oauth'); });
      const highSeq = result.current.sendModelSeq!;
      await act(async () => { await result.current.pickModel('gemini-4-neon-low-oauth'); });
      mocks.updateThread.mockClear();

      // M1 (High) runs while Tab B switches the thread to gpt-6.2-sol + reasoning_effort: 'high'.
      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq, admitted: true },
      });

      const tabBRow = { ...row('gpt-6.2-sol'), reasoning_effort: 'high' } as Thread;
      mocks.server = tabBRow;
      mocks.getThread.mockResolvedValueOnce(tabBRow);
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      await waitFor(() => expect(result.current.model).toBe('gpt-6.2-sol'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(model-picker-races): R1-2b — when conditional repatch receives 409 conflict, silently abandon repatch, refetch, and follow server's latest model
    it('R1-2: abandons repatch silently on 409 conflict and follows server latest model', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-medium-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-high-oauth'));
      mocks.updateThread.mockResolvedValueOnce(row('gemini-4-neon-low-oauth'));

      const { result, rerender } = setup(
        { workspaceId: 'ws-personal', activeTurnModel: null },
        row('gemini-4-neon-medium-oauth'),
      );

      await act(async () => { await result.current.pickModel('gemini-4-neon-high-oauth'); });
      const highSeq = result.current.sendModelSeq!;
      await act(async () => { await result.current.pickModel('gemini-4-neon-low-oauth'); });
      mocks.toast.mockClear();

      rerender({
        threadId: THREAD,
        isLoading: true,
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', pickSeq: highSeq, admitted: true },
      });

      // Reread still sees High, so Tab A issues conditional repatch, which hits 409 because Tab B saved gpt-6.2-sol in between.
      const tabBRow = row('gpt-6.2-sol');
      mocks.getThread
        .mockResolvedValueOnce(row('gemini-4-neon-high-oauth'))
        .mockResolvedValueOnce(tabBRow);
      mocks.updateThread.mockRejectedValueOnce(
        Object.assign(new Error('Conflict'), { response: { status: 409 } }),
      );
      rerender({
        threadId: THREAD,
        isLoading: false,
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      await waitFor(() => expect(result.current.model).toBe('gpt-6.2-sol'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.toast).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(model-picker-races): unresolved deferred flush carries expected_llm_model and yields on mid-wait update or 409
    it('unresolved deferred model flush skips PATCH on mid-wait update and yields silently on 409', async () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-a', 'model-x', 'model-y']);
      mocks.sharedEnabled = true;
      mocks.sharedEntry = undefined;

      // Case A: mid-wait detail update from Tab B (model-a -> model-y) before permission resolves.
      const viewA = setup({ workspaceId: 'ws-personal' }, row('model-a'));
      act(() => { void viewA.result.current.pickModel('model-x'); });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      act(() => {
        viewA.queryClient.setQueryData(queryKeys.threads.detail(THREAD), row('model-y'));
      });
      mocks.sharedEntry = null;
      viewA.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      await waitFor(() => expect(viewA.result.current.model).toBe('model-y'));
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(viewA.result.current.sendModel).toBeNull();
      viewA.unmount();

      // Case B: cache stays stale model-a -> resolves and sends conditional PATCH -> backend returns 409 -> yields & refetches model-y.
      mocks.sharedEntry = undefined;
      mocks.toast.mockClear();
      const viewB = setup({ workspaceId: 'ws-personal' }, row('model-a'));
      act(() => { void viewB.result.current.pickModel('model-x'); });

      mocks.updateThread.mockRejectedValueOnce(
        Object.assign(new Error('Conflict'), { response: { status: 409 } }),
      );
      mocks.getThread.mockResolvedValueOnce(row('model-y'));
      mocks.sharedEntry = null;
      viewB.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(
        THREAD,
        { llm_model: 'model-x', expected_llm_model: 'model-a' },
      ));
      await waitFor(() => expect(viewB.result.current.model).toBe('model-y'));
      expect(viewB.result.current.sendModel).toBeNull();
      expect(mocks.toast).not.toHaveBeenCalled();
      viewB.unmount();
    });

    // DALTON-HOOK(model-picker-races): R2 Codex-H1 & Grok-H1 — first-turn pre-id background repatch preserves unsent pick on 503, skips PATCH when server already moved, and silently follows server on 409
    it('first-turn pre-id repatch preserves unsent pick on 503, skips PATCH when server already changed, and yields silently on 409', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'model-a',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
      ]);
      mocks.sharedEntry = null;

      // Case 1: 503 on first-turn repatch -> carries expected_llm_model & expected_reasoning_effort, preserves unsent Neon Low & sendModel.
      const qc503 = createTestQueryClient();
      const firstRow = { ...row('model-a'), reasoning_effort: 'high', fast_mode: null } as Thread;
      const view503 = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-a', workspaceId: 'ws-personal' },
        null,
        qc503,
      );
      view503.rerender({ threadId: '__default__', isLoading: true, initialModel: 'model-a', workspaceId: 'ws-personal' });
      await act(async () => { await view503.result.current.pickModel('gemini-4-neon-low-oauth'); });

      mocks.server = firstRow;
      mocks.getThread.mockResolvedValue(firstRow);
      view503.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-a', workspaceId: 'ws-personal' });
      await waitFor(() => expect(qc503.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(firstRow));

      mocks.updateThread.mockRejectedValueOnce(Object.assign(new Error('503'), { response: { status: 503 } }));
      view503.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-a', workspaceId: 'ws-personal' });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        reasoning_effort: null,
        expected_llm_model: 'model-a',
        expected_reasoning_effort: 'high',
      }));
      await act(async () => {});
      expect(view503.result.current.model).toBe('gemini-4-neon-low-oauth');
      expect(view503.result.current.sendModel).toBe('gemini-4-neon-low-oauth');
      view503.unmount();

      // Case 2: Tab B / switch_thread_model already changed the thread to gpt-6.2-sol during the first turn -> skip PATCH & follow gpt-6.2-sol.
      mocks.updateThread.mockClear();
      mocks.toast.mockClear();
      const qcMoved = createTestQueryClient();
      const tabBRow = { ...row('gpt-6.2-sol'), reasoning_effort: 'high', fast_mode: null } as Thread;
      const viewMoved = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-a', workspaceId: 'ws-personal' },
        null,
        qcMoved,
      );
      viewMoved.rerender({ threadId: '__default__', isLoading: true, initialModel: 'model-a', workspaceId: 'ws-personal' });
      const onSettledMoved = vi.fn();
      await act(async () => {
        await viewMoved.result.current.pickModel('gemini-4-neon-low-oauth', { onSettled: onSettledMoved });
      });

      mocks.server = firstRow;
      mocks.getThread.mockResolvedValueOnce(firstRow).mockResolvedValueOnce(tabBRow);
      viewMoved.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-a', workspaceId: 'ws-personal' });
      await waitFor(() => expect(qcMoved.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(firstRow));

      mocks.server = tabBRow;
      viewMoved.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-a', workspaceId: 'ws-personal' });
      await waitFor(() => expect(viewMoved.result.current.model).toBe('gpt-6.2-sol'));
      expect(viewMoved.result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(onSettledMoved).toHaveBeenCalledWith(false);
      viewMoved.unmount();

      // Case 3: Reread still sees model-a, conditional PATCH receives 409 -> silently yields (no toast), refetches & follows Tab B's gpt-6.2-sol.
      mocks.updateThread.mockClear();
      mocks.toast.mockClear();
      const qc409 = createTestQueryClient();
      const view409 = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-a', workspaceId: 'ws-personal' },
        null,
        qc409,
      );
      view409.rerender({ threadId: '__default__', isLoading: true, initialModel: 'model-a', workspaceId: 'ws-personal' });
      const onSettled409 = vi.fn();
      await act(async () => {
        await view409.result.current.pickModel('gemini-4-neon-low-oauth', { onSettled: onSettled409 });
      });

      mocks.getThread
        .mockResolvedValueOnce(firstRow)
        .mockResolvedValueOnce(firstRow)
        .mockResolvedValueOnce(tabBRow);
      view409.rerender({ threadId: THREAD, isLoading: true, initialModel: 'model-a', workspaceId: 'ws-personal' });
      await waitFor(() => expect(qc409.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(firstRow));

      mocks.updateThread.mockRejectedValueOnce(Object.assign(new Error('Conflict'), { response: { status: 409 } }));
      view409.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-a', workspaceId: 'ws-personal' });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        reasoning_effort: null,
        expected_llm_model: 'model-a',
        expected_reasoning_effort: 'high',
      }));
      await waitFor(() => expect(view409.result.current.model).toBe('gpt-6.2-sol'));
      expect(view409.result.current.sendModel).toBeNull();
      expect(mocks.toast).not.toHaveBeenCalled();
      expect(onSettled409).toHaveBeenCalledWith(false);
      view409.unmount();
    });

    // DALTON-HOOK(model-picker-races): R2 Codex-H2 — when thread detail was still unread at pick time during unresolved permissions, cancel automatic flush instead of overwriting Tab B
    it('cancels unresolved deferred model flush when thread detail was unread at pick time and arrives later with Tab B model', async () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-x', 'model-y']);
      mocks.sharedEnabled = true;
      mocks.sharedEntry = undefined;

      const slowDetail = deferred<Thread>();
      const queryClient = createTestQueryClient();
      mocks.getThread.mockReturnValueOnce(slowDetail.promise);
      const { result, rerender } = renderHookWithProviders(
        (p: Props = { threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' }) => useThreadModel(p),
        { queryClient },
      );

      // Detail has not loaded yet and permissions are unresolved; User A picks model-x.
      expect(result.current.model).toBeNull();
      act(() => { void result.current.pickModel('model-x'); });
      expect(result.current.model).toBe('model-x');
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // Thread detail arrives late with Tab B's model-y, then permissions resolve to personal.
      await act(async () => {
        slowDetail.resolve(row('model-y'));
        await slowDetail.promise;
      });
      mocks.sharedEntry = null;
      rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });

      await waitFor(() => expect(result.current.model).toBe('model-y'));
      expect(result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(model-picker-races): R3 Codex #1 — background Antigravity saves (first-turn, existing-thread, deferred) MUST send explicit expected_reasoning_effort: null when row effort is null
    it('R3 Codex #1: attaches explicit expected_reasoning_effort: null on first-turn, existing-thread, and deferred Antigravity background saves when row effort is null', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;

      // 1) First-turn pre-id repatch to Antigravity when fresh.reasoning_effort is null.
      const qcFirst = createTestQueryClient();
      const firstRowNullEffort = { ...row('gpt-6.2-sol'), reasoning_effort: null, fast_mode: null } as Thread;
      const viewFirst = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'gpt-6.2-sol', workspaceId: 'ws-personal' },
        null,
        qcFirst,
      );
      viewFirst.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'gpt-6.2-sol',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gpt-6.2-sol', admitted: true },
      });
      await act(async () => { await viewFirst.result.current.pickModel('gemini-4-neon-low-oauth'); });
      mocks.server = firstRowNullEffort;
      mocks.getThread.mockResolvedValue(firstRowNullEffort);
      viewFirst.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'gpt-6.2-sol',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gpt-6.2-sol', admitted: true },
      });
      await waitFor(() => expect(qcFirst.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(firstRowNullEffort));
      mocks.updateThread.mockResolvedValueOnce({ ...row('gemini-4-neon-low-oauth'), reasoning_effort: null } as Thread);
      viewFirst.rerender({
        threadId: THREAD,
        isLoading: false,
        initialModel: 'gpt-6.2-sol',
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        expected_llm_model: 'gpt-6.2-sol',
        expected_reasoning_effort: null,
      }));
      viewFirst.unmount();

      // 2) Deferred permission-recovery flush to Antigravity when row effort is null.
      mocks.updateThread.mockClear();
      mocks.sharedEnabled = true;
      mocks.sharedEntry = undefined;
      const viewDeferred = setup(
        { workspaceId: 'ws-personal' },
        { ...row('gpt-6.2-sol'), reasoning_effort: null } as Thread,
      );
      act(() => { void viewDeferred.result.current.pickModel('gemini-4-neon-low-oauth'); });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      mocks.updateThread.mockResolvedValueOnce({ ...row('gemini-4-neon-low-oauth'), reasoning_effort: null } as Thread);
      mocks.sharedEntry = null;
      viewDeferred.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        expected_llm_model: 'gpt-6.2-sol',
        expected_reasoning_effort: null,
      }));
      viewDeferred.unmount();

      // 3) Deferred permission-recovery flush abandons when Tab B sets reasoning_effort='high' on same model before permissions resolve.
      mocks.updateThread.mockClear();
      mocks.sharedEntry = undefined;
      const viewDrift = setup(
        { workspaceId: 'ws-personal' },
        { ...row('gpt-6.2-sol'), reasoning_effort: null } as Thread,
      );
      act(() => { void viewDrift.result.current.pickModel('gemini-4-neon-low-oauth'); });
      act(() => {
        viewDrift.queryClient.setQueryData(
          queryKeys.threads.detail(THREAD),
          { ...row('gpt-6.2-sol'), reasoning_effort: 'high' } as Thread,
        );
      });
      mocks.sharedEntry = null;
      viewDrift.rerender({ threadId: THREAD, isLoading: false, workspaceId: 'ws-personal' });
      await waitFor(() => expect(viewDrift.result.current.model).toBe('gpt-6.2-sol'));
      expect(mocks.updateThread).not.toHaveBeenCalled();
      viewDrift.unmount();
    });

    // DALTON-HOOK(model-picker-races): R3 Codex #4 — once first-turn repatch succeeds, release local lock so another tab's later update takes effect
    it('R3 Codex #4: releases local lock after first-turn repatch succeeds so subsequent updates from another tab take effect', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;

      const qc = createTestQueryClient();
      const firstRow = { ...row('gemini-4-neon-high-oauth'), reasoning_effort: null } as Thread;
      const view = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'gemini-4-neon-high-oauth', workspaceId: 'ws-personal' },
        null,
        qc,
      );
      view.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'gemini-4-neon-high-oauth',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', admitted: true },
      });
      await act(async () => { await view.result.current.pickModel('gemini-4-neon-low-oauth'); });

      mocks.server = firstRow;
      mocks.getThread.mockResolvedValue(firstRow);
      view.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'gemini-4-neon-high-oauth',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', admitted: true },
      });
      await waitFor(() => expect(qc.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(firstRow));

      mocks.updateThread.mockResolvedValueOnce({ ...row('gemini-4-neon-low-oauth'), reasoning_effort: null } as Thread);
      view.rerender({
        threadId: THREAD,
        isLoading: false,
        initialModel: 'gemini-4-neon-high-oauth',
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        expected_llm_model: 'gemini-4-neon-high-oauth',
        expected_reasoning_effort: null,
      }));
      await waitFor(() => expect(cachedModel(qc)).toBe('gemini-4-neon-low-oauth'));

      // Tab B later switches the thread to gpt-6.2-sol and the detail cache refreshes via SSE.
      act(() => {
        qc.setQueryData(queryKeys.threads.detail(THREAD), row('gpt-6.2-sol'));
      });
      await waitFor(() => expect(view.result.current.model).toBe('gpt-6.2-sol'));
      expect(view.result.current.sendModel).toBeNull();
      view.unmount();
    });

    // DALTON-HOOK(model-picker-races): R3 Codex #5 — when first message fails before admission (503) leaving llm_model: null, reread does NOT discard user's later pick; when admission is unknown, keeps pick locally without PATCH
    it('R3 Codex #5: repatches with expected_llm_model: null when first message was unadmitted (503) and keeps pick locally when admission is indeterminate', async () => {
      mocks.catalogModelNames = new Set(['model-default', 'model-alpha', 'model-beta', 'model-other']);
      mocks.sharedEntry = null;

      // Case 1: First message refused/503 before admission (admitted: false) -> thread row has llm_model: null -> repatches model-beta with expected_llm_model: null.
      const qc503 = createTestQueryClient();
      const view503 = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-alpha', workspaceId: 'ws-personal' },
        null,
        qc503,
      );
      view503.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'model-alpha',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'model-alpha', admitted: false },
      });
      await act(async () => { await view503.result.current.pickModel('model-beta'); });

      const nullRow = row(null);
      mocks.server = nullRow;
      mocks.getThread.mockResolvedValue(nullRow);
      view503.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'model-alpha',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'model-alpha', admitted: false },
      });
      await waitFor(() => expect(qc503.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(nullRow));

      mocks.updateThread.mockResolvedValueOnce(row('model-beta'));
      view503.rerender({
        threadId: THREAD,
        isLoading: false,
        initialModel: 'model-alpha',
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'model-beta',
        expected_llm_model: null,
      }));
      expect(view503.result.current.model).toBe('model-beta');
      view503.unmount();

      // Case 2: Admission status cannot be confirmed (modelUnknown: true) and row has llm_model: null -> keep model-beta locally without issuing PATCH.
      mocks.updateThread.mockClear();
      const qcUnknown = createTestQueryClient();
      const viewUnknown = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-alpha', workspaceId: 'ws-personal' },
        null,
        qcUnknown,
      );
      viewUnknown.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'model-alpha',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: null, admitted: true, modelUnknown: true },
      });
      await act(async () => { await viewUnknown.result.current.pickModel('model-beta'); });

      mocks.server = nullRow;
      mocks.getThread.mockResolvedValue(nullRow);
      viewUnknown.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'model-alpha',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: null, admitted: true, modelUnknown: true },
      });
      await waitFor(() => expect(qcUnknown.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(nullRow));

      viewUnknown.rerender({
        threadId: THREAD,
        isLoading: false,
        initialModel: 'model-alpha',
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });
      await act(async () => {});
      expect(mocks.updateThread).not.toHaveBeenCalled();
      expect(viewUnknown.result.current.model).toBe('model-beta');
      expect(viewUnknown.result.current.sendModel).toBe('model-beta');
      viewUnknown.unmount();
    });

    // DALTON-HOOK(model-picker-races): R3 Codex #6 & Grok #1 — deferred permission-recovery model flush encountering 503 or network error preserves unsent model, sendModel, and Antigravity effort clear
    it('R3 Codex #6 / Grok #1: preserves unsent model, sendModel, and effort clear when deferred permission-recovery flush hits 503 or network error', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
      ]);
      mocks.sharedEnabled = true;
      mocks.sharedEntry = undefined;

      const queryClient = createTestQueryClient();
      const existingRow = { ...row('gpt-6.2-sol'), reasoning_effort: 'high', fast_mode: null } as Thread;
      queryClient.setQueryData(queryKeys.threads.detail(THREAD), existingRow);
      mocks.getThread.mockResolvedValue(existingRow);

      type P = { threadId: string; isLoading: boolean; tick: number };
      const init: P = { threadId: THREAD, isLoading: false, tick: 0 };
      const { result, rerender } = renderHookWithProviders(
        (p: P = init) => ({
          model: useThreadModel({
            threadId: p.threadId,
            isLoading: p.isLoading,
            workspaceId: 'ws-personal',
          }),
          tuning: useThreadTuning({
            threadId: p.threadId,
            workspaceId: 'ws-personal',
          }),
        }),
        { queryClient },
      );

      expect(result.current.tuning.tuning.reasoningEffort).toBe('high');
      await act(async () => {
        const effortClear = result.current.tuning.clearLocalEffort();
        await result.current.model.pickModel('gemini-4-neon-low-oauth', {
          onSettled: (saved) => {
            if (saved) effortClear.confirm();
            else effortClear();
          },
        });
      });
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // Permissions resolve to personal -> deferred conditional PATCH hits 503.
      mocks.updateThread.mockRejectedValueOnce(
        Object.assign(new Error('503'), { response: { status: 503 } }),
      );
      mocks.sharedEntry = null;
      rerender({ threadId: THREAD, isLoading: false, tick: 1 });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        reasoning_effort: null,
        expected_llm_model: 'gpt-6.2-sol',
        expected_reasoning_effort: 'high',
      }));
      await act(async () => {});

      // User's unsent Neon Low selection, sendModel, and cleared effort MUST stay preserved.
      expect(result.current.model.model).toBe('gemini-4-neon-low-oauth');
      expect(result.current.model.sendModel).toBe('gemini-4-neon-low-oauth');
      expect(result.current.tuning.tuning.reasoningEffort).toBeNull();
    });

    // DALTON-HOOK(model-picker-races): R3 Codex #7 — Core install (VITE_DALTON_UI=false) MUST still protect first-turn model repatch with CAS guards, 409 silent yield, and 503 preservation
    it('R3 Codex #7: protects first-turn model repatch with CAS guards, 409 yield, and 503 preservation when VITE_DALTON_UI is disabled', async () => {
      mocks.isDaltonBuild = false;
      mocks.catalogModelNames = new Set(['model-default', 'model-alpha', 'model-beta', 'model-tab-b']);

      // Case 1: Tab B saved model-tab-b before first turn ended -> Core build skips PATCH & follows Tab B.
      const qcMoved = createTestQueryClient();
      const viewMoved = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-alpha' },
        null,
        qcMoved,
      );
      viewMoved.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'model-alpha',
        activeTurnModel: { model: 'model-alpha', admitted: true },
      });
      await act(async () => { await viewMoved.result.current.pickModel('model-beta'); });

      mocks.getThread.mockResolvedValueOnce(row('model-alpha')).mockResolvedValueOnce(row('model-tab-b'));
      viewMoved.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'model-alpha',
        activeTurnModel: { model: 'model-alpha', admitted: true },
      });
      await waitFor(() => expect(qcMoved.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(row('model-alpha')));

      viewMoved.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-alpha', activeTurnModel: null });
      await waitFor(() => expect(viewMoved.result.current.model).toBe('model-tab-b'));
      expect(mocks.updateThread).not.toHaveBeenCalled();
      viewMoved.unmount();

      // Case 2: Reread sees model-alpha, conditional PATCH gets 409 -> Core build silently refetches Tab B without toast.
      mocks.updateThread.mockClear();
      mocks.toast.mockClear();
      const qc409 = createTestQueryClient();
      const view409 = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-alpha' },
        null,
        qc409,
      );
      view409.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'model-alpha',
        activeTurnModel: { model: 'model-alpha', admitted: true },
      });
      await act(async () => { await view409.result.current.pickModel('model-beta'); });

      mocks.getThread
        .mockResolvedValueOnce(row('model-alpha'))
        .mockResolvedValueOnce(row('model-alpha'))
        .mockResolvedValueOnce(row('model-tab-b'));
      view409.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'model-alpha',
        activeTurnModel: { model: 'model-alpha', admitted: true },
      });
      await waitFor(() => expect(qc409.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(row('model-alpha')));

      mocks.updateThread.mockRejectedValueOnce(Object.assign(new Error('Conflict'), { response: { status: 409 } }));
      view409.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-alpha', activeTurnModel: null });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'model-beta',
        expected_llm_model: 'model-alpha',
      }));
      await waitFor(() => expect(view409.result.current.model).toBe('model-tab-b'));
      expect(mocks.toast).not.toHaveBeenCalled();
      view409.unmount();

      // Case 3: Conditional PATCH hits 503 -> Core build preserves user's model-beta & sendModel.
      mocks.updateThread.mockClear();
      const qc503 = createTestQueryClient();
      const view503 = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'model-alpha' },
        null,
        qc503,
      );
      view503.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'model-alpha',
        activeTurnModel: { model: 'model-alpha', admitted: true },
      });
      await act(async () => { await view503.result.current.pickModel('model-beta'); });
      mocks.getThread.mockResolvedValue(row('model-alpha'));
      view503.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'model-alpha',
        activeTurnModel: { model: 'model-alpha', admitted: true },
      });
      await waitFor(() => expect(qc503.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(row('model-alpha')));

      mocks.updateThread.mockRejectedValueOnce(Object.assign(new Error('503'), { response: { status: 503 } }));
      view503.rerender({ threadId: THREAD, isLoading: false, initialModel: 'model-alpha', activeTurnModel: null });
      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'model-beta',
        expected_llm_model: 'model-alpha',
      }));
      await act(async () => {});
      expect(view503.result.current.model).toBe('model-beta');
      expect(view503.result.current.sendModel).toBe('model-beta');
      view503.unmount();
    });

    // DALTON-HOOK(model-picker-races): R4 Codex #2 — turn-end Antigravity repatch uses confirmed creation/pick baseline expected_reasoning_effort (never fresh.reasoning_effort), abandoning on drift & yielding silently on 409
    it('R4 Codex #2: abandons turn-end Antigravity repatch when another tab changed reasoning_effort before reread, and sends baseline expected_reasoning_effort (null) to yield on 409', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;

      // Case 1 (First-turn pre-id): Tab A created thread with reasoning_effort: null, picks Neon Low before ID arrives.
      // Tab B sets reasoning_effort='high' before Tab A's reread. Tab A MUST NOT echo 'high' as expected_reasoning_effort; it abandons repatch and follows server (gpt-6.2-sol + high).
      const qcFirstDrift = createTestQueryClient();
      const tabBHighRow = { ...row('gpt-6.2-sol'), reasoning_effort: 'high', fast_mode: null } as Thread;
      type FirstProps = {
        threadId: string;
        isLoading: boolean;
        initialModel: string | null;
        activeTurnModel?: { model: string | null; admitted?: boolean } | null;
        createdTuning?: { threadId: string; reasoningEffort: string | null; fastMode: boolean | null } | null;
      };
      const initFirst: FirstProps = {
        threadId: '__default__',
        isLoading: false,
        initialModel: 'gpt-6.2-sol',
      };
      const viewFirstDrift = renderHookWithProviders(
        (p: FirstProps = initFirst) => ({
          model: useThreadModel({
            threadId: p.threadId,
            isLoading: p.isLoading,
            initialModel: p.initialModel,
            workspaceId: 'ws-personal',
            activeTurnModel: p.activeTurnModel,
            createdTuning: p.createdTuning,
          }),
          tuning: useThreadTuning({
            threadId: p.threadId,
            workspaceId: 'ws-personal',
            createdTuning: p.createdTuning,
          }),
        }),
        { queryClient: qcFirstDrift },
      );

      viewFirstDrift.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'gpt-6.2-sol',
        activeTurnModel: { model: 'gpt-6.2-sol', admitted: true },
      });
      await act(async () => {
        const effortClear = viewFirstDrift.result.current.tuning.clearLocalEffort();
        await viewFirstDrift.result.current.model.pickModel('gemini-4-neon-low-oauth', {
          onSettled: (saved) => {
            if (saved) effortClear.confirm();
            else effortClear();
          },
        });
      });

      // Thread ID arrives with creation baseline reasoningEffort: null, but Tab B already saved 'high' before Tab A reads detail.
      mocks.getThread.mockResolvedValue(tabBHighRow);
      const createdNull = { threadId: THREAD, reasoningEffort: null, fastMode: null };
      viewFirstDrift.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'gpt-6.2-sol',
        activeTurnModel: { model: 'gpt-6.2-sol', admitted: true },
        createdTuning: createdNull,
      });
      await waitFor(() => expect(qcFirstDrift.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(tabBHighRow));

      viewFirstDrift.rerender({
        threadId: THREAD,
        isLoading: false,
        initialModel: 'gpt-6.2-sol',
        activeTurnModel: null,
        createdTuning: createdNull,
      });
      await waitFor(() => expect(viewFirstDrift.result.current.model.model).toBe('gpt-6.2-sol'));
      expect(viewFirstDrift.result.current.model.sendModel).toBeNull();
      expect(viewFirstDrift.result.current.tuning.tuning.reasoningEffort).toBe('high');
      expect(mocks.updateThread).not.toHaveBeenCalled();
      viewFirstDrift.unmount();

      // Case 2 (Existing-thread preserved unsent pick): Tab A observed reasoning_effort: null at pick time,
      // an earlier queued turn ran gpt-6.2-sol, and Tab B changed reasoning_effort to 'high' before Tab A's turn-end reread -> abandons repatch & follows Tab B.
      mocks.updateThread.mockClear();
      const qcExistingDrift = createTestQueryClient();
      const initialNullRow = { ...row('gpt-6.2-sol'), reasoning_effort: null, fast_mode: null } as Thread;
      qcExistingDrift.setQueryData(queryKeys.threads.detail(THREAD), initialNullRow);
      mocks.getThread.mockResolvedValue(initialNullRow);

      const viewExistingDrift = renderHookWithProviders(
        (p: { isLoading: boolean; activeTurnModel?: { model: string | null; admitted?: boolean } | null } = { isLoading: false }) =>
          useThreadModel({
            threadId: THREAD,
            isLoading: p.isLoading,
            workspaceId: 'ws-personal',
            activeTurnModel: p.activeTurnModel,
          }),
        { queryClient: qcExistingDrift },
      );

      // User A picks Neon Low (saved on server when row had reasoning_effort: null), then an earlier queued turn on gpt-6.2-sol runs.
      const savedNeonLow = { ...row('gemini-4-neon-low-oauth'), reasoning_effort: null, fast_mode: null } as Thread;
      mocks.updateThread.mockResolvedValueOnce(savedNeonLow);
      mocks.getThread.mockResolvedValue(savedNeonLow);
      await act(async () => {
        await viewExistingDrift.result.current.pickModel('gemini-4-neon-low-oauth');
      });
      mocks.updateThread.mockClear();

      // Earlier queued turn on gpt-6.2-sol starts (preserves Neon Low with expectedEffort: null).
      viewExistingDrift.rerender({
        isLoading: true,
        activeTurnModel: { model: 'gpt-6.2-sol', admitted: true },
      });

      // Turn ends; reread returns Tab B's { llm_model: 'gpt-6.2-sol', reasoning_effort: 'high' } -> effort drifted from null -> abandon repatch!
      mocks.getThread.mockResolvedValue(tabBHighRow);
      viewExistingDrift.rerender({
        isLoading: false,
        activeTurnModel: null,
      });
      await waitFor(() => expect(viewExistingDrift.result.current.model).toBe('gpt-6.2-sol'));
      expect(viewExistingDrift.result.current.sendModel).toBeNull();
      expect(mocks.updateThread).not.toHaveBeenCalled();
      viewExistingDrift.unmount();

      // Case 3 (Existing-thread preserved unsent pick hitting 409): reread still sees null -> sends expected_reasoning_effort: null -> 409 yields silently to Tab B's high.
      mocks.updateThread.mockClear();
      mocks.toast.mockClear();
      const qcExisting409 = createTestQueryClient();
      qcExisting409.setQueryData(queryKeys.threads.detail(THREAD), initialNullRow);
      mocks.getThread.mockResolvedValue(initialNullRow);

      const viewExisting409 = renderHookWithProviders(
        (p: { isLoading: boolean; activeTurnModel?: { model: string | null; admitted?: boolean } | null } = { isLoading: false }) =>
          useThreadModel({
            threadId: THREAD,
            isLoading: p.isLoading,
            workspaceId: 'ws-personal',
            activeTurnModel: p.activeTurnModel,
          }),
        { queryClient: qcExisting409 },
      );

      mocks.updateThread.mockResolvedValueOnce(savedNeonLow);
      mocks.getThread.mockResolvedValue(savedNeonLow);
      await act(async () => {
        await viewExisting409.result.current.pickModel('gemini-4-neon-low-oauth');
      });
      mocks.updateThread.mockClear();

      viewExisting409.rerender({
        isLoading: true,
        activeTurnModel: { model: 'gpt-6.2-sol', admitted: true },
      });

      mocks.getThread
        .mockResolvedValueOnce(initialNullRow)
        .mockResolvedValueOnce(tabBHighRow);
      mocks.updateThread.mockRejectedValueOnce(
        Object.assign(new Error('Conflict'), { response: { status: 409 } }),
      );
      viewExisting409.rerender({
        isLoading: false,
        activeTurnModel: null,
      });

      await waitFor(() => expect(mocks.updateThread).toHaveBeenCalledWith(THREAD, {
        llm_model: 'gemini-4-neon-low-oauth',
        expected_llm_model: 'gpt-6.2-sol',
        expected_reasoning_effort: null,
      }));
      await waitFor(() => expect(viewExisting409.result.current.model).toBe('gpt-6.2-sol'));
      expect(viewExisting409.result.current.sendModel).toBeNull();
      expect(mocks.toast).not.toHaveBeenCalled();
      viewExisting409.unmount();
    });

    // DALTON-HOOK(model-picker-races): R4 Codex #4 — when turn-end reread confirms server already saved the same model (first-turn or existing-thread), release preservedUnsentSeqRef so subsequent Tab B switches take effect
    it('R4 Codex #4: releases local lock when turn-end reread confirms the picked model is already saved so subsequent Tab B switches take effect', async () => {
      mocks.seriesCatalog = NEON_CATALOG;
      mocks.rawMetadata = NEON_RAW_META;
      mocks.catalogModelNames = new Set([
        'model-default',
        'gpt-6.2-sol',
        'gemini-4-neon-low-oauth',
        'gemini-4-neon-high-oauth',
      ]);
      mocks.sharedEntry = null;

      // Case 1: First-turn pre-id pick Low; Tab B also saves Low before A's reread -> A clears pending & local lock -> Tab B later switches to High -> A follows High (model='High', sendModel=null).
      const qcFirst = createTestQueryClient();
      const firstRowHigh = { ...row('gemini-4-neon-high-oauth'), reasoning_effort: null } as Thread;
      const tabBLowRow = { ...row('gemini-4-neon-low-oauth'), reasoning_effort: null } as Thread;
      const viewFirst = setup(
        { threadId: '__default__', isLoading: false, initialModel: 'gemini-4-neon-high-oauth', workspaceId: 'ws-personal' },
        null,
        qcFirst,
      );
      viewFirst.rerender({
        threadId: '__default__',
        isLoading: true,
        initialModel: 'gemini-4-neon-high-oauth',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', admitted: false },
      });
      await act(async () => { await viewFirst.result.current.pickModel('gemini-4-neon-low-oauth'); });

      // Admission arrives while manualPick is Low -> recordDaltonTurnPick sets preservedUnsentSeqRef = Low.seq.
      mocks.getThread
        .mockResolvedValueOnce(firstRowHigh)
        .mockResolvedValueOnce(tabBLowRow);
      viewFirst.rerender({
        threadId: THREAD,
        isLoading: true,
        initialModel: 'gemini-4-neon-high-oauth',
        workspaceId: 'ws-personal',
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', admitted: true },
      });
      await waitFor(() => expect(qcFirst.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(firstRowHigh));

      // First turn ends; reread sees Tab B already saved Neon Low -> no PATCH, local lock released!
      viewFirst.rerender({
        threadId: THREAD,
        isLoading: false,
        initialModel: 'gemini-4-neon-high-oauth',
        workspaceId: 'ws-personal',
        activeTurnModel: null,
      });
      await waitFor(() => expect(qcFirst.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(tabBLowRow));
      expect(mocks.updateThread).not.toHaveBeenCalled();

      // Tab B later switches the thread to Neon High -> Tab A MUST follow Neon High (not stay pinned on Low).
      act(() => {
        qcFirst.setQueryData(queryKeys.threads.detail(THREAD), row('gemini-4-neon-high-oauth'));
      });
      await waitFor(() => expect(viewFirst.result.current.model).toBe('gemini-4-neon-high-oauth'));
      expect(viewFirst.result.current.sendModel).toBeNull();
      viewFirst.unmount();

      // Case 2: Existing-thread preserved unsent pick Low; Tab B already saved Low before reread -> local lock released -> Tab B switches to gpt-6.2-sol -> A follows gpt-6.2-sol.
      mocks.updateThread.mockClear();
      const qcExisting = createTestQueryClient();
      qcExisting.setQueryData(queryKeys.threads.detail(THREAD), firstRowHigh);
      mocks.getThread.mockResolvedValue(tabBLowRow);
      const viewExisting = renderHookWithProviders(
        (p: { isLoading: boolean; activeTurnModel?: { model: string | null; admitted?: boolean } | null } = { isLoading: false }) =>
          useThreadModel({
            threadId: THREAD,
            isLoading: p.isLoading,
            workspaceId: 'ws-personal',
            activeTurnModel: p.activeTurnModel,
          }),
        { queryClient: qcExisting },
      );

      mocks.updateThread.mockResolvedValueOnce(tabBLowRow);
      await act(async () => { await viewExisting.result.current.pickModel('gemini-4-neon-low-oauth'); });
      mocks.updateThread.mockClear();

      viewExisting.rerender({
        isLoading: true,
        activeTurnModel: { model: 'gemini-4-neon-high-oauth', admitted: true },
      });
      viewExisting.rerender({
        isLoading: false,
        activeTurnModel: null,
      });
      await waitFor(() => expect(qcExisting.getQueryData<Thread>(queryKeys.threads.detail(THREAD))).toEqual(tabBLowRow));
      expect(mocks.updateThread).not.toHaveBeenCalled();

      act(() => {
        qcExisting.setQueryData(queryKeys.threads.detail(THREAD), row('gpt-6.2-sol'));
      });
      await waitFor(() => expect(viewExisting.result.current.model).toBe('gpt-6.2-sol'));
      expect(viewExisting.result.current.sendModel).toBeNull();
      viewExisting.unmount();
    });
  });
});
