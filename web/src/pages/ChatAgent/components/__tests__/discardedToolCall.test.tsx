/**
 * Tests for discarded pre-finish tool calls (output_cap, malformed_call, or
 * interrupted), localized copy + a11y labels, and same-frame text + tool_calls.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@testing-library/jest-dom';
import zhCN from '@/locales/zh-CN.json';
import enUS from '@/locales/en-US.json';
import type { ChatMessage } from '@/types/chat';
import type { SSEEvent } from '../../session/types';
import { createStreamEventProcessor } from '../../session/stream/processStreamEvent';
import { buildRenderBlocks } from '../messageList/buildRenderBlocks';
import { ToolCallLiveRow } from '../messageList/InlineActivityRows';
import ActivityBlock from '../ActivityBlock';
import DetailPanel from '../DetailPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => {
      if (opts && typeof opts === 'object') {
        let out = key;
        for (const [k, v] of Object.entries(opts)) {
          out = out.replace(new RegExp(`{{\\s*${k}\\s*}}`, 'g'), String(v));
        }
        return out;
      }
      return key;
    },
  }),
}));

vi.mock('../Markdown', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
  CodeBlock: ({ children }: { children?: React.ReactNode }) => <pre>{children}</pre>,
}));

vi.mock('../charts/InlineArtifactCards', () => ({
  INLINE_ARTIFACT_TOOLS: new Set<string>(),
  isInlineArtifactReady: () => false,
  INLINE_ARTIFACT_MAP: {},
  InlineStockPriceCard: () => null,
  InlineCompanyOverviewCard: () => null,
  InlineMarketIndicesCard: () => null,
  InlineSectorPerformanceCard: () => null,
  InlineMarketOverviewCard: () => null,
  InlineSecFilingCard: () => null,
  InlineStockScreenerCard: () => null,
  InlineWebSearchCard: () => null,
}));

vi.mock('../charts/InlineAutomationCards', () => ({ InlineAutomationCard: () => null }));
vi.mock('../charts/InlinePreviewCard', () => ({ InlinePreviewCard: () => null }));
vi.mock('../ToolCallDetailView', () => ({ default: () => <div data-testid="detail-body" /> }));

const viewport = vi.hoisted(() => ({ mobile: false }));
vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => viewport.mobile }));

const FALLBACK_TRUNCATED = '工具调用未执行：回复超出输出上限被截断';

function setupStream() {
  const BUBBLE = 'a1';
  let messages: ChatMessage[] = [{
    id: BUBBLE,
    role: 'assistant',
    content: '',
    isStreaming: true,
    contentSegments: [],
    reasoningProcesses: {},
    toolCallProcesses: {},
  } as unknown as ChatMessage];
  const setMessages = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
    messages = updater(messages);
  };
  const rt = {
    setMessages,
    queueMessages: setMessages,
    flushMessages: vi.fn(),
    updateSubagentCard: vi.fn(),
    lastEventIdRef: { current: null },
    threadIdRef: { current: 'thread-1' },
    threadId: 'thread-1',
    setThreadId: vi.fn(),
    pendingPTCBackfillRef: { current: new Map() },
    subagentHistory: { toolCalls: new Map() },
    t: (k: string) => k,
  };
  const deps = { clearModelStatus: () => {}, armReportBack: vi.fn(), refreshWorkspaces: vi.fn(), refreshClaudeCodePlan: vi.fn() };
  const refs = {
    contentOrderCounterRef: { current: 0 },
    currentReasoningIdRef: { current: null },
    currentToolCallIdRef: { current: null },
    subagentStateRefs: {},
    isReconnect: false,
  };
  type Processor = Parameters<typeof createStreamEventProcessor>;
  const process = createStreamEventProcessor(
    rt as unknown as Processor[0],
    deps as unknown as Processor[1],
    BUBBLE,
    refs as unknown as Processor[3],
    (event) => (event.agent as string) ?? null,
  );
  const bubble = () => messages.find((m) => m.id === BUBBLE) as unknown as Record<string, unknown>;
  return { process, bubble };
}

const renderWithQuery = (ui: React.ReactElement) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
};

describe('discarded pre-finish tool calls & same-frame text', () => {
  it('renders inline text carried on the same tool_calls SSE frame ahead of the tool call', () => {
    const { process, bubble } = setupStream();

    process({
      event: 'tool_calls',
      _eventId: 10,
      content: '我将写入文件',
      content_type: 'text',
      tool_calls: [{ id: 'ag-call-1', name: 'Write', args: { file_path: 'model.py', content: 'x = 1' } }],
    } as unknown as SSEEvent);

    expect(bubble().content).toBe('我将写入文件');
    const segments = bubble().contentSegments as Array<{ type: string; content?: string; toolCallId?: string; order: number }>;
    expect(segments.map((s) => s.type)).toEqual(['text', 'tool_call']);
    expect(segments[0].content).toBe('我将写入文件');
    expect(segments[1].toolCallId).toBe('ag-call-1');
    expect(segments[0].order).toBeLessThan(segments[1].order);

    process({
      event: 'tool_call_result',
      tool_call_id: 'ag-call-1',
      status: 'error',
      discarded: true,
      discarded_reason: 'output_cap',
      content: FALLBACK_TRUNCATED,
      content_type: 'text',
    } as SSEEvent);

    const proc = (bubble().toolCallProcesses as Record<string, Record<string, unknown>>)['ag-call-1'];
    expect(proc.isFailed).toBe(true);
    expect(proc.isInProgress).toBe(false);
    expect((proc.toolCallResult as { content?: string; status?: string; discarded?: boolean; discarded_reason?: string })).toMatchObject({
      content: FALLBACK_TRUNCATED,
      status: 'error',
      discarded: true,
      discarded_reason: 'output_cap',
    });

    const { blocks } = buildRenderBlocks(segments as never, {
      reasoningProcesses: {},
      toolCallProcesses: bubble().toolCallProcesses as never,
      isStreaming: true,
    });
    expect(blocks[0].type).toBe('text');
    const activity = blocks[1];
    expect(activity.type).toBe('activity');
    if (activity.type === 'activity') {
      expect(activity.items[0]._liveState).toBe('failed');
    }
  });

  it.each(['output_cap', 'malformed_call', 'interrupted'] as const)(
    'uses localized reason key "%s" and toolCallNotExecuted a11y label across live row, timeline row, and detail header',
    (reasonKey) => {
      const expectedKey = `toolArtifact.discarded.${reasonKey}`;
      const proc = {
        type: 'tool_call' as const,
        id: 'ag-call-1',
        toolCallId: 'ag-call-1',
        toolName: 'Write',
        toolCall: { id: 'ag-call-1', name: 'Write', args: { file_path: 'model.py' } },
        toolCallResult: {
          content: FALLBACK_TRUNCATED,
          content_type: 'text',
          tool_call_id: 'ag-call-1',
          status: 'error',
          discarded: true,
          discarded_reason: reasonKey,
        },
        isInProgress: false,
        isComplete: true,
        isFailed: true,
        _liveState: 'failed' as const,
        order: 1,
      };

      // Live zone row
      const liveView = render(<ToolCallLiveRow tc={proc as never} liveState="failed" />);
      expect(liveView.getByText(new RegExp(expectedKey))).toBeInTheDocument();
      expect(liveView.getByLabelText('toolArtifact.a11y.toolCallNotExecuted')).toBeInTheDocument();
      expect(liveView.queryByLabelText('toolArtifact.a11y.toolCallFailed')).toBeNull();
      liveView.unmount();

      // Collapsed/archived timeline row (FileToolGroupRow path for Write)
      const settledItem = { ...proc, _liveState: 'completed' as const };
      const { container, unmount } = render(<ActivityBlock items={[settledItem] as never} isStreaming={false} />);
      fireEvent.click(screen.getByRole('button', { name: /toolArtifact/i }));
      const failedRow = container.querySelector('.titem.failed');
      expect(failedRow).not.toBeNull();
      expect(failedRow!.textContent).toContain(expectedKey);
      expect(failedRow!.querySelector('.nrow-badge')!.getAttribute('aria-label')).toBe('toolArtifact.a11y.toolCallNotExecuted');
      expect(screen.queryByLabelText('toolArtifact.a11y.toolCallFailed')).toBeNull();
      unmount();

      // Mobile detail sheet header
      viewport.mobile = true;
      try {
        render(<DetailPanel toolCallProcess={proc as never} />);
        expect(screen.getByLabelText('toolArtifact.a11y.toolCallNotExecuted')).toBeInTheDocument();
        expect(screen.queryByLabelText('toolArtifact.a11y.toolCallFailed')).toBeNull();
      } finally {
        viewport.mobile = false;
      }
    },
  );

  it('ships matching zh-CN and en-US copy without auto-retry phrasing', () => {
    expect(zhCN.toolArtifact.a11y.toolCallNotExecuted).toBe('工具调用未执行');
    expect(enUS.toolArtifact.a11y.toolCallNotExecuted).toBe('Tool call not executed');
    expect(zhCN.toolArtifact.discarded).toEqual({
      output_cap: '工具调用未执行：回复超出输出上限被截断',
      malformed_call: '工具调用未执行：调用格式错误',
      interrupted: '工具调用未执行：模型输出中断',
    });
    expect(enUS.toolArtifact.discarded).toEqual({
      output_cap: 'Tool call not executed: reply truncated at output limit',
      malformed_call: 'Tool call not executed: malformed tool call',
      interrupted: 'Tool call not executed: model output interrupted',
    });
    expect(enUS.chat.autoRetryExhausted).toBe("Couldn't finish this reply after a few tries. Try again in a moment.");
  });

  it('ignores discarded-looking prefix in ordinary Bash output without discarded=true & status=error', () => {
    const ordinarySuccess = {
      type: 'tool_call' as const,
      id: 'bash-ok-1',
      toolCallId: 'bash-ok-1',
      toolName: 'Bash',
      toolCall: { id: 'bash-ok-1', name: 'Bash', args: { command: 'cat log.txt' } },
      toolCallResult: {
        content: `${FALLBACK_TRUNCATED}\nlog line 2`,
        content_type: 'text',
        tool_call_id: 'bash-ok-1',
        status: 'success',
      },
      isInProgress: false,
      isComplete: true,
      isFailed: false,
      _liveState: 'completed' as const,
      order: 1,
    };

    const { container, unmount } = renderWithQuery(<ActivityBlock items={[ordinarySuccess] as never} isStreaming={false} />);
    fireEvent.click(screen.getByRole('button', { name: /toolArtifact/i }));
    expect(container.querySelector('.titem.failed')).toBeNull();
    expect(screen.queryByText(new RegExp(FALLBACK_TRUNCATED))).toBeNull();
    unmount();

    const ordinaryErrorWithoutDiscardedFlag = {
      ...ordinarySuccess,
      id: 'bash-err-1',
      toolCallId: 'bash-err-1',
      toolCallResult: {
        content: `${FALLBACK_TRUNCATED}\nexit code 1`,
        content_type: 'text',
        tool_call_id: 'bash-err-1',
        status: 'error',
      },
      isFailed: true,
    };
    const errView = renderWithQuery(<ActivityBlock items={[ordinaryErrorWithoutDiscardedFlag] as never} isStreaming={false} />);
    fireEvent.click(screen.getByRole('button', { name: /toolArtifact/i }));
    const failedRow = errView.container.querySelector('.titem.failed');
    expect(failedRow).not.toBeNull();
    expect(failedRow!.querySelector('.nrow-badge')!.getAttribute('aria-label')).toBe('toolArtifact.a11y.toolCallFailed');
    expect(errView.queryByText(new RegExp(FALLBACK_TRUNCATED))).toBeNull();
    errView.unmount();

    const discardedFlagWithSuccessStatus = {
      ...ordinarySuccess,
      id: 'bash-weird-1',
      toolCallId: 'bash-weird-1',
      toolCallResult: {
        content: FALLBACK_TRUNCATED,
        content_type: 'text',
        tool_call_id: 'bash-weird-1',
        status: 'success',
        discarded: true,
        discarded_reason: 'output_cap',
      },
      isFailed: true,
    };
    const weirdView = renderWithQuery(<ToolCallLiveRow tc={discardedFlagWithSuccessStatus as never} liveState="failed" />);
    expect(weirdView.queryByText(/toolArtifact\.discarded\.output_cap/)).toBeNull();
    expect(weirdView.queryByText(new RegExp(FALLBACK_TRUNCATED))).toBeNull();
    expect(weirdView.getByLabelText('toolArtifact.a11y.toolCallFailed')).toBeInTheDocument();
  });
});
