import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient } from '@tanstack/react-query';
import { renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { isSharedFileBodyQueryKey } from '@/lib/dalton/shareFileGuard';
import type { ApiAdapter } from '../../ChatAgent/components/filePanel/types';
import type { toast as ToastFn } from '@/components/ui/use-toast';
import type {
  getSharedFiles as GetSharedFilesFn,
  readSharedFile as ReadSharedFileFn,
  replaySharedThread as ReplaySharedThreadFn,
  resolveSharedFile as ResolveSharedFileFn,
  downloadSharedFile as DownloadSharedFileFn,
  servedBytes as ServedBytesFn,
  servedObjectUrl as ServedObjectUrlFn,
  SharedThreadMetadata,
  SSEEvent,
} from '../api';
import type MermaidDiagramComponent from '../../ChatAgent/components/markdown/MermaidDiagram';

const h = vi.hoisted(() => ({
  mermaidSpy: vi.fn<typeof MermaidDiagramComponent>(({ code }) => (
    <div data-testid="mock-mermaid-svg">
      <svg data-mermaid="true">
        <text>{code}</text>
      </svg>
    </div>
  )),
  replayMock: vi.fn<typeof ReplaySharedThreadFn>(),
  getSharedFilesMock: vi.fn<typeof GetSharedFilesFn>(),
  readSharedFileMock: vi.fn<typeof ReadSharedFileFn>(),
  resolveSharedFileMock: vi.fn<typeof ResolveSharedFileFn>(),
  downloadSharedFileMock: vi.fn<typeof DownloadSharedFileFn>(),
  servedObjectUrlMock: vi.fn<typeof ServedObjectUrlFn>(),
  servedBytesMock: vi.fn<typeof ServedBytesFn>(),
  toastSpy: vi.fn<typeof ToastFn>(),
  capturedApiAdapter: null as ApiAdapter | null,
}));

vi.mock('react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router')>();
  return {
    ...actual,
    Link: ({ children, to }: { children?: React.ReactNode; to: string }) => (
      <a href={to}>{children}</a>
    ),
  };
});

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ isLoggedIn: false, isInitialized: true, userId: null }),
}));

vi.mock('../../../contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}));

vi.mock('../../ChatAgent/hooks/usePTCDispatchStatus', () => ({
  DispatchStatusProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/ui/use-toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/ui/use-toast')>();
  return {
    ...actual,
    toast: (...args: Parameters<typeof ToastFn>) => {
      h.toastSpy(...args);
      return actual.toast(...args);
    },
  };
});

vi.mock('../../ChatAgent/components/FilePanel', () => ({
  default: ({ apiAdapter }: { apiAdapter?: ApiAdapter }) => {
    h.capturedApiAdapter = apiAdapter ?? null;
    return <div data-testid="file-panel" />;
  },
}));

vi.mock('../../ChatAgent/components/markdown/MermaidDiagram', () => ({
  default: (props: React.ComponentProps<typeof MermaidDiagramComponent>) => h.mermaidSpy(props),
}));

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    replaySharedThread: (...args: Parameters<typeof ReplaySharedThreadFn>) => h.replayMock(...args),
    getSharedFiles: (...args: Parameters<typeof GetSharedFilesFn>) => h.getSharedFilesMock(...args),
    readSharedFile: (...args: Parameters<typeof ReadSharedFileFn>) => h.readSharedFileMock(...args),
    resolveSharedFile: (...args: Parameters<typeof ResolveSharedFileFn>) => h.resolveSharedFileMock(...args),
    downloadSharedFile: (...args: Parameters<typeof DownloadSharedFileFn>) => h.downloadSharedFileMock(...args),
    servedObjectUrl: (...args: Parameters<typeof ServedObjectUrlFn>) => h.servedObjectUrlMock(...args),
    servedBytes: (...args: Parameters<typeof ServedBytesFn>) => h.servedBytesMock(...args),
  };
});

import SharedChatView from '../SharedChatView';
import Markdown from '../../ChatAgent/components/Markdown';
import * as sharedImageCache from '@/lib/dalton/sharedImageCache';
import { streamFetch } from '../../ChatAgent/utils/api/transport';

