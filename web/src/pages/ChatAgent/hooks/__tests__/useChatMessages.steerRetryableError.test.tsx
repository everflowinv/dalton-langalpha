/**
 * DALTON-HOOK(chat-large-upload): End-to-end test across real frontend SSE
 * parsing (postSSEStream -> streamFetch), useChatMessages (handleSendSteering),
 * useComposerGiveBack, ErrorDisplay, and ChatInput when a steer encounters a
 * structured recoverable 503 storage error frame from handle_workflow_error.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useCallback, useEffect, useRef } from 'react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useChatMessages } from '../useChatMessages';
import { useComposerGiveBack } from '../../components/chatView/useComposerGiveBack';
import ChatInput, { type ChatInputHandle } from '@/components/ui/chat-input';
import { ErrorBanner } from '@/components/ui/error-banner';
import { attachmentSendParts } from '../../utils/fileUpload';
import type { ComposerSend } from '@/components/ui/chat-input.types';
import zhCN from '@/locales/zh-CN.json';

function lookupZh(key: string): string {
  const parts = key.split('.');
  let cur: unknown = zhCN;
  for (const p of parts) {
    if (!cur || typeof cur !== 'object') return key;
    cur = (cur as Record<string, unknown>)[p];
  }
  return typeof cur === 'string' ? cur : key;
}

vi.mock('react-i18next', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-i18next')>();
  return {
    ...actual,
    useTranslation: () => ({ t: (k: string) => lookupZh(k), i18n: { language: 'zh-CN' } }),
    Trans: ({ i18nKey }: { i18nKey: string }) => lookupZh(i18nKey),
  };
});

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/attachmentUpload', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/attachmentUpload')>();
  return {
    ...actual,
    stageAttachment: vi.fn().mockResolvedValue('att-staged-503'),
    discardAttachment: vi.fn(),
  };
});

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      post: vi.fn().mockResolvedValue({ data: {} }),
      put: vi.fn().mockResolvedValue({ data: {} }),
      get: vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/features')) return { data: [] };
        return { data: {} };
      }),
      delete: vi.fn().mockResolvedValue({ data: {} }),
    },
  };
});

vi.mock('../../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/api')>();
  return {
    ...actual,
    getSkills: vi.fn().mockResolvedValue([]),
    getModelMetadata: vi.fn().mockResolvedValue({}),
    getWorkflowStatus: vi.fn().mockResolvedValue({
      status: 'completed',
      can_reconnect: false,
      pending_steers: [],
      failed_steers: [],
    }),
    replayThreadHistory: vi.fn().mockImplementation(async (_tid: string, onEvent: (e: Record<string, unknown>) => void) => {
      onEvent({ event: 'history_page', first_turn_index: 0, has_more: false });
      onEvent({ event: 'replay_done', thread_id: 'th-steer-sse', latest_run_id: null });
    }),
    fetchThreadTurns: vi.fn().mockResolvedValue({ turns: [] }),
    getThreadFeedback: vi.fn().mockResolvedValue([]),
    watchThread: vi.fn().mockResolvedValue(() => {}),
  };
});

function sseResponse(frames: Array<{ id?: number; event: string; data: Record<string, unknown> }>): Response {
  const encoder = new TextEncoder();
  const bytes = encoder.encode(
    frames
      .map((f) => `${f.id !== undefined ? `id: ${f.id}\n` : ''}event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`)
      .join(''),
  );
  let sent = false;
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'Content-Type': 'text/event-stream' }),
    body: {
      getReader: () => ({
        read: async () => {
          if (sent) return { done: true, value: undefined };
          sent = true;
          return { done: false, value: bytes };
        },
        cancel: async () => {},
      }),
    },
  } as unknown as Response;
}

describe('steer transient 503 SSE flow preserves attachments and prompts retry', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it.each([
    {
      label: 'upstream object read failure',
      error: 'Temporarily unable to read attachment from storage. Please retry.',
    },
    {
      label: 'multipart part upload failure',
      error: 'Failed to stage inline attachment part. Please retry.',
    },
    {
      label: 'multipart complete failure',
      error: 'Failed to assemble staged inline attachment.',
    },
  ])(
    'routes backend recoverable steer SSE error ($label) through streamFetch -> useChatMessages -> ChatInput',
    async ({ error }) => {
    // Keep the initial turn stream open so isLoading stays true when steering fires
    let releaseInitialTurn: () => void = () => {};
    const postedBodies: Array<Record<string, unknown>> = [];

    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (!url.includes('/api/v1/threads/th-steer-sse/messages')) {
        return new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      const parsed = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      postedBodies.push(parsed);

      if (postedBodies.length === 1) {
        const encoder = new TextEncoder();
        const firstChunk = encoder.encode(
          `id: 1\nevent: metadata\ndata: ${JSON.stringify({ thread_id: 'th-steer-sse', run_id: 'run-open-1', turn_index: 0 })}\n\n`,
        );
        const endChunk = encoder.encode(
          `id: 2\nevent: message_chunk\ndata: ${JSON.stringify({ role: 'assistant', content: 'done', finish_reason: 'stop' })}\n\n`,
        );
        let step = 0;
        let resolveHold: (() => void) | null = null;
        releaseInitialTurn = () => {
          resolveHold?.();
        };
        return {
          ok: true,
          status: 200,
          headers: new Headers({ 'Content-Type': 'text/event-stream' }),
          body: {
            getReader: () => ({
              read: async () => {
                if (step === 0) {
                  step = 1;
                  return { done: false, value: firstChunk };
                }
                if (step === 1) {
                  await new Promise<void>((r) => {
                    resolveHold = r;
                  });
                  step = 2;
                  return { done: false, value: endChunk };
                }
                return { done: true, value: undefined };
              },
              cancel: async () => {
                resolveHold?.();
              },
            }),
          },
        } as unknown as Response;
      }

      if (postedBodies.length === 2) {
        // Exact wire payload emitted by backend handle_workflow_error on steer_only HTTPException(503)
        return sseResponse([
          {
            id: 1,
            event: 'error',
            data: {
              thread_id: 'th-steer-sse',
              error,
              type: 'workflow_error',
              error_type: 'transient_error',
              error_class: 'HTTPException',
              is_recoverable: true,
              retryable: true,
              recovery: 'resend',
              status_code: 503,
            },
          },
        ]);
      }

      // 3rd POST: user retries the steer without re-attaching -> steering_accepted
      return sseResponse([
        {
          id: 1,
          event: 'steering_accepted',
          data: {
            position: 0,
            attachments: [
              {
                name: 'report.pdf',
                type: 'pdf',
                mime_type: 'application/pdf',
                size: 128,
                status: 'uploaded',
                upload_id: 'up-retry-ok',
              },
            ],
          },
        },
      ]);
    });
    vi.stubGlobal('fetch', fetchMock);

    let chatHook: ReturnType<typeof useChatMessages> | null = null;
    const chatInputRef = { current: null as ChatInputHandle | null };

    function Harness() {
      const ref = useRef<ChatInputHandle>(null);
      useEffect(() => {
        chatInputRef.current = ref.current;
      });
      const chat = useChatMessages('ws-1', 'th-steer-sse', { platform: 'web' });
      chatHook = chat;
      useComposerGiveBack(ref, chat);

      const handleSend = useCallback((send: ComposerSend) => {
        const attached = attachmentSendParts(send.files);
        return chat.handleSendMessage(
          send.message,
          attached.contexts.length > 0 ? attached.contexts : null,
          attached.meta.length > 0 ? attached.meta : null,
          {
            ...send.modelOptions,
            draft: send,
          },
        );
      }, [chat]);

      return (
        <div>
          {chat.messageError && (!chat.isLoading || (typeof chat.messageError === 'object' && chat.messageError.kind === 'notice')) && (
            <ErrorBanner error={chat.messageError} />
          )}
          <ChatInput
            ref={ref}
            onSend={handleSend}
            isLoading={chat.isLoading}
          />
        </div>
      );
    }

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(chatHook?.isLoadingHistory).toBe(false));

    // Start initial turn so isLoading transitions to true
    await act(async () => {
      void chatHook!.handleSendMessage('Analyze Q3');
    });
    await waitFor(() => expect(chatHook!.isLoading).toBe(true));

    // Attach report.pdf to ChatInput (stages via /api/v1/chat/attachments -> att-staged-503)
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileInput).not.toBeNull();
    const pdfFile = new File([new Uint8Array(128).fill(7)], 'report.pdf', { type: 'application/pdf' });
    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [pdfFile] } });
    });
    await waitFor(() => expect(screen.getByText('report.pdf')).toBeInTheDocument());

    // Type steer message and send via ChatInput
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    await act(async () => {
      fireEvent.change(textarea, { target: { value: 'Also read attached report' } });
    });
    const sendButton = screen.getByRole('button', { name: '发送消息' });
    await act(async () => {
      fireEvent.click(sendButton);
    });

    // Real SSE recoverable 503 error reaches ErrorDisplay with "暂时失败，可重试。"
    // and ChatInput preserves both the text and the report.pdf attachment for retry.
    await waitFor(() => {
      expect(screen.getByText('暂时失败，可重试。')).toBeInTheDocument();
      expect(screen.getByText('report.pdf')).toBeInTheDocument();
    });
    expect(chatHook!.messageError).toEqual({
      message: '暂时失败，可重试。',
      kind: 'notice',
    });
    expect(textarea.value).toBe('Also read attached report');
    // Unaccepted steer bubble was returned cleanly rather than stuck as 409 refused
    expect(
      chatHook!.messages.some((m) => m.role === 'user' && (m as { queueRefused?: boolean }).queueRefused === true),
    ).toBe(false);

    // Retry immediately by clicking Send again without re-attaching the file
    const retrySendButton = screen.getByRole('button', { name: '发送消息' });
    await act(async () => {
      fireEvent.click(retrySendButton);
    });

    await waitFor(() => expect(postedBodies).toHaveLength(3));
    expect(postedBodies[2].steer_only).toBe(true);
    expect(postedBodies[2].additional_context).toEqual([
      expect.objectContaining({
        type: 'pdf',
        attachment_id: 'att-staged-503',
      }),
    ]);

    await act(async () => {
      releaseInitialTurn();
    });
  });
});
