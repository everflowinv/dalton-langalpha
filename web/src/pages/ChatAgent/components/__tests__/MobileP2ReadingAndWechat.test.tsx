/**
 * Mobile Web P2 component & utility tests:
 *   - TranscriptStatus: Popover (click-to-open) on mobile vs HoverCard on desktop
 *   - Markdown CodeBlock: horizontal scroll (wrapLongLines=false + overflow-x-auto) on mobile vs wrapped on desktop
 *   - PerModelMatrix: sticky first column classes on mobile + .tap-target on row reset buttons
 *   - fileUpload: .heic / .heif classified as image
 *   - useHtmlActions: WeChat UA prompts to open in external browser + en/zh catalog coverage
 */
import type React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import { renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import i18n from '@/i18n';
import { enUS, zhCN, lookup } from '@/test/catalogs';

const mediaState = vi.hoisted(() => ({ mobile: false }));
const highlighterCapture = vi.hoisted(() => ({
  lastProps: null as Record<string, unknown> | null,
}));
const toastMock = vi.hoisted(() =>
  vi.fn((..._args: unknown[]) => ({
    id: 't-1',
    dismiss: vi.fn(),
    update: vi.fn(),
  })),
);

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => mediaState.mobile,
  getIsMobileSnapshot: () => mediaState.mobile,
}));

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', setTheme: vi.fn() }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  toast: (...args: unknown[]) => toastMock(...args),
  useToast: () => ({ toast: (...args: unknown[]) => toastMock(...args) }),
}));

vi.mock('../SyntaxHighlighter', () => ({
  default: ({ children, ...props }: { children?: React.ReactNode } & Record<string, unknown>) => {
    highlighterCapture.lastProps = props;
    return <pre data-testid="mock-syntax-highlighter" data-wrap={String(Boolean(props.wrapLongLines))}>{children}</pre>;
  },
  oneDark: {},
  oneLight: {},
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: {
      model_preference: {
        preferred_model: 'claude-sonnet-4',
        profiles: {
          'claude-sonnet-4': { reasoning_effort: 'high' },
          'retired-model-x': { reasoning_effort: 'low' },
        },
      },
    },
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useFeatures', () => ({
  useFeatureEnabled: () => false,
}));

vi.mock('@/hooks/useModelProfile', () => ({
  useModelProfileWriter: () => ({
    writeProfile: vi.fn(),
    clearProfile: vi.fn(),
  }),
}));

import { TranscriptStatus } from '../chatView/TranscriptStatus';
import { CodeBlock } from '../Markdown';
import { PerModelMatrix } from '@/components/model/PerModelMatrix';
import { api } from '@/api/client';
import { uploadChatFile } from '@/lib/dalton/chatUploadClient';
import { attachmentSendParts, attachmentsToContexts, inferImageMimeType, isImageFile } from '../../utils/fileUpload';
import { uploadAttachment } from '../../utils/attachmentUpload';
import * as attachmentsApi from '../../utils/api/attachments';
import { useFileDownloads } from '../filePanel/useFileDownloads';
import { exportServedPdf, isWeChatBrowser, useHtmlActions } from '../viewers/html/useHtmlActions';

const originalUserAgent = navigator.userAgent;

function setUserAgent(ua: string) {
  Object.defineProperty(navigator, 'userAgent', {
    value: ua,
    configurable: true,
  });
}

