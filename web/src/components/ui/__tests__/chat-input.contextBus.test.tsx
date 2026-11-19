import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, act, screen } from '@testing-library/react';
import { createRef } from 'react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import i18n from '@/i18n'; // DALTON-HOOK(chat-attachment-count)
import ChatInput, { type ChatInputHandle } from '../chat-input';
import { MAX_FILES } from '../chat-input.helpers';
import { ChatInputRegistry, ContextBus } from '@/lib/contextBus';
import type { WidgetContextSnapshot } from '@/pages/Dashboard/widgets/framework/contextSnapshot';
import { __resetWidgetContextRegistryForTests, useWidgetContextExport } from '@/pages/Dashboard/widgets/framework/contextSnapshot';
import { WidgetFrame } from '@/pages/Dashboard/widgets/framework/WidgetFrame';
import type { WidgetDefinition } from '@/pages/Dashboard/widgets/types';

const toastMock = vi.hoisted(() => vi.fn());

// Mock the API helpers chat-input fetches at mount — they hit network in real
// usage and we don't need them for deck behavior.
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ data: undefined, isLoading: false }),
}));

// DALTON-HOOK(chat-attachment-count): attachment mock for MAX_FILES widget-snapshot test
vi.mock('@/pages/ChatAgent/utils/api/attachments', () => ({
  beginAttachment: vi.fn().mockResolvedValue({ attachment_id: 'att-bus', mode: 'relay', max_bytes: 1 << 20 }),
  sendAttachmentContent: vi.fn().mockResolvedValue({ attachment_id: 'att-bus', state: 'ready', name: 'f.csv', size: 8, mime_type: 'text/csv', error: null }),
  signAttachmentParts: vi.fn(),
  completeAttachment: vi.fn(),
  checkAttachment: vi.fn(),
  deleteAttachment: vi.fn().mockResolvedValue(undefined),
  deleteAttachmentOnUnload: vi.fn(),
}));

vi.mock('../use-toast', () => ({
  useToast: () => ({ toast: toastMock }),
  toast: toastMock,
}));

const SNAP_A: WidgetContextSnapshot = {
  widget_type: 'markets.chart',
  widget_id: 'a',
  label: 'NVDA · 1d Chart',
  description: '120 daily bars',
  captured_at: '2026-04-26T11:42:08Z',
  text: '<widget-context>chart</widget-context>',
  data: {},
};

const SNAP_B: WidgetContextSnapshot = {
  widget_type: 'news.feed',
  widget_id: 'b',
  label: 'News headline',
  description: 'Reuters · 14m ago',
  captured_at: '2026-04-26T11:43:00Z',
  text: '<widget-context>news</widget-context>',
  data: {},
};

