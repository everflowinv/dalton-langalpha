/**
 * End-to-end hook integration tests verifying that real first-send thread
 * creation paths (`ensureThreadId` pre-create and legacy SSE first frame with
 * `thread_id`) set `createdThreadId === newId` even though both upstream paths
 * mutate `threadIdRef.current` before calling `setThreadId`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { createTestQueryClient, renderHookWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { buildSeriesCatalog, type AntigravitySeriesCatalog } from '@/lib/antigravitySeries';
import type { Thread } from '@/types/api';
import type {
  createThreadWithTitle as createThreadWithTitleApi,
  getThread as getThreadApi,
  sendChatMessageStream as sendChatMessageStreamApi,
  ThreadCreatedInfo,
  updateThread as updateThreadApi,
} from '@/pages/ChatAgent/utils/api';
import type {
  getStoredThreadId as getStoredThreadIdFn,
  removeStoredThreadId as removeStoredThreadIdFn,
  setStoredThreadId as setStoredThreadIdFn,
} from '@/pages/ChatAgent/hooks/utils/threadStorage';

const NEON_CATALOG: AntigravitySeriesCatalog = buildSeriesCatalog([
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

const mocks = vi.hoisted(() => ({
  isDaltonBuild: true,
  createThreadWithTitle: vi.fn<typeof createThreadWithTitleApi>(),
  sendChatMessageStream: vi.fn<typeof sendChatMessageStreamApi>(),
  getThread: vi.fn<typeof getThreadApi>(),
  updateThread: vi.fn<typeof updateThreadApi>(),
  toast: vi.fn(),
  getStoredThreadId: vi.fn<typeof getStoredThreadIdFn>().mockReturnValue('__default__'),
  setStoredThreadId: vi.fn<typeof setStoredThreadIdFn>(),
  removeStoredThreadId: vi.fn<typeof removeStoredThreadIdFn>(),
  catalogModelNames: new Set<string>(['model-default', 'model-a', 'model-b', 'gemini-4-neon-medium-oauth']),
}));

vi.mock('react-i18next', () => {
  const t = (k: string) => k;
  return { useTranslation: () => ({ t }) };
});

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  get isDaltonBuild() {
    return mocks.isDaltonBuild;
  },
}));

vi.mock('@/pages/ChatAgent/hooks/utils/threadStorage', () => ({
  getStoredThreadId: mocks.getStoredThreadId,
  setStoredThreadId: mocks.setStoredThreadId,
  removeStoredThreadId: mocks.removeStoredThreadId,
}));

vi.mock('@/pages/ChatAgent/utils/api', async () => {
  const { apiMockModule } = await import('@/pages/ChatAgent/hooks/__tests__/chatHookHarness');
  return apiMockModule({
    createThreadWithTitle: mocks.createThreadWithTitle,
    sendChatMessageStream: mocks.sendChatMessageStream,
    getThread: mocks.getThread,
    updateThread: mocks.updateThread,
  });
});

vi.mock('@/hooks/useSlotDefaultModels', () => ({
  useDefaultModel: () => 'model-default',
}));

vi.mock('@/hooks/useTurnModels', () => ({
  useTurnDefaultModel: () => 'model-default',
}));

vi.mock('@/hooks/useLapsedSignIn', () => ({
  useLapsedSignIn: () => () => null,
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    catalogModelNames: mocks.catalogModelNames,
    metadata: undefined,
    seriesCatalog: NEON_CATALOG,
    rawApiResponse: { model_metadata: NEON_RAW_META },
    customModels: [],
  }),
}));

vi.mock('@/hooks/useSharedWorkspaces', () => ({
  useSharedEntry: () => null,
  useSharedWorkspaces: () => ({
    enabled: true,
    data: { workspaces: [] },
    isLoading: false,
    isError: false,
  }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  toast: mocks.toast,
}));

import { settleMountEffect } from '@/pages/ChatAgent/hooks/__tests__/chatHookHarness';
import { useChatMessages } from '@/pages/ChatAgent/hooks/useChatMessages';
import { useThreadModel } from '@/pages/ChatAgent/hooks/useThreadModel';
import { useThreadTuning } from '@/pages/ChatAgent/hooks/useThreadTuning';

function threadRow(threadId: string, overrides: Partial<Thread> = {}): Thread {
  return {
    thread_id: threadId,
    workspace_id: 'ws-personal',
    thread_index: 1,
    current_status: 'in_progress',
    msg_type: 'user',
    title: 'First chat',
    created_at: '2026-11-26T00:00:00Z',
    updated_at: '2026-11-26T00:00:00Z',
    llm_model: 'model-a',
    reasoning_effort: 'high',
    fast_mode: null,
    ...overrides,
  } as Thread;
}

function renderWiredHooks(queryClient = createTestQueryClient()) {
  return renderHookWithProviders(
    () => {
      const chat = useChatMessages('ws-personal', null);
      const model = useThreadModel({
        threadId: chat.threadId,
        isLoading: chat.isLoading,
        initialModel: 'model-a',
        workspaceId: 'ws-personal',
        createdThreadId: chat.createdThreadId,
        activeTurnModel: chat.activeTurnModel,
      });
      const tuning = useThreadTuning({
        threadId: chat.threadId,
        workspaceId: 'ws-personal',
        createdThreadId: chat.createdThreadId,
      });
      return { chat, model, tuning };
    },
    { queryClient },
  );
}

describe('createdThreadId transition across real useChatMessages + useThreadModel + useThreadTuning', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.isDaltonBuild = true;
    mocks.getStoredThreadId.mockReturnValue('__default__');
    mocks.updateThread.mockImplementation(async (threadId, patch) =>
      threadRow(threadId, {
        ...(patch.llm_model !== undefined ? { llm_model: patch.llm_model } : {}),
        ...(patch.reasoning_effort !== undefined ? { reasoning_effort: patch.reasoning_effort } : {}),
        ...(patch.fast_mode !== undefined ? { fast_mode: patch.fast_mode } : {}),
      }),
    );
  });

  it('keeps newly picked model, effort, and bound clearLocal handle across real ensureThreadId creation', async () => {
    const NEW_ID = 'thread-created-ensure-1';
    let resolveCreate!: (info: ThreadCreatedInfo) => void;
    mocks.createThreadWithTitle.mockImplementationOnce(
      () => new Promise<ThreadCreatedInfo>((resolve) => { resolveCreate = resolve; }),
    );
    let resolveStream!: (res: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mocks.sendChatMessageStream.mockImplementationOnce(
      () => new Promise((resolve) => { resolveStream = resolve; }),
    );
    mocks.getThread.mockResolvedValue(threadRow(NEW_ID));

    const queryClient = createTestQueryClient();
    const { result } = renderWiredHooks(queryClient);
    await settleMountEffect();

    expect(result.current.chat.threadId).toBe('__default__');
    expect(result.current.chat.createdThreadId).toBeNull();

    // Pick initial effort before first send.
    await act(async () => {
      await result.current.tuning.setReasoningEffort('high');
    });

    // Launch first send — ensureThreadId awaits createThreadWithTitle.
    let sendPromise!: Promise<boolean>;
    act(() => {
      sendPromise = result.current.chat.handleSendMessage('Analyze NVDA', null, null, {
        model: 'model-a',
        reasoningEffort: 'high',
        fastMode: false,
      });
    });
    await waitFor(() => expect(mocks.createThreadWithTitle).toHaveBeenCalledTimes(1));

    // While pre-create is in flight, change effort, fastMode, and pick a new Antigravity model with a null-origin clearLocal handle.
    let clearHandle!: ReturnType<typeof result.current.tuning.clearLocalEffort>;
    await act(async () => {
      await result.current.tuning.setReasoningEffort('low');
      await result.current.tuning.setFastMode(true);
      clearHandle = result.current.tuning.clearLocalEffort();
      await result.current.model.pickModel('gemini-4-neon-medium-oauth', {
        onSettled: (saved) => {
          if (saved) clearHandle.confirm();
          else clearHandle();
        },
      });
    });

    expect(result.current.model.model).toBe('gemini-4-neon-medium-oauth');
    expect(result.current.tuning.tuning.fastMode).toBe(true);
    expect(result.current.tuning.tuning.reasoningEffort).toBeNull();

    // Resolve createThreadWithTitle — ensureThreadId writes threadIdRef.current BEFORE calling setThreadIdFromSend.
    await act(async () => {
      resolveCreate({
        thread_id: NEW_ID,
        workspace_id: 'ws-personal',
        thread_index: 1,
        title: 'Analyze NVDA',
        msg_type: 'user',
        reasoning_effort: 'high',
        fast_mode: null,
      });
    });

    await waitFor(() => expect(result.current.chat.threadId).toBe(NEW_ID));
    expect(result.current.chat.createdThreadId).toBe(NEW_ID);
    expect(result.current.model.model).toBe('gemini-4-neon-medium-oauth');
    expect(result.current.tuning.tuning.fastMode).toBe(true);
    expect(result.current.tuning.tuning.reasoningEffort).toBeNull();

    // Calling restore() on the pre-id clearHandle must still target NEW_ID (bound, not invalidated as a switch)
    // and restore the 'low' effort picked while creation was in flight.
    act(() => {
      clearHandle();
    });
    expect(result.current.tuning.tuning.reasoningEffort).toBe('low');

    // Once the created thread detail loads, createdRef persists the in-flight effort pick to the new row.
    await waitFor(() => expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(NEW_ID))).toBeDefined());
    await waitFor(() =>
      expect(mocks.updateThread).toHaveBeenCalledWith(NEW_ID, { reasoning_effort: 'low' }),
    );

    await act(async () => {
      resolveStream({ disconnected: false, aborted: false, contentLocation: null });
      await sendPromise;
    });
  });

  it('keeps newly picked model, effort, and bound clearLocal handle when legacy SSE first frame carries thread_id', async () => {
    const LEGACY_ID = 'thread-created-legacy-sse-1';
    mocks.createThreadWithTitle.mockRejectedValueOnce(new Error('pre-create unavailable'));

    let capturedOnEvent: ((event: Record<string, unknown>) => void) | undefined;
    let resolveStream!: (res: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mocks.sendChatMessageStream.mockImplementationOnce(
      (_msg, _ws, _tid, opts) => {
        capturedOnEvent = opts?.onEvent as ((event: Record<string, unknown>) => void) | undefined;
        return new Promise((resolve) => { resolveStream = resolve; });
      },
    );
    mocks.getThread.mockResolvedValue(threadRow(LEGACY_ID, { reasoning_effort: null }));

    const queryClient = createTestQueryClient();
    const { result } = renderWiredHooks(queryClient);
    await settleMountEffect();

    await act(async () => {
      await result.current.tuning.setReasoningEffort('high');
    });

    let sendPromise!: Promise<boolean>;
    act(() => {
      sendPromise = result.current.chat.handleSendMessage('Legacy first send', null, null, {
        model: 'model-a',
        reasoningEffort: 'high',
        fastMode: false,
      });
    });

    await waitFor(() => expect(mocks.sendChatMessageStream).toHaveBeenCalledTimes(1));
    expect(mocks.sendChatMessageStream.mock.calls[0][2]).toBe('__default__');

    // While waiting for the first SSE frame on '__default__', change effort, fastMode, and pick a new model with clearLocal.
    let clearHandle!: ReturnType<typeof result.current.tuning.clearLocalEffort>;
    await act(async () => {
      await result.current.tuning.setReasoningEffort('low');
      await result.current.tuning.setFastMode(true);
      clearHandle = result.current.tuning.clearLocalEffort();
      await result.current.model.pickModel('gemini-4-neon-medium-oauth', {
        onSettled: (saved) => {
          if (saved) clearHandle.confirm();
          else clearHandle();
        },
      });
    });

    // Legacy SSE first content frame carries thread_id; processStreamEvent:315-317 mutates rt.threadIdRef.current BEFORE calling rt.setThreadId.
    act(() => {
      capturedOnEvent?.({
        event: 'message_chunk',
        thread_id: LEGACY_ID,
        content: 'Analyzing...',
        content_type: 'text',
      });
    });

    await waitFor(() => expect(result.current.chat.threadId).toBe(LEGACY_ID));
    expect(result.current.chat.createdThreadId).toBe(LEGACY_ID);
    expect(result.current.model.model).toBe('gemini-4-neon-medium-oauth');
    expect(result.current.tuning.tuning.fastMode).toBe(true);
    expect(result.current.tuning.tuning.reasoningEffort).toBeNull();

    // Null-origin clearHandle bound to LEGACY_ID: restore() brings back the in-flight 'low' effort.
    act(() => {
      clearHandle();
    });
    expect(result.current.tuning.tuning.reasoningEffort).toBe('low');

    await waitFor(() => expect(queryClient.getQueryData<Thread>(queryKeys.threads.detail(LEGACY_ID))).toBeDefined());
    await waitFor(() =>
      expect(mocks.updateThread).toHaveBeenCalledWith(LEGACY_ID, { reasoning_effort: 'low' }),
    );

    await act(async () => {
      resolveStream({ disconnected: false, aborted: false, contentLocation: null });
      await sendPromise;
    });
  });
});