describe('Mobile P2 reading, table, HEIC, and WeChat details', () => {
  beforeEach(() => {
    mediaState.mobile = false;
    highlighterCapture.lastProps = null;
    toastMock.mockClear();
    setUserAgent(originalUserAgent);
  });

  afterEach(() => {
    setUserAgent(originalUserAgent);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('TranscriptStatus opens sandbox explanation Popover on tap on mobile and keeps HoverCard on desktop', async () => {
    const user = userEvent.setup();
    const baseProps = {
      modelStatus: null,
      isLoading: false,
      retired: null,
      turnStatus: { workspaceStarting: 'starting' as const, turnHeld: null, cliQueue: null },
      model: null,
      isCompacting: false as const,
      compactionTokens: null,
      queuedSend: false as const,
      threadId: 'th-1',
      active: false,
    };

    mediaState.mobile = true;
    const { unmount } = renderWithProviders(<TranscriptStatus {...baseProps} />);
    const infoBtn = screen.getByRole('button', { name: /Why is this taking a while|为什么需要等待/i });
    expect(infoBtn.className).toContain('tap-target');
    await user.click(infoBtn);
    const popover = document.querySelector('[data-slot="popover"]');
    expect(popover).not.toBeNull();
    expect(popover).toBeVisible();
    unmount();

    mediaState.mobile = false;
    renderWithProviders(<TranscriptStatus {...baseProps} />);
    const desktopBtn = screen.getByRole('button', { name: /Why is this taking a while|为什么需要等待/i });
    await user.click(desktopBtn);
    expect(document.querySelector('[data-slot="popover"]')).toBeNull();
  });

  it('Markdown CodeBlock scrolls horizontally without wrapping long lines on mobile and keeps wrap on desktop', () => {
    const sampleCode = 'const veryLongFinancialMetricHeader = "EBITDA_MARGIN_FY2026_ADJUSTED_CONSENSUS";';

    mediaState.mobile = true;
    const { container, unmount } = renderWithProviders(
      <CodeBlock language="typescript" code={sampleCode} />,
    );
    expect(highlighterCapture.lastProps?.wrapLongLines).toBe(false);
    const scroller = container.querySelector('[data-code-scroll="horizontal"]');
    expect(scroller).not.toBeNull();
    expect(scroller!.className).toContain('overflow-x-auto');
    unmount();

    mediaState.mobile = false;
    const desktop = renderWithProviders(
      <CodeBlock language="typescript" code={sampleCode} />,
    );
    expect(highlighterCapture.lastProps?.wrapLongLines).toBe(true);
    expect(desktop.container.querySelector('[data-code-scroll="horizontal"]')).toBeNull();
  });

  it('PerModelMatrix applies mobile-only border-separate, sticky first column with inner truncation, and .tap-target on row reset buttons', () => {
    const { container } = renderWithProviders(
      <PerModelMatrix
        metadata={{
          'claude-sonnet-4': {
            display_name: 'Claude Sonnet 4 Very Long Name',
            provider: 'anthropic',
            reasoning_efforts: ['low', 'medium', 'high'],
          },
        }}
        validModelNames={new Set(['claude-sonnet-4'])}
        compactionProfiles={null}
      />,
    );

    const table = container.querySelector('table') as HTMLTableElement | null;
    expect(table).not.toBeNull();
    expect(table!.className).toContain('border-collapse');
    expect(table!.className).toContain('max-md:border-separate');
    expect(table!.className).toContain('max-md:border-spacing-0');
    expect(table!.style.borderCollapse).toBe('');

    const firstTh = container.querySelector('thead th:first-child') as HTMLElement | null;
    expect(firstTh).not.toBeNull();
    expect(firstTh!.className).toContain('max-md:sticky');
    expect(firstTh!.className).toContain('max-md:left-0');
    expect(firstTh!.className).toContain('max-md:z-20');
    expect(firstTh!.className).toContain('max-md:bg-(--color-bg-card)');
    const firstThInner = firstTh!.querySelector('div');
    expect(firstThInner).not.toBeNull();
    expect(firstThInner!.className).toContain('max-md:max-w-[140px]');
    expect(firstThInner!.className).toContain('max-md:truncate');

    const firstTds = Array.from(container.querySelectorAll('tbody tr td:first-child'));
    expect(firstTds.length).toBe(2);
    for (const td of firstTds) {
      expect(td.className).toContain('max-md:sticky');
      expect(td.className).toContain('max-md:left-0');
      expect(td.className).toContain('max-md:z-10');
      expect(td.className).toContain('max-md:bg-(--color-bg-card)');
      const inner = td.querySelector('div');
      expect(inner).not.toBeNull();
      expect(inner!.className).toContain('max-md:max-w-[140px]');
      expect(inner!.className).toContain('max-md:truncate');
    }

    const resetBtns = Array.from(container.querySelectorAll('tbody tr td:last-child button'));
    expect(resetBtns.length).toBe(2);
    for (const btn of resetBtns) {
      expect(btn.className).toContain('tap-target');
    }
  });

  it('fileUpload classifies .heic and .heif photos as images and infers image/heic & image/heif MIME types when browser omits file.type', async () => {
    expect(isImageFile({ name: 'IMG_1234.heic', type: '' })).toBe(true);
    expect(isImageFile({ name: 'IMG_5678.HEIC', type: '' })).toBe(true);
    expect(isImageFile({ name: 'live_photo.heif', type: '' })).toBe(true);
    expect(isImageFile({ name: 'LIVE_PHOTO.HEIF', type: '' })).toBe(true);
    expect(isImageFile({ name: 'model.xlsx', type: '' })).toBe(false);

    expect(inferImageMimeType({ name: 'IMG_1234.heic', type: '' })).toBe('image/heic');
    expect(inferImageMimeType({ name: 'IMG_5678.HEIC', type: '' })).toBe('image/heic');
    expect(inferImageMimeType({ name: 'live_photo.heif', type: '' })).toBe('image/heif');
    expect(inferImageMimeType({ name: 'LIVE_PHOTO.HEIF', type: '' })).toBe('image/heif');
    expect(inferImageMimeType({ name: 'shot.png', type: '' })).toBe('image/png');
    expect(inferImageMimeType({ name: 'IMG_1234.heic', type: 'image/heic' })).toBe('image/heic');
    expect(inferImageMimeType({ name: 'model.xlsx', type: '' })).toBe('');

    const heicFile = new File(['bytes'], 'IMG_9001.HEIC', { type: '' });
    const heifFile = new File(['bytes'], 'IMG_9002.heif', { type: '' });

    const { contexts } = attachmentSendParts([
      { file: heicFile, type: '', attachmentId: 'att-heic' },
      { file: heifFile, type: '', attachmentId: '', dataUrl: 'data:application/octet-stream;base64,AA==' },
    ]);
    expect(contexts[0]).toMatchObject({ type: 'image', attachment_id: 'att-heic' });
    expect(contexts[1]).toMatchObject({ type: 'image' });

    const inlineContexts = attachmentsToContexts([
      { file: heicFile, type: '', dataUrl: 'data:application/octet-stream;base64,BB==' },
    ]);
    expect(inlineContexts[0]).toMatchObject({ type: 'image' });

    const beginSpy = vi
      .spyOn(attachmentsApi, 'beginAttachment')
      .mockResolvedValue({ attachment_id: 'att-staged-heic', mode: 'relay', max_bytes: 1024 * 1024 });
    vi.spyOn(attachmentsApi, 'sendAttachmentContent').mockResolvedValue({
      attachment_id: 'att-staged-heic',
      state: 'ready',
      name: 'IMG_9001.HEIC',
      size: 5,
      mime_type: 'image/heic',
      error: null,
    });

    await uploadAttachment(heicFile);
    expect(beginSpy).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'IMG_9001.HEIC', mime_type: 'image/heic' }),
    );

    await uploadAttachment(heifFile);
    expect(beginSpy).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'IMG_9002.heif', mime_type: 'image/heif' }),
    );

    // Chunked multipart upload (uploadChatFile) also infers image/heic & image/heif when File.type is empty
    const postSpy = vi.spyOn(api, 'post').mockImplementation(async (url, body) => {
      if (url === '/api/v1/dalton/chat-uploads') {
        const req = body as { workspace_id: string; filename: string; size_bytes: number; mime_type: string };
        return {
          data: {
            upload_id: `up-${req.filename}`,
            workspace_id: req.workspace_id,
            filename: req.filename,
            mime_type: req.mime_type,
            size_bytes: req.size_bytes,
            part_size: 1024,
            part_count: 1,
            state: 'uploading',
          },
          status: 200,
          statusText: 'OK',
          headers: {},
        };
      }
      const id = decodeURIComponent(url.split('/')[5] ?? 'up-unknown');
      return {
        data: {
          upload_id: id,
          workspace_id: 'ws-1',
          filename: id.replace(/^up-/, ''),
          mime_type: id.endsWith('.heif') ? 'image/heif' : 'image/heic',
          size_bytes: 5,
          part_size: 1024,
          part_count: 1,
          state: 'ready',
          sha256: null,
        },
        status: 200,
        statusText: 'OK',
        headers: {},
      };
    });
    vi.spyOn(api, 'put').mockImplementation(async (url) => {
      const id = decodeURIComponent(url.split('/')[5] ?? 'up-unknown');
      return {
        data: {
          upload_id: id,
          part_number: 1,
          size_bytes: 5,
          sha256: '',
          state: 'uploaded',
        },
        status: 200,
        statusText: 'OK',
        headers: {},
      };
    });

    await uploadChatFile(heicFile, { workspaceId: 'ws-1' });
    expect(postSpy).toHaveBeenCalledWith(
      '/api/v1/dalton/chat-uploads',
      expect.objectContaining({ filename: 'IMG_9001.HEIC', mime_type: 'image/heic' }),
      expect.anything(),
    );

    await uploadChatFile(heifFile, { workspaceId: 'ws-1' });
    expect(postSpy).toHaveBeenCalledWith(
      '/api/v1/dalton/chat-uploads',
      expect.objectContaining({ filename: 'IMG_9002.heif', mime_type: 'image/heif' }),
      expect.anything(),
    );
  });

  it('useHtmlActions and exportServedPdf prompt to open in external browser under WeChat UA, with en/zh copy', async () => {
    expect(typeof lookup(enUS, 'filePanel:wechatOpenInBrowserHint')).toBe('string');
    expect(String(lookup(enUS, 'filePanel:wechatOpenInBrowserHint')).length).toBeGreaterThan(0);
    expect(typeof lookup(zhCN, 'filePanel:wechatOpenInBrowserHint')).toBe('string');
    expect(String(lookup(zhCN, 'filePanel:wechatOpenInBrowserHint')).length).toBeGreaterThan(0);

    const wechatUA =
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x18003237) NetType/WIFI Language/zh_CN';
    const safariUA =
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1';

    expect(isWeChatBrowser(wechatUA)).toBe(true);
    expect(isWeChatBrowser(safariUA)).toBe(false);

    setUserAgent(wechatUA);
    const createObjectURL = vi.fn(() => 'blob:unused');
    const fetchMock = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    vi.stubGlobal('fetch', fetchMock);

    // Widget mode downloadHtml & exportPdf in WeChat
    const widgetHook = renderHook(() =>
      useHtmlActions({ mode: 'widget', srcDoc: '<html><body>chart</body></html>', fileName: 'chart.html' }),
    );
    widgetHook.result.current.downloadHtml();
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(String((toastMock.mock.calls[0][0] as { description?: string }).description)).toMatch(/WeChat|external browser|微信|浏览器/i);
    expect(createObjectURL).not.toHaveBeenCalled();

    toastMock.mockClear();
    await widgetHook.result.current.exportPdf();
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(createObjectURL).not.toHaveBeenCalled();

    // File mode downloadHtml in WeChat
    toastMock.mockClear();
    const triggerDownload = vi.fn().mockResolvedValue(undefined);
    const fileHook = renderHook(() =>
      useHtmlActions({
        mode: 'file',
        filePath: 'reports/q3.html',
        servedUrl: '/api/v1/wsfiles/g/tok/reports/q3.html',
        triggerDownload,
      }),
    );
    fileHook.result.current.downloadHtml();
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect(triggerDownload).not.toHaveBeenCalled();

    // Direct exportServedPdf in WeChat
    toastMock.mockClear();
    await exportServedPdf({
      filePath: 'reports/q3.html',
      servedUrl: '/api/v1/wsfiles/g/tok/reports/q3.html',
      printHint: 'print',
      generatingHint: 'generating',
      wechatHint: 'Open in external browser',
    });
    expect(toastMock).toHaveBeenCalledWith({ description: 'Open in external browser' });
    expect(fetchMock).not.toHaveBeenCalled();

    // Non-WeChat browser downloads normally without WeChat toast
    toastMock.mockClear();
    setUserAgent(safariUA);
    fileHook.result.current.downloadHtml();
    expect(triggerDownload).toHaveBeenCalledTimes(1);
    expect(toastMock).not.toHaveBeenCalled();
  });

  it('useFileDownloads prompts to open in external browser in WeChat across download, downloadQuietly, and downloadMany, and downloads normally in Safari', async () => {
    const wechatUA =
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x18003237) NetType/WIFI Language/zh_CN';
    const safariUA =
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1';

    const triggerDownloadFn = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() =>
      useFileDownloads({ workspaceId: 'ws-1', triggerDownloadFn, workspaceStatus: 'ready' }),
    );

    // WeChat built-in browser blocks download, downloadQuietly, and non-empty downloadMany with guidance toast
    const activeCatalog = (i18n.language || '').startsWith('zh') ? zhCN : enUS;
    const expectedWechatHint = String(lookup(activeCatalog, 'filePanel:wechatOpenInBrowserHint'));
    expect(expectedWechatHint).toBeTruthy();
    expect(expectedWechatHint).not.toBe('filePanel:wechatOpenInBrowserHint');

    setUserAgent(wechatUA);

    toastMock.mockClear();
    result.current.download('reports/q3.html');
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect((toastMock.mock.calls[0][0] as { description?: string }).description).toBe(expectedWechatHint);
    expect(triggerDownloadFn).not.toHaveBeenCalled();

    toastMock.mockClear();
    result.current.downloadQuietly('reports/q3.html');
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect((toastMock.mock.calls[0][0] as { description?: string }).description).toBe(expectedWechatHint);
    expect(triggerDownloadFn).not.toHaveBeenCalled();

    toastMock.mockClear();
    await result.current.downloadMany(['reports/q3.html', 'reports/notes.md']);
    expect(toastMock).toHaveBeenCalledTimes(1);
    expect((toastMock.mock.calls[0][0] as { description?: string }).description).toBe(expectedWechatHint);
    expect(triggerDownloadFn).not.toHaveBeenCalled();

    // Empty batch should not toast
    toastMock.mockClear();
    await result.current.downloadMany([]);
    expect(toastMock).not.toHaveBeenCalled();
    expect(triggerDownloadFn).not.toHaveBeenCalled();

    // Non-WeChat Safari downloads normally
    setUserAgent(safariUA);
    toastMock.mockClear();

    result.current.download('reports/q3.html');
    result.current.downloadQuietly('reports/notes.md');
    await result.current.downloadMany(['reports/a.csv', 'reports/b.pdf']);

    expect(triggerDownloadFn).toHaveBeenCalledTimes(4);
    expect(triggerDownloadFn).toHaveBeenNthCalledWith(1, 'ws-1', 'reports/q3.html');
    expect(triggerDownloadFn).toHaveBeenNthCalledWith(2, 'ws-1', 'reports/notes.md');
    expect(triggerDownloadFn).toHaveBeenNthCalledWith(3, 'ws-1', 'reports/a.csv');
    expect(triggerDownloadFn).toHaveBeenNthCalledWith(4, 'ws-1', 'reports/b.pdf');
    expect(toastMock).not.toHaveBeenCalled();
  });
});