function renderInput(onSend = vi.fn(), refOpts?: { ref?: React.Ref<ChatInputHandle> }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChatInput ref={refOpts?.ref} onSend={onSend} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ChatInput — widget context deck', () => {
  beforeEach(() => {
    toastMock.mockReset(); // DALTON-HOOK(chat-attachment-count)
    ContextBus.__resetForTests();
    ChatInputRegistry.__resetForTests();
  });
  afterEach(() => {
    ContextBus.__resetForTests();
    ChatInputRegistry.__resetForTests();
  });

  it('does not render the deck when no snapshots are attached', () => {
    renderInput();
    expect(screen.queryByTestId('widget-context-deck')).toBeNull();
  });

  it('renders a card on ContextBus.attach', () => {
    renderInput();
    act(() => {
      ContextBus.attach(SNAP_A);
    });
    const deck = screen.getByTestId('widget-context-deck');
    expect(deck).toBeInTheDocument();
    expect(deck.textContent).toContain('NVDA · 1d Chart');
    expect(deck.textContent).toContain('1 in context');
  });

  it('addWidgetSnapshot ref method adds a card without using the bus', () => {
    const ref = createRef<ChatInputHandle>();
    renderInput(undefined, { ref });
    const handler = vi.fn();
    ContextBus.subscribe(handler);
    act(() => {
      ref.current?.addWidgetSnapshot(SNAP_A);
    });
    expect(screen.getByTestId('widget-context-deck').textContent).toContain('NVDA · 1d Chart');
    // No attach event was published — the ref method is local-only.
    expect(handler).not.toHaveBeenCalled();
  });

  it('removing a card publishes detach so other inputs drop it', () => {
    renderInput();
    act(() => {
      ContextBus.attach(SNAP_A);
      ContextBus.attach(SNAP_B);
    });
    const handler = vi.fn();
    ContextBus.subscribe(handler);
    const removeButtons = screen.getAllByLabelText(/remove from context/i);
    act(() => {
      fireEvent.click(removeButtons[0]);
    });
    // detach published with one of the widget ids
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0][0].type).toBe('detach');
    expect(['a', 'b']).toContain(handler.mock.calls[0][0].widgetId);
  });

  it('two mounted ChatInputs mirror the same deck via ContextBus', () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatInput onSend={vi.fn()} />
          <ChatInput onSend={vi.fn()} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    act(() => {
      ContextBus.attach(SNAP_A);
    });
    const decks = screen.getAllByTestId('widget-context-deck');
    expect(decks).toHaveLength(2);
    decks.forEach((d) => expect(d.textContent).toContain('NVDA'));
  });

  it('ContextBus.clear empties every mounted deck', () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatInput onSend={vi.fn()} />
          <ChatInput onSend={vi.fn()} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    act(() => {
      ContextBus.attach(SNAP_A);
      ContextBus.attach(SNAP_B);
    });
    expect(screen.getAllByTestId('widget-context-deck')).toHaveLength(2);
    act(() => {
      ContextBus.clear();
    });
    expect(screen.queryAllByTestId('widget-context-deck')).toHaveLength(0);
  });

  it('registers chat-input root in ChatInputRegistry on mount', () => {
    expect(ChatInputRegistry.size()).toBe(0);
    const { unmount } = renderInput();
    expect(ChatInputRegistry.size()).toBe(1);
    unmount();
    expect(ChatInputRegistry.size()).toBe(0);
  });

  it('renders 1 / 3 / 5 cards with stable peek geometry', () => {
    const { rerender, unmount } = renderInput();
    const more: WidgetContextSnapshot[] = [SNAP_A, SNAP_B, { ...SNAP_A, widget_id: 'c' }, { ...SNAP_A, widget_id: 'd' }, { ...SNAP_A, widget_id: 'e' }];

    // 1 card
    act(() => ContextBus.attach(more[0]));
    let deck = screen.getByTestId('widget-context-deck');
    let cards = deck.querySelectorAll('.widget-deck-card');
    expect(cards.length).toBe(1);
    expect((cards[0] as HTMLElement).style.transform).toContain('scale(1)');

    // +2 → 3 cards. Snapshot the per-index transforms.
    act(() => {
      ContextBus.attach(more[1]);
      ContextBus.attach(more[2]);
    });
    deck = screen.getByTestId('widget-context-deck');
    cards = deck.querySelectorAll('.widget-deck-card');
    expect(cards.length).toBe(3);

    // +2 more → 5 cards. Last card uses index-4 peek (24px translate).
    act(() => {
      ContextBus.attach(more[3]);
      ContextBus.attach(more[4]);
    });
    deck = screen.getByTestId('widget-context-deck');
    cards = deck.querySelectorAll('.widget-deck-card');
    expect(cards.length).toBe(5);
    // Top card (newest, index 0): no peek translate
    const indexed = Array.from(cards) as HTMLElement[];
    const top = indexed.find((c) => c.dataset.i === '0')!;
    expect(top.style.transform).toContain('translateY(0px)');
    expect(top.style.opacity).toBe('1');
    // Last card (index 4): 24px peek translate, lower opacity
    const last = indexed.find((c) => c.dataset.i === '4')!;
    expect(last.style.transform).toContain('translateY(24px)');
    expect(parseFloat(last.style.opacity)).toBeLessThan(1);

    rerender(<></>);
    unmount();
  });

  it('clear button publishes ContextBus.clear', () => {
    renderInput();
    act(() => {
      ContextBus.attach(SNAP_A);
      ContextBus.attach(SNAP_B);
    });
    const handler = vi.fn();
    ContextBus.subscribe(handler);
    act(() => {
      fireEvent.click(screen.getByText(/^Clear$/i));
    });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0][0].type).toBe('clear');
  });

  // DALTON-HOOK(chat-attachment-count): image-bearing snapshots count toward MAX_FILES and toast when full
  it('rejects an image-bearing widget snapshot with a clear toast when 10 files are already attached', async () => {
    const ref = createRef<ChatInputHandle>();
    const onSend = vi.fn();
    renderInput(onSend, { ref });

    const files = Array.from({ length: MAX_FILES }, (_, i) => new File(['a'], `f${i}.csv`, { type: 'text/csv' }));
    act(() => {
      ref.current?.addContext({ kind: 'attachment', files });
    });
    expect(ref.current?.getAttachmentCount()).toBe(MAX_FILES);

    const imgSnapshot: WidgetContextSnapshot = {
      ...SNAP_A,
      widget_id: 'img-snap',
      image_jpeg_data_url: 'data:image/jpeg;base64,AAAA',
    };
    let attached = true;
    act(() => {
      attached = ContextBus.attach(imgSnapshot);
    });
    expect(attached).toBe(false);
    expect(screen.queryByTestId('widget-context-deck')).toBeNull();
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'destructive',
        title: expect.stringContaining(`${MAX_FILES}`),
      }),
    );

    // Chinese copy also displays clearly
    try {
      await act(() => i18n.changeLanguage('zh-CN'));
      toastMock.mockReset();
      let imperativeResult: boolean | undefined;
      act(() => {
        imperativeResult = ref.current?.addWidgetSnapshot(imgSnapshot);
      });
      expect(imperativeResult).toBe(false);
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'destructive',
          title: expect.stringContaining('10 个'),
        }),
      );
    } finally {
      await act(() => i18n.changeLanguage('en-US'));
    }

    // Non-image widget snapshot does not carry an image item and is still allowed
    act(() => {
      expect(ContextBus.attach(SNAP_B)).toBe(true);
    });
    expect(screen.getByTestId('widget-context-deck').textContent).toContain('News headline');
  });

  // DALTON-HOOK(chat-attachment-count): WidgetFrame must not show "Added to context" when ChatInput rejects an over-limit snapshot
  it('WidgetFrame skips Added to context toast when ChatInput rejects snapshot at MAX_FILES, and shows it when accepted', async () => {
    const ref = createRef<ChatInputHandle>();
    const onSend = vi.fn();
    const imgSnapshot: WidgetContextSnapshot = {
      ...SNAP_A,
      widget_id: 'wf-chart-1',
      image_jpeg_data_url: 'data:image/jpeg;base64,AAAA',
    };
    function TestExporter() {
      useWidgetContextExport('wf-chart-1', { full: () => imgSnapshot });
      return <div>chart body</div>;
    }
    const widgetDef = {
      type: 'test.chart',
      titleKey: 'Chart',
      source: 'builtin',
      category: 'markets',
      defaultSize: { w: 4, h: 3 },
      minSize: { w: 2, h: 2 },
      component: () => null,
    } as unknown as WidgetDefinition<unknown>;

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    try {
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <WidgetFrame
              definition={widgetDef}
              instance={{ id: 'wf-chart-1', type: 'test.chart', config: {} }}
              editMode={false}
              onRemove={vi.fn()}
              onDuplicate={vi.fn()}
              onOpenSettings={vi.fn()}
            >
              <TestExporter />
            </WidgetFrame>
            <ChatInput ref={ref} onSend={onSend} />
          </MemoryRouter>
        </QueryClientProvider>,
      );

      // Fill ChatInput to 10 files -> click attach -> limit toast only, NO "Added to context" toast
      const tenFiles = Array.from({ length: MAX_FILES }, (_, i) => new File(['a'], `f${i}.csv`, { type: 'text/csv' }));
      await act(async () => {
        ref.current?.addContext({ kind: 'attachment', files: tenFiles });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(ref.current?.getAttachmentCount()).toBe(MAX_FILES);

      toastMock.mockReset();
      const attachBtn = document.querySelector('.widget-card__attach') as HTMLButtonElement;
      expect(attachBtn).not.toBeNull();
      expect(attachBtn.disabled).toBe(false);
      await act(async () => {
        fireEvent.click(attachBtn);
        await Promise.resolve();
      });

      expect(screen.queryByTestId('widget-context-deck')).toBeNull();
      expect(toastMock).toHaveBeenCalledTimes(1);
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'destructive',
          title: expect.stringContaining(`${MAX_FILES}`),
        }),
      );

      // Remove one file via its tile remove button -> room < MAX_FILES
      const tileGroup = screen.getByText('f0.csv').closest('[role="group"]');
      const removeBtn = tileGroup?.querySelector('button');
      expect(removeBtn).not.toBeNull();
      await act(async () => {
        fireEvent.click(removeBtn!);
      });
      expect(ref.current?.getAttachmentCount()).toBe(MAX_FILES - 1);

      toastMock.mockReset();
      await act(async () => {
        fireEvent.click(attachBtn);
        await Promise.resolve();
      });
      expect(screen.getByTestId('widget-context-deck')).toBeInTheDocument();
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          title: expect.stringMatching(/Added to context|dashboard\.widgets\.frame\.contextAttached/i),
        }),
      );
    } finally {
      __resetWidgetContextRegistryForTests();
    }
  });

  // DALTON-HOOK(chat-attachment-count): if files + screenshots exceed MAX_FILES at send time,
  // refuse to send and keep all files and the widget deck intact.
  it('blocks sending past MAX_FILES with toast without clearing files or widget deck', async () => {
    const ref = createRef<ChatInputHandle>();
    const onSend = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatInput ref={ref} onSend={onSend} chartImage="data:image/png;base64,CHART" />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // Attach 1 image widget snapshot + 9 files (10 total before counting chartImage -> 11 total)
    const imgSnapshot: WidgetContextSnapshot = {
      ...SNAP_A,
      widget_id: 'send-guard-img',
      image_jpeg_data_url: 'data:image/jpeg;base64,AAAA',
    };
    act(() => {
      ContextBus.attach(imgSnapshot);
    });
    const files = Array.from({ length: MAX_FILES - 1 }, (_, i) => new File(['a'], `f${i}.csv`, { type: 'text/csv' }));
    act(() => {
      ref.current?.addContext({ kind: 'attachment', files });
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(ref.current?.getAttachmentCount()).toBe(MAX_FILES + 1);

    toastMock.mockReset();
    act(() => {
      expect(ref.current?.send()).toBe(false);
    });
    expect(onSend).not.toHaveBeenCalled();
    expect(ref.current?.getAttachmentCount()).toBe(MAX_FILES + 1);
    expect(screen.getByTestId('widget-context-deck')).toBeInTheDocument();
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'destructive',
        title: expect.stringContaining(`${MAX_FILES}`),
      }),
    );
  });
});
