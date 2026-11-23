/**
 * A composer's reasoning effort and fast mode are its thread's own, over what
 * Settings gives the model. A thread host owns them (the hook behind `tuning`
 * is pinned in useThreadTuning's tests); a composer with no thread holds them
 * and hands them to the thread its send starts. These pin what each shows and
 * sends: the thread's own level, shown clamped onto the model's ladder and
 * sent as the row holds it, else the Settings level; a pick handed to the host, null for the level Settings
 * gives, and never written to the account; and every send carrying the level
 * the pill shows.
 */
import { createRef } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ChatInput, { type ChatInputHandle } from '../chat-input';
import type { ThreadTuning } from '../chat-input.types';
import type { ModelTuning } from '../chat-input.modelTuning';
import { ChatInputRegistry, ContextBus } from '@/lib/contextBus';
// DALTON-HOOK(automation-effort): Antigravity series catalog fixture for retry effort-variant tests
import { buildSelectableAntigravitySeries, buildSeriesCatalog } from '@/lib/antigravitySeries';
import { queryKeys } from '@/lib/queryKeys';

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

const mocks = vi.hoisted(() => ({
  preferences: null as unknown,
  mutateAsync: vi.fn(),
  mutate: vi.fn(),
}));

// DALTON-HOOK(automation-effort): Antigravity series catalog and metadata used by series-effort retry tests
const AG_SERIES_CATALOG = buildSeriesCatalog([
  {
    series_id: 'antigravity-oauth:gemini-4-neon',
    series_prefix: 'gemini-4-neon',
    provider: 'antigravity-oauth',
    display_name: 'Gemini 4 Neon',
    qualified_display_name: 'Gemini 4 Neon (Antigravity)',
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
const AG_MODELS = {
  'antigravity-oauth': {
    display_name: 'Google Antigravity',
    models: [
      'gemini-4-neon-low-oauth',
      'gemini-4-neon-medium-oauth',
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
    ],
  },
};
// DALTON-HOOK(automation-effort): metadata for Antigravity series variants alongside default test model
const AG_RAW_METADATA = {
  'model-default': { sdk: 'codex', reasoning_efforts: ['low', 'medium', 'high'], reasoning_effort_default: 'medium' },
  'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['low'] },
  'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['medium'] },
  'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['high'] },
  'gemini-4-carbon-medium-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['medium'] },
};

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: mocks.preferences, isLoading: false, isLoaded: true }),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    models: AG_MODELS,
    payers: {},
    // DALTON-HOOK(automation-effort): expose Antigravity series variants for series-effort retry tests
    validModelNames: new Set([
      'model-default',
      'gemini-4-neon-low-oauth',
      'gemini-4-neon-medium-oauth',
      'gemini-4-neon-high-oauth',
      'gemini-4-carbon-medium-oauth',
    ]),
    metadata: AG_RAW_METADATA,
    rawApiResponse: { model_metadata: AG_RAW_METADATA },
    seriesCatalog: AG_SERIES_CATALOG,
    selectableSeries: buildSelectableAntigravitySeries(AG_SERIES_CATALOG, AG_MODELS, {
      rawMetadata: AG_RAW_METADATA,
      customModels: [],
    }),
    customModels: [],
    isLoading: false,
    systemDefaults: { default_model: 'model-default' },
    offPlanModels: new Set(),
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutateAsync: mocks.mutateAsync, mutate: mocks.mutate }),
}));

vi.mock('../use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
  toast: vi.fn(),
}));

// The picker's own rendering is pinned in chatInputModelTuning's tests; this
// one reads the tuning the pill is handed and reaches the same handlers its
// levels and speeds call.
vi.mock('../chat-input.modelMenu', () => ({
  // DALTON-HOOK(automation-effort): expose both unified tuning controls and model-menu pick button to distinguish model pick from effort pick
  ChatInputModelMenu: ({ tuning, onSelectModel }: { tuning: ModelTuning; onSelectModel?: (model: string) => void }) => (
    <>
      <span>{`effort:${tuning.reasoningEffort}`}</span>
      <span>{`default:${tuning.defaultEffort}`}</span>
      <span>{`fast:${tuning.fastMode}`}</span>
      <button type="button" onClick={() => onSelectModel?.('gemini-4-carbon-medium-oauth')}>
        pick-model-carbon
      </button>
      <button type="button" onClick={() => tuning.onReasoningEffortChange('high')}>pick-high</button>
      <button type="button" onClick={() => tuning.onReasoningEffortChange(null)}>pick-default</button>
      <button type="button" onClick={() => tuning.onFastModeChange(true)}>pick-fast</button>
      <button type="button" onClick={() => tuning.onFastModeChange(false)}>pick-standard</button>
    </>
  ),
  ModelTriggerMeasure: () => null,
}));