function makeMetadata(allowFiles: boolean, overrides: Partial<SharedThreadMetadata> = {}): SharedThreadMetadata {
  return {
    kind: 'thread',
    thread_id: 'thread-1',
    title: 'Safe Shared Title',
    msg_type: 'chat',
    created_at: '2026-11-27T00:00:00Z',
    updated_at: '2026-11-27T00:00:00Z',
    workspace_name: 'Research',
    permissions: { allow_files: allowFiles, allow_download: false },
    ...overrides,
  };
}

describe('SharedChatView redaction, paging, downgrade, and regression', () => {
  const originalRevoke = URL.revokeObjectURL;

  beforeEach(() => {
    vi.clearAllMocks();
    h.capturedApiAdapter = null;
    URL.revokeObjectURL = vi.fn<typeof URL.revokeObjectURL>();
    h.getSharedFilesMock.mockResolvedValue({ path: '.', files: [], source: 'live' });
    h.servedObjectUrlMock.mockResolvedValue('blob:served-img');
  });

  afterEach(() => {
    sharedImageCache.revokeSharedImageScope('tok-redacted');
    sharedImageCache.revokeSharedImageScope('tok-downgrade');
    URL.revokeObjectURL = originalRevoke;
  });

  it('renders mermaid as plain <pre><code> text, strips resources/attributes, shows redacted placeholders, and handles redacted wakeups/steers when allowFiles=false', async () => {
    const events: SSEEvent[] = [
      {
        event: 'user_message',
        turn_index: 0,
        content: 'Analyze quarterly revenue',
        timestamp: '2026-11-27T00:59:00Z',
      },
      {
        event: 'message_chunk',
        turn_index: 0,
        role: 'assistant',
        content_type: 'text',
        phase: 'final_answer',
        finish_reason: 'stop',
        content: [
          'Here is the summary.',
          '',
          '```mermaid',
          'graph TD',
          '  A["Node"] --> B["End"]',
          '```',
          '',
          '<redacted-image alt="Revenue Breakdown"></redacted-image>',
          '',
          '<img src="https://evil.example/secret.png" alt="SECRET_IMG" />',
          '<iframe src="https://evil.example/embed"></iframe>',
          '<span style="background-image:url(https://evil.example/bg.png)" title="SECRET_ATTR_TITLE" data-secret="SECRET_DATA" aria-label="SECRET_ARIA">Clean span</span>',
        ].join('\n'),
      },
      {
        event: 'artifact',
        turn_index: 0,
        artifact_type: 'html_widget',
        artifact_id: 'w-redacted-1',
        status: 'completed',
        payload: { redacted: true },
      },
      {
        event: 'user_message',
        turn_index: 1,
        query_type: 'wakeup',
        content: '[定时唤醒触发]',
        timestamp: '2026-11-27T01:00:00Z',
        metadata: {
          wakeup: {
            v: 1,
            notices: [
              // Redacted wakeup notice intentionally omits wakeup_id
              { kind: 'at', notice: 'fire', seq: 1, total: 1, last: true, skipped: 0, fired_at: '2026-11-27T01:00:00Z' },
            ],
          },
        },
      },
      {
        event: 'steering_delivered',
        turn_index: 1,
        agent: 'main',
        timestamp: 1764205260,
        count: 2,
        messages: [
          {
            content: '[定时唤醒触发]',
            timestamp: 1764205260,
            wakeup: { kind: 'cron', notice: 'fire', seq: 2, total: 3, last: false, skipped: 0, fired_at: '2026-11-27T01:01:00Z' },
          },
          {
            content: 'Follow-up question from user',
            timestamp: 1764205261,
          },
        ],
      },
      {
        event: 'message_chunk',
        turn_index: 1,
        role: 'assistant',
        content_type: 'text',
        phase: 'final_answer',
        finish_reason: 'stop',
        content: 'Follow-up answer text.',
      },
      { event: 'replay_done', thread_id: 'thread-1' },
    ];

    h.replayMock.mockImplementationOnce(async (_token, onEvent) => {
      events.forEach((ev) => onEvent?.(ev));
    });

    const { container } = renderWithProviders(
      <SharedChatView shareToken="tok-redacted" metadata={makeMetadata(false)} />,
    );

    await waitFor(() => {
      expect(screen.getByText('Here is the summary.')).toBeInTheDocument();
    });

    // MermaidDiagram was never called; mermaid fence rendered as <pre><code> text.
    expect(h.mermaidSpy).toHaveBeenCalledTimes(0);
    const codeBlocks = Array.from(container.querySelectorAll('pre code'));
    expect(codeBlocks.some((el) => el.textContent?.includes('graph TD'))).toBe(true);

    // Zero diagram <svg>, zero <img>, zero <iframe>
    expect(container.querySelectorAll('svg[data-mermaid="true"]')).toHaveLength(0);
    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(container.querySelectorAll('iframe')).toHaveLength(0);

    // Zero style attributes containing url( and zero title/data-*/aria-* carrying protected content
    const allElements = Array.from(container.querySelectorAll('*'));
    for (const el of allElements) {
      const styleAttr = el.getAttribute('style') ?? '';
      expect(styleAttr.toLowerCase()).not.toContain('url(');
      for (const attr of Array.from(el.attributes)) {
        if (attr.name === 'title' || attr.name.startsWith('data-') || attr.name.startsWith('aria-')) {
          expect(attr.value).not.toMatch(/SECRET_/);
        }
      }
    }

    // Redacted image & widget static placeholders are rendered
    expect(screen.getByText('Revenue Breakdown')).toBeInTheDocument();
    expect(screen.getByText(/Interactive widget hidden|交互组件已隐藏/)).toBeInTheDocument();

    // Steering follow-up & redacted wakeup divider rendered cleanly
    expect(screen.getByText('Follow-up question from user')).toBeInTheDocument();
    expect(screen.getByText('Follow-up answer text.')).toBeInTheDocument();
  });

  it('loads earlier history page via history_page and prepends older turns before newer ones', async () => {
    h.replayMock
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'history_page', first_turn_index: 5, has_more: true });
        onEvent?.({ event: 'user_message', turn_index: 5, content: 'Newer turn question' });
        onEvent?.({ event: 'message_chunk', turn_index: 5, role: 'assistant', content_type: 'text', content: 'Newer turn answer' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      })
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'history_page', first_turn_index: 4, has_more: false });
        onEvent?.({ event: 'user_message', turn_index: 4, content: 'Older turn question' });
        onEvent?.({ event: 'message_chunk', turn_index: 4, role: 'assistant', content_type: 'text', content: 'Older turn answer' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      });

    const { container } = renderWithProviders(
      <SharedChatView shareToken="tok-redacted" metadata={makeMetadata(false)} />,
    );

    const loadButton = await screen.findByRole('button', { name: /Load earlier messages|加载更早的对话/ });
    expect(screen.getByText('Newer turn answer')).toBeInTheDocument();

    fireEvent.click(loadButton);

    await waitFor(() => {
      expect(screen.getByText('Older turn answer')).toBeInTheDocument();
    });
    expect(h.replayMock).toHaveBeenCalledTimes(2);
    expect(h.replayMock.mock.calls[1][2]).toMatchObject({ beforeTurn: 5 });
    expect(screen.queryByRole('button', { name: /Load earlier messages|加载更早的对话/ })).not.toBeInTheDocument();

    // Older turn must precede newer turn in DOM order
    const fullText = container.textContent ?? '';
    expect(fullText.indexOf('Older turn answer')).toBeLessThan(fullText.indexOf('Newer turn answer'));
  });

  it('revokes sharedImageCache, blocks slow backfill, clears old view, and reloads redacted replay on allow_files downgrade', async () => {
    const revokeSpy = vi.spyOn(sharedImageCache, 'revokeSharedImageScope');

    h.replayMock
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Full question' });
        onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Full view answer' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      })
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Full question' });
        onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Redacted replay answer' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      });

    const scope = sharedImageCache.buildSharedImageScope('tok-downgrade', true);
    const slowRead = sharedImageCache.beginScopedImageRead(scope);

    const { rerender } = renderWithProviders(
      <SharedChatView shareToken="tok-downgrade" metadata={makeMetadata(true)} />,
    );

    await waitFor(() => {
      expect(screen.getByText('Full view answer')).toBeInTheDocument();
    });

    // Downgrade effective permission from allow_files=true to allow_files=false
    rerender(<SharedChatView shareToken="tok-downgrade" metadata={makeMetadata(false)} />);

    await waitFor(() => {
      expect(screen.getByText('Redacted replay answer')).toBeInTheDocument();
    });

    expect(revokeSpy).toHaveBeenCalledWith('tok-downgrade');
    expect(slowRead.signal.aborted).toBe(true);
    expect(sharedImageCache.shouldCommitScopedImageRead(scope, slowRead.epoch)).toBe(false);
    expect(
      sharedImageCache.commitScopedImageUrl(scope, 'https://api.test/slow.png', slowRead.epoch, 'blob:late'),
    ).toBe(false);
    expect(screen.queryByText('Full view answer')).not.toBeInTheDocument();
    expect(h.replayMock).toHaveBeenCalledTimes(2);

    revokeSpy.mockRestore();
  });

  it('triggers immediate downgrade, revokes cache, and reloads redacted replay when public controlled media returns 403', async () => {
    const originalFetch = globalThis.fetch;
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('forbidden', { status: 403 }));
    globalThis.fetch = fetchMock;
    const revokeSpy = vi.spyOn(sharedImageCache, 'revokeSharedImageScope');
    const onPermissionForbidden = vi.fn<() => void>();

    h.replayMock
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Show chart' });
        onEvent?.({
          event: 'message_chunk',
          turn_index: 0,
          role: 'assistant',
          content_type: 'text',
          content: '![Chart](/api/v1/public/shared/tok-downgrade/media/ref-403)',
        });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      })
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Show chart' });
        onEvent?.({
          event: 'message_chunk',
          turn_index: 0,
          role: 'assistant',
          content_type: 'text',
          content: '<redacted-image alt="Chart"></redacted-image>',
        });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      });

    try {
      const { container } = renderWithProviders(
        <SharedChatView
          shareToken="tok-downgrade"
          metadata={makeMetadata(true)}
          onPermissionForbidden={onPermissionForbidden}
        />,
      );

      await waitFor(() => {
        expect(h.replayMock).toHaveBeenCalledTimes(2);
      });

      expect(onPermissionForbidden).toHaveBeenCalledTimes(1);
      expect(revokeSpy).toHaveBeenCalledWith('tok-downgrade');
      expect(container.querySelectorAll('img')).toHaveLength(0);
      expect(screen.getByText('Chart')).toBeInTheDocument();
    } finally {
      globalThis.fetch = originalFetch;
      revokeSpy.mockRestore();
    }
  });

  it('keeps default Markdown diagram rendering enabled in allowFiles=true / owner views', () => {
    render(<Markdown variant="chat" content={'```mermaid\ngraph LR\n  A --> B\n```'} />);
    expect(h.mermaidSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('mock-mermaid-svg')).toBeInTheDocument();
  });

  it('sends cache: no-store on all public SharedChat/api fetch calls and appends refreshStamp on serve/pdf URLs', async () => {
    const actualApi = await vi.importActual<typeof import('../api')>('../api');
    const originalFetch = globalThis.fetch;
    const originalCreateObjectURL = URL.createObjectURL;
    const fetchMock = vi.fn<typeof fetch>();
    globalThis.fetch = fetchMock;
    URL.createObjectURL = vi.fn<typeof URL.createObjectURL>(() => 'blob:obj');

    try {
      fetchMock
        .mockResolvedValueOnce(new Response(JSON.stringify({ kind: 'thread', thread_id: 't1', title: 'T', permissions: {} }), { status: 200 }))
        .mockResolvedValueOnce(new Response('event: replay_done\ndata: {"thread_id":"t1"}\n\n', { status: 200 }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ path: '.', files: [], source: 'live' }), { status: 200 }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ resolved: null }), { status: 200 }))
        .mockResolvedValueOnce(new Response(new Blob(['img']), { status: 200 }));

      await actualApi.getSharedMetadata('tok-1');
      await actualApi.replaySharedThread('tok-1', () => {}, { beforeTurn: 12 });
      await actualApi.getSharedFiles('tok-1');
      await actualApi.resolveSharedFile('tok-1', ['a.md']);
      await actualApi.servedObjectUrl(actualApi.sharedServePrefix('tok-1'), 'charts/a.png', { refreshStamp: 170000 });

      expect(fetchMock).toHaveBeenCalledTimes(5);
      for (const [, init] of fetchMock.mock.calls) {
        expect(init?.cache).toBe('no-store');
      }
      expect(String(fetchMock.mock.calls[1][0])).toContain('before_turn=12');
      expect(String(fetchMock.mock.calls[4][0])).toContain('v=170000');

      const pdfUrl = actualApi.buildSharedServeUrl(actualApi.sharedServePrefix('tok-1'), 'report.html', {
        format: 'pdf',
        refreshStamp: 170000,
      });
      expect(pdfUrl).toContain('format=pdf');
      expect(pdfUrl).toContain('v=170000');
    } finally {
      globalThis.fetch = originalFetch;
      URL.createObjectURL = originalCreateObjectURL;
    }
  });

  it('attaches credentials: include and cache: no-store on trusted-origin owner /messages/replay in transport.ts', async () => {
    const originalFetch = globalThis.fetch;
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response('event: replay_done\ndata: {}\n\n', { status: 200 }),
    );
    globalThis.fetch = fetchMock;
    try {
      await act(async () => {
        await streamFetch('/api/v1/threads/t-owner/messages/replay', { method: 'GET', headers: {} }, () => {});
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][1]?.credentials).toBe('include');
      expect(fetchMock.mock.calls[0][1]?.cache).toBe('no-store');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('treats download 403 as download-only refusal without downgrading file browsing, replaying, or revoking image cache', async () => {
    const revokeSpy = vi.spyOn(sharedImageCache, 'revokeSharedImageScope');
    const onPermissionForbidden = vi.fn<() => void>();
    const err403 = Object.assign(new Error('File download not permitted'), { response: { status: 403 } });
    h.downloadSharedFileMock.mockRejectedValueOnce(err403);

    h.replayMock.mockImplementationOnce(async (_token, onEvent) => {
      onEvent?.({ event: 'user_message', turn_index: 0, content: 'Open report' });
      onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Report ready' });
      onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
    });

    const { container } = renderWithProviders(
      <SharedChatView
        shareToken="tok-dl403"
        metadata={makeMetadata(true, { permissions: { allow_files: true, allow_download: true } })}
        refreshStamp={1000}
        onPermissionForbidden={onPermissionForbidden}
      />,
    );

    expect(await screen.findByText('Report ready')).toBeInTheDocument();
    const toggleBtn = container.querySelector('button[data-panel-toggle="header"]');
    expect(toggleBtn).not.toBeNull();
    fireEvent.click(toggleBtn!);
    await waitFor(() => expect(h.capturedApiAdapter).not.toBeNull());

    await expect(h.capturedApiAdapter!.triggerDownload!('report.md')).rejects.toBe(err403);

    expect(onPermissionForbidden).toHaveBeenCalledTimes(1);
    expect(h.toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        description: expect.stringMatching(/Downloads are turned off for this share\.|当前分享未开放下载/),
      }),
    );
    // File browsing stays enabled, replay is NOT refetched, image cache is NOT revoked
    expect(h.replayMock).toHaveBeenCalledTimes(1);
    expect(revokeSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('file-panel')).toBeInTheDocument();

    revokeSpy.mockRestore();
  });

  it('downgrades on /files/serve or /files/read 403 and recovers when newer metadata refreshStamp affirms allow_files=true', async () => {
    const revokeSpy = vi.spyOn(sharedImageCache, 'revokeSharedImageScope');
    const onPermissionForbidden = vi.fn<() => void>();
    const err403 = Object.assign(new Error('File access not permitted'), { response: { status: 403 } });
    h.readSharedFileMock.mockRejectedValueOnce(err403);

    h.replayMock
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Q' });
        onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Initial full replay' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      })
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Q' });
        onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Downgraded text replay' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      })
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Q' });
        onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Recovered full replay' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      });

    const { container, rerender } = renderWithProviders(
      <SharedChatView
        shareToken="tok-recover"
        metadata={makeMetadata(true)}
        refreshStamp={1000}
        onPermissionForbidden={onPermissionForbidden}
      />,
    );

    expect(await screen.findByText('Initial full replay')).toBeInTheDocument();
    fireEvent.click(container.querySelector('button[data-panel-toggle="header"]')!);
    await waitFor(() => expect(h.capturedApiAdapter).not.toBeNull());

    await expect(h.capturedApiAdapter!.readFile!('secret.md')).rejects.toBe(err403);

    await waitFor(() => expect(screen.getByText('Downgraded text replay')).toBeInTheDocument());
    expect(onPermissionForbidden).toHaveBeenCalledTimes(1);
    expect(revokeSpy).toHaveBeenCalledWith('tok-recover');
    expect(container.querySelector('button[data-panel-toggle="header"]')).toBeNull();

    // Metadata revalidation confirms allow_files=true with a strictly newer refreshStamp
    rerender(
      <SharedChatView
        shareToken="tok-recover"
        metadata={makeMetadata(true)}
        refreshStamp={2000}
        onPermissionForbidden={onPermissionForbidden}
      />,
    );

    await waitFor(() => expect(screen.getByText('Recovered full replay')).toBeInTheDocument());
    expect(container.querySelector('button[data-panel-toggle="header"]')).not.toBeNull();
    expect(h.replayMock).toHaveBeenCalledTimes(3);

    revokeSpy.mockRestore();
  });

  it('aborts in-flight readSharedFile and downloadSharedFile on downgrade, keeping React Query file bodies empty and skipping saveBlob', async () => {
    const actualApi = await vi.importActual<typeof import('../api')>('../api');
    const originalFetch = globalThis.fetch;
    const originalCreateObjectURL = URL.createObjectURL;
    const createObjectUrlSpy = vi.fn<typeof URL.createObjectURL>(() => 'blob:should-not-save');
    const anchorClickSpy = vi.fn<() => void>();
    const realCreateElement = document.createElement.bind(document);
    const createElSpy = vi.spyOn(document, 'createElement').mockImplementation(((tagName: string) => {
      const el = realCreateElement(tagName);
      if (tagName === 'a') (el as HTMLAnchorElement).click = anchorClickSpy;
      return el;
    }) as typeof document.createElement);
    URL.createObjectURL = createObjectUrlSpy;

    let capturedReadSignal: AbortSignal | undefined;
    let resolveSlowRead!: (value: { path: string; content: string; mime: string; offset: number; limit: number; truncated: boolean }) => void;
    h.readSharedFileMock.mockImplementationOnce((_token, _path, opts) => {
      capturedReadSignal = opts?.signal;
      return new Promise((resolve) => {
        resolveSlowRead = resolve;
      });
    });

    let resolveSlowDownloadFetch!: (res: Response) => void;
    const fetchMock = vi.fn<typeof fetch>(
      () =>
        new Promise<Response>((resolve) => {
          resolveSlowDownloadFetch = resolve;
        }),
    );
    globalThis.fetch = fetchMock;
    h.downloadSharedFileMock.mockImplementationOnce((token, path, opts) =>
      actualApi.downloadSharedFile(token, path, opts),
    );

    h.replayMock
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Files' });
        onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Full files view' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      })
      .mockImplementationOnce(async (_token, onEvent) => {
        onEvent?.({ event: 'user_message', turn_index: 0, content: 'Files' });
        onEvent?.({ event: 'message_chunk', turn_index: 0, role: 'assistant', content_type: 'text', content: 'Redacted view' });
        onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
      });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 5 * 60_000 } },
    });
    const existingAdapterKey = queryKeys.workspaceFiles.body('adapter:existing', 'old.md', 'text');
    queryClient.setQueryData(existingAdapterKey, { text: 'stale cached body' });

    try {
      const { container, rerender } = renderWithProviders(
        <SharedChatView
          shareToken="tok-inflight"
          metadata={makeMetadata(true, { permissions: { allow_files: true, allow_download: true } })}
        />,
        { queryClient },
      );

      expect(await screen.findByText('Full files view')).toBeInTheDocument();
      fireEvent.click(container.querySelector('button[data-panel-toggle="header"]')!);
      await waitFor(() => expect(h.capturedApiAdapter).not.toBeNull());

      const adapter = h.capturedApiAdapter!;
      const bodyKey = queryKeys.workspaceFiles.body('adapter:inflight', 'slow.md', 'text');
      let innerReadError: unknown = null;
      const bodyQueryError = queryClient
        .fetchQuery({
          queryKey: bodyKey,
          queryFn: () =>
            adapter.readFile!('slow.md').catch((err: unknown) => {
              innerReadError = err;
              throw err;
            }),
        })
        .catch((err: unknown) => err);
      const downloadError = adapter.triggerDownload!('secret.pdf').catch((err: unknown) => err);

      await waitFor(() => {
        expect(capturedReadSignal).toBeDefined();
        expect(fetchMock).toHaveBeenCalledTimes(1);
      });

      // Downgrade while both requests are in flight
      rerender(
        <SharedChatView
          shareToken="tok-inflight"
          metadata={makeMetadata(false)}
        />,
      );

      await waitFor(() => expect(screen.getByText('Redacted view')).toBeInTheDocument());
      expect(capturedReadSignal!.aborted).toBe(true);

      // Slow responses arrive after downgrade
      resolveSlowRead({ path: 'slow.md', content: 'SECRET_LATE_BODY', mime: 'text/markdown', offset: 0, limit: 100, truncated: false });
      resolveSlowDownloadFetch(new Response(new Blob(['SECRET_PDF']), { status: 200 }));

      await expect(bodyQueryError).resolves.toBeInstanceOf(Error);
      await waitFor(() => expect(innerReadError).toMatchObject({ name: 'AbortError' }));
      await expect(downloadError).resolves.toMatchObject({ name: 'AbortError' });

      expect(
        queryClient.getQueryCache().findAll({ predicate: (q) => isSharedFileBodyQueryKey(q.queryKey) }),
      ).toHaveLength(0);
      expect(createObjectUrlSpy).not.toHaveBeenCalled();
      expect(anchorClickSpy).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = originalFetch;
      URL.createObjectURL = originalCreateObjectURL;
      createElSpy.mockRestore();
    }
  });

  it('renders redacted html_widget placeholder as static card with zero iframes even when metadata allow_files=true', async () => {
    h.replayMock.mockImplementationOnce(async (_token, onEvent) => {
      onEvent?.({ event: 'user_message', turn_index: 0, content: 'Show dashboard' });
      onEvent?.({
        event: 'message_chunk',
        turn_index: 0,
        role: 'assistant',
        content_type: 'text',
        content: 'Widget summary below.',
      });
      onEvent?.({
        event: 'artifact',
        turn_index: 0,
        artifact_type: 'html_widget',
        artifact_id: 'w-stale-meta',
        status: 'completed',
        payload: { redacted: true },
      });
      onEvent?.({ event: 'replay_done', thread_id: 'thread-1' });
    });

    const { container } = renderWithProviders(
      <SharedChatView shareToken="tok-stale-widget" metadata={makeMetadata(true)} />,
    );

    expect(await screen.findByText('Widget summary below.')).toBeInTheDocument();
    expect(screen.getByText(/Interactive widget hidden|交互组件已隐藏/)).toBeInTheDocument();
    expect(container.querySelectorAll('iframe')).toHaveLength(0);
  });
});