let queryClient: QueryClient;

interface TreeProps {
  onSend?: (...args: unknown[]) => void;
  tuning?: ThreadTuning;
  onPickReasoningEffort?: (effort: string | null) => void;
  onPickFastMode?: (fast: boolean | null) => void;
  inputRef?: React.Ref<ChatInputHandle>;
  // DALTON-HOOK(automation-effort): pass active retry target id and host model props into ChatInput
  retryTargetId?: string | null;
  model?: string;
  sendModel?: string | null;
  onPickModel?: (model: string) => void;
}

function tree({
  onSend = vi.fn(),
  tuning,
  onPickReasoningEffort,
  onPickFastMode,
  inputRef,
  retryTargetId,
  model = 'model-default',
  sendModel,
  onPickModel,
}: TreeProps = {}) {
  queryClient.setQueryData(queryKeys.user.preferences(), mocks.preferences);
  // DALTON-HOOK(automation-effort): wire retryTargetId and host model props into ChatInput
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChatInput
          ref={inputRef}
          onSend={onSend}
          model={model}
          sendModel={sendModel}
          onPickModel={onPickModel}
          tuning={tuning}
          onPickReasoningEffort={onPickReasoningEffort}
          onPickFastMode={onPickFastMode}
          retryTargetId={retryTargetId}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function send(text: string) {
  const textarea = screen.getByRole('textbox');
  fireEvent.change(textarea, { target: { value: text } });
  fireEvent.keyDown(textarea, { key: 'Enter' });
}

function sentOptions(onSend: ReturnType<typeof vi.fn>) {
  return onSend.mock.calls.at(-1)?.[0].modelOptions as Record<string, unknown>;
}

describe('ChatInput: the thread owns its effort and fast mode', () => {
  beforeEach(() => {
    ContextBus.__resetForTests();
    ChatInputRegistry.__resetForTests();
    Element.prototype.scrollIntoView = vi.fn();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Settings: the model's profile says low, the account says fast.
    mocks.preferences = {
      model_preference: { fast_mode: true, profiles: { 'model-default': { reasoning_effort: 'low' } } },
    };
    mocks.mutate.mockReset();
    mocks.mutateAsync.mockReset();
    localStorage.clear();
  });

  it('shows and sends what Settings gives a thread with none of its own', () => {
    const onSend = vi.fn();
    render(tree({ onSend, tuning: { reasoningEffort: null, fastMode: null } }));
    expect(screen.getByText('effort:low')).toBeInTheDocument();
    expect(screen.getByText('default:low')).toBeInTheDocument();
    expect(screen.getByText('fast:true')).toBeInTheDocument();

    send('hi');
    expect(sentOptions(onSend)).toMatchObject({ reasoningEffort: 'low', fastMode: true });
  });

  it("shows the thread's own clamped onto the model's ladder, and sends it as held", () => {
    const onSend = vi.fn();
    render(tree({ onSend, tuning: { reasoningEffort: 'max', fastMode: false } }));
    expect(screen.getByText('effort:high')).toBeInTheDocument();
    expect(screen.getByText('default:low')).toBeInTheDocument();

    send('hi');
    expect(sentOptions(onSend)).toMatchObject({ reasoningEffort: 'max', fastMode: false });
    expect(sentOptions(onSend)).not.toHaveProperty('reasoningEffortOverride');
  });

  it('claims and sends no level while the thread is unread, so the server runs its own', () => {
    const onSend = vi.fn();
    render(tree({ onSend, tuning: { reasoningEffort: undefined, fastMode: undefined } }));
    expect(screen.getByText('effort:null')).toBeInTheDocument();

    send('hi');
    expect(sentOptions(onSend)).toMatchObject({ reasoningEffort: null, fastMode: null });
  });

  it('hands a pick to the host and never writes Settings', () => {
    const onPickReasoningEffort = vi.fn();
    const onPickFastMode = vi.fn();
    render(tree({ tuning: { reasoningEffort: null, fastMode: null }, onPickReasoningEffort, onPickFastMode }));

    fireEvent.click(screen.getByText('pick-high'));
    expect(onPickReasoningEffort).toHaveBeenCalledWith('high');
    // Speed on the side Settings is on follows Settings; the other side is the thread's own.
    fireEvent.click(screen.getByText('pick-standard'));
    expect(onPickFastMode).toHaveBeenCalledWith(false);
    expect(mocks.mutate).not.toHaveBeenCalled();
    expect(mocks.mutateAsync).not.toHaveBeenCalled();
  });

  it("returns the thread to Settings with a pick of the side Settings is on", () => {
    const onPickReasoningEffort = vi.fn();
    const onPickFastMode = vi.fn();
    render(tree({ tuning: { reasoningEffort: 'high', fastMode: false }, onPickReasoningEffort, onPickFastMode }));

    fireEvent.click(screen.getByText('pick-default'));
    expect(onPickReasoningEffort).toHaveBeenCalledWith(null);
    fireEvent.click(screen.getByText('pick-fast'));
    expect(onPickFastMode).toHaveBeenCalledWith(null);
  });

  it('with no host, holds its picks and hands them to the thread its send starts', () => {
    const onSend = vi.fn();
    const inputRef = createRef<ChatInputHandle>();
    render(tree({ onSend, inputRef }));
    send('first');
    expect(sentOptions(onSend)).not.toHaveProperty('reasoningEffortOverride');
    expect(sentOptions(onSend)).not.toHaveProperty('fastModeOverride');

    fireEvent.click(screen.getByText('pick-high'));
    fireEvent.click(screen.getByText('pick-standard'));
    expect(screen.getByText('effort:high')).toBeInTheDocument();
    expect(screen.getByText('fast:false')).toBeInTheDocument();

    send('second');
    expect(sentOptions(onSend)).toMatchObject({
      reasoningEffort: 'high',
      fastMode: false,
      reasoningEffortOverride: 'high',
      fastModeOverride: false,
    });
    // A start with nothing typed reads the options rather than sending, and
    // hands on the same picks.
    expect(inputRef.current?.getModelOptions()).toMatchObject({
      reasoningEffortOverride: 'high',
      fastModeOverride: false,
    });
    expect(mocks.mutate).not.toHaveBeenCalled();
  });

  // DALTON-HOOK(automation-effort): ordinary retry omits saved thread effort unless explicitly picked for this retry
  it('omits saved thread reasoningEffort on getRetryModelOptions unless explicitly picked for this retry', () => {
    const onSend = vi.fn();
    const onPickReasoningEffort = vi.fn();
    const inputRef = createRef<ChatInputHandle>();
    render(tree({
      onSend,
      inputRef,
      tuning: { reasoningEffort: 'low', fastMode: false },
      onPickReasoningEffort,
    }));

    // Normal send/edit/regenerate still reads the saved thread effort
    expect(inputRef.current?.getModelOptions().reasoningEffort).toBe('low');

    // Ordinary retry without explicitly picking an effort omits saved thread effort
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBeNull();

    // Explicitly picking an effort for this retry submits it once, then resets
    fireEvent.click(screen.getByText('pick-high'));
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBe('high');
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBeNull();

    // DALTON-HOOK(automation-effort): picking an effort and then sending a normal message clears the retry pick
    fireEvent.click(screen.getByText('pick-high'));
    send('normal follow-up');
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBeNull();
  });

  // DALTON-HOOK(automation-effort): unsent effort picked before a failure or followed by draft typing is never submitted on retry
  it('ignores unsent effort picked before a failed turn or edited as a draft when retrying later', () => {
    const onSend = vi.fn();
    const onPickReasoningEffort = vi.fn();
    const inputRef = createRef<ChatInputHandle>();
    const { rerender } = render(tree({
      onSend,
      inputRef,
      tuning: { reasoningEffort: 'low', fastMode: false },
      onPickReasoningEffort,
      retryTargetId: null,
    }));

    // 1) Pick effort when no failed turn exists, never send -> later automation fails -> retry omits it
    fireEvent.click(screen.getByText('pick-high'));
    rerender(tree({
      onSend,
      inputRef,
      tuning: { reasoningEffort: 'high', fastMode: false },
      onPickReasoningEffort,
      retryTargetId: 'err-run-1',
    }));
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBeNull();

    // DALTON-HOOK(automation-effort): switching retry targets or typing into draft invalidates earlier retry effort pick
    // 2) Pick effort on err-run-1, never retry -> a newer failure err-run-2 arrives -> retry omits it
    fireEvent.click(screen.getByText('pick-high'));
    rerender(tree({
      onSend,
      inputRef,
      tuning: { reasoningEffort: 'high', fastMode: false },
      onPickReasoningEffort,
      retryTargetId: 'err-run-2',
    }));
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBeNull();

    // 3) Pick effort on err-run-2, then type into draft (never sent) -> retry omits it
    fireEvent.click(screen.getByText('pick-high'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'unsent draft' } });
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBeNull();

    // 4) Explicitly pick effort right for err-run-2 without composing -> submits once, then resets
    fireEvent.click(screen.getByText('pick-high'));
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBe('high');
    expect(inputRef.current?.getRetryModelOptions?.().reasoningEffort).toBeNull();
  });

  // DALTON-HOOK(automation-effort): unsent Antigravity effort variant switch does not submit llm_model on later automation retry
  it('omits unsent Antigravity effort variant model on automation retry unless explicitly picked for that retry', () => {
    const onSend = vi.fn();
    const inputRef = createRef<ChatInputHandle>();
    let hostModel = 'gemini-4-neon-medium-oauth';
    let hostSendModel: string | null = null;
    const onPickModel = vi.fn((next: string) => {
      hostModel = next;
      hostSendModel = next;
    });

    const renderWithHost = (retryTargetId: string | null) => tree({
      onSend,
      inputRef,
      model: hostModel,
      sendModel: hostSendModel,
      onPickModel,
      tuning: { reasoningEffort: null, fastMode: false },
      retryTargetId,
    });

    const { rerender } = render(renderWithHost(null));

    // DALTON-HOOK(automation-effort): 1) switch Antigravity effort before failure without sending -> later retry omits llm_model
    fireEvent.click(screen.getByText('pick-high'));
    expect(onPickModel).toHaveBeenCalledWith('gemini-4-neon-high-oauth');
    rerender(renderWithHost('err-ag-1'));
    expect(inputRef.current?.getModelOptions().model).toBe('gemini-4-neon-high-oauth');
    expect(inputRef.current?.getRetryModelOptions?.().model).toBeNull();

    // 2) Switch Antigravity effort on err-ag-1, never retry -> newer failure err-ag-2 arrives -> retry omits llm_model
    fireEvent.click(screen.getByText('pick-high'));
    rerender(renderWithHost('err-ag-2'));
    expect(inputRef.current?.getRetryModelOptions?.().model).toBeNull();

    // 3) Switch Antigravity effort on err-ag-2, then type into draft -> retry omits llm_model
    fireEvent.click(screen.getByText('pick-high'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'unsent draft' } });
    expect(inputRef.current?.getRetryModelOptions?.().model).toBeNull();

    // DALTON-HOOK(automation-effort): 4) explicit Antigravity effort pick for active retry submits variant once, then omits it
    fireEvent.click(screen.getByText('pick-high'));
    expect(inputRef.current?.getRetryModelOptions?.().model).toBe('gemini-4-neon-high-oauth');
    expect(inputRef.current?.getRetryModelOptions?.().model).toBeNull();

    // 5) Explicit model-menu switch (not effort switch) still submits the picked model on retry
    fireEvent.click(screen.getByText('pick-model-carbon'));
    expect(onPickModel).toHaveBeenLastCalledWith('gemini-4-carbon-medium-oauth');
    rerender(renderWithHost('err-ag-2'));
    expect(inputRef.current?.getRetryModelOptions?.().model).toBe('gemini-4-carbon-medium-oauth');
  });
});
