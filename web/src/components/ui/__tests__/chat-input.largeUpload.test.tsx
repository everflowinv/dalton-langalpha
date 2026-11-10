import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createRef } from 'react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError, type ApiResponse, type RequestConfig } from '@/api/client';
import {
  CHAT_UPLOAD_PART_SIZE,
  MAX_CHAT_UPLOAD_FILE_SIZE,
  MAX_PARALLEL_PARTS,
  MAX_TOTAL_CHAT_UPLOAD_BYTES,
} from '@/lib/dalton/chatUploadClient';
import { MAX_FILE_SIZE, MAX_TOTAL_ATTACHMENTS_BYTES } from '../chat-input.helpers';
import ChatInput, { type ChatInputHandle, type ChatInputProps } from '../chat-input';
import { attachmentsToContexts } from '@/pages/ChatAgent/utils/fileUpload';
import {
  foldSteeringDelivery,
  steeringDeliveredEntries,
} from '@/pages/ChatAgent/session/stream/steeringDelivery';
import {
  handleHistorySteeringDelivered,
  handleHistoryUserMessage,
} from '@/pages/ChatAgent/session/history/historyHandlers';
import type { ChatMessage, UserMessage } from '@/types/chat';
import type { MessageRecord, SetMessages } from '@/pages/ChatAgent/hooks/utils/types';
import type { PairState } from '@/pages/ChatAgent/session/types';

const {
  apiPostMock,
  apiPutMock,
  apiGetMock,
  apiDeleteMock,
  toastSpy,
} = vi.hoisted(() => ({
  apiPostMock: vi.fn(),
  apiPutMock: vi.fn(),
  apiGetMock: vi.fn(),
  apiDeleteMock: vi.fn(),
  toastSpy: vi.fn(),
}));

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      post: apiPostMock,
      put: apiPutMock,
      get: apiGetMock,
      delete: apiDeleteMock,
    },
  };
});

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ data: undefined, isLoading: false }),
}));

vi.mock('../use-toast', () => ({
  useToast: () => ({ toast: toastSpy }),
  toast: toastSpy,
}));

function okResponse<T>(data: T, status = 200): ApiResponse<T> {
  return {
    data,
    status,
    statusText: 'OK',
    headers: {},
  };
}

function makeStubFile(name: string, mimeType: string, logicalSize: number): File {
  const seed = new Uint8Array(logicalSize === 0 ? 0 : 16);
  for (let i = 0; i < seed.length; i += 1) {
    seed[i] = (i * 7 + 3) & 0xff;
  }
  const file = new File([seed], name, { type: mimeType });
  Object.defineProperty(file, 'size', { value: logicalSize, configurable: true });
  file.slice = vi.fn((start = 0, end = logicalSize) => {
    const clampedStart = Math.max(0, start);
    const clampedEnd = Math.min(logicalSize, end);
    const chunkLen = Math.max(0, clampedEnd - clampedStart);
    const sample = new Uint8Array(chunkLen === 0 ? 0 : 32);
    if (sample.length > 0) {
      sample[0] = Math.floor(clampedStart / CHAT_UPLOAD_PART_SIZE) & 0xff;
    }
    const blob = new Blob([sample], { type: mimeType });
    Object.defineProperty(blob, 'size', { value: chunkLen, configurable: true });
    Object.defineProperty(blob, 'arrayBuffer', {
      value: () => Promise.resolve(sample.buffer.slice(0)),
      configurable: true,
    });
    return blob;
  });
  return file;
}

function installDefaultUploadApiMocks(resolvedWorkspaceId = 'ws-resolved-1') {
  let seq = 0;
  let activeParallelPuts = 0;
  let peakParallelPuts = 0;
  const sessions = new Map<
    string,
    {
      workspaceId: string;
      filename: string;
      mimeType: string;
      sizeBytes: number;
      partSize: number;
      partCount: number;
    }
  >();

  apiPostMock.mockImplementation((url: string, body?: Record<string, unknown>) => {
    if (url === '/api/v1/dalton/chat-uploads') {
      seq += 1;
      const uploadId = `up-test-${seq}`;
      const sizeBytes = Number(body?.size_bytes ?? 0);
      const partSize = CHAT_UPLOAD_PART_SIZE;
      const partCount = Math.max(1, Math.ceil(sizeBytes / partSize));
      const workspaceId =
        typeof body?.workspace_id === 'string' && body.workspace_id !== 'home'
          ? body.workspace_id
          : resolvedWorkspaceId;
      const filename = String(body?.filename ?? 'file.bin');
      const mimeType = String(body?.mime_type ?? 'application/octet-stream');
      sessions.set(uploadId, {
        workspaceId,
        filename,
        mimeType,
        sizeBytes,
        partSize,
        partCount,
      });
      return Promise.resolve(
        okResponse(
          {
            upload_id: uploadId,
            workspace_id: workspaceId,
            filename,
            mime_type: mimeType,
            size_bytes: sizeBytes,
            part_size: partSize,
            part_count: partCount,
            state: 'uploading',
          },
          201,
        ),
      );
    }
    const completeMatch = /^\/api\/v1\/dalton\/chat-uploads\/([^/]+)\/complete$/.exec(url);
    if (completeMatch) {
      const uploadId = completeMatch[1];
      const meta = sessions.get(uploadId);
      if (!meta) {
        return Promise.reject(new Error(`Unknown upload session ${uploadId}`));
      }
      return Promise.resolve(
        okResponse({
          upload_id: uploadId,
          workspace_id: meta.workspaceId,
          filename: meta.filename,
          mime_type: meta.mimeType,
          size_bytes: meta.sizeBytes,
          part_size: meta.partSize,
          part_count: meta.partCount,
          state: 'ready',
          sha256: 'deadbeef'.repeat(8),
        }),
      );
    }
    return Promise.reject(new Error(`Unexpected POST ${url}`));
  });

  apiPutMock.mockImplementation(
    async (url: string, chunk: Blob, config?: RequestConfig) => {
      const partMatch = /^\/api\/v1\/dalton\/chat-uploads\/([^/]+)\/parts\/(\d+)$/.exec(url);
      if (!partMatch) {
        throw new Error(`Unexpected PUT ${url}`);
      }
      if (config?.signal?.aborted) {
        throw new ApiError('Request aborted', 'ERR_CANCELED');
      }
      activeParallelPuts += 1;
      peakParallelPuts = Math.max(peakParallelPuts, activeParallelPuts);
      await Promise.resolve();
      activeParallelPuts -= 1;
      return okResponse({
        upload_id: partMatch[1],
        part_number: Number(partMatch[2]),
        size_bytes: chunk.size,
        sha256: 'a'.repeat(64),
        state: 'uploading',
      });
    },
  );

  apiDeleteMock.mockResolvedValue(okResponse({ upload_id: 'up-test-1', state: 'aborted' }));

  return {
    getPeakParallelPuts: () => peakParallelPuts,
  };
}

function renderComposer(props: Partial<ChatInputProps> = {}) {
  const ref = createRef<ChatInputHandle>();
  const onSend = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChatInput ref={ref} onSend={onSend} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const box = screen.getAllByRole('textbox')[0] as HTMLTextAreaElement;
  const fileInput = view.container.querySelector('input[type="file"]') as HTMLInputElement;
  const sendButton = () => screen.getByRole('button', { name: 'Send message' });
  return { ...view, ref, box, fileInput, sendButton, onSend };
}

describe('ChatInput large file uploads (up to 200 MiB)', () => {
  beforeEach(() => {
    apiPostMock.mockReset();
    apiPutMock.mockReset();
    apiGetMock.mockReset();
    apiDeleteMock.mockReset();
    toastSpy.mockReset();
    for (const urlCtor of new Set([URL, window.URL])) {
      if (!urlCtor) continue;
      if (typeof urlCtor.createObjectURL === 'function') {
        vi.spyOn(urlCtor, 'createObjectURL').mockImplementation(() => 'blob:preview');
      } else {
        Object.defineProperty(urlCtor, 'createObjectURL', {
          value: vi.fn(() => 'blob:preview'),
          configurable: true,
          writable: true,
        });
      }
      if (typeof urlCtor.revokeObjectURL === 'function') {
        vi.spyOn(urlCtor, 'revokeObjectURL').mockImplementation(() => {});
      } else {
        Object.defineProperty(urlCtor, 'revokeObjectURL', {
          value: vi.fn(),
          configurable: true,
          writable: true,
        });
      }
    }
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('preserves exact 30 MiB / 60 MiB inline constants while exposing 200 MiB / 600 MiB stored upload constants', () => {
    expect(MAX_FILE_SIZE).toBe(30 * 1024 * 1024);
    expect(MAX_TOTAL_ATTACHMENTS_BYTES).toBe(60 * 1024 * 1024);
    expect(MAX_CHAT_UPLOAD_FILE_SIZE).toBe(200 * 1024 * 1024);
    expect(MAX_TOTAL_CHAT_UPLOAD_BYTES).toBe(600 * 1024 * 1024);
    expect(CHAT_UPLOAD_PART_SIZE).toBe(8 * 1024 * 1024);
    expect(MAX_PARALLEL_PARTS).toBe(2);
  });

  it('rejects 0-byte and >200 MiB files with plain product error toasts and never starts an upload', () => {
    installDefaultUploadApiMocks();
    const { fileInput } = renderComposer();

    const emptyFile = makeStubFile('empty.csv', 'text/csv', 0);
    fireEvent.change(fileInput, { target: { files: [emptyFile] } });
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        title: expect.stringContaining('empty.csv'),
        variant: 'destructive',
      }),
    );

    toastSpy.mockClear();
    const tooLargeFile = makeStubFile(
      'too-big.parquet',
      'application/octet-stream',
      MAX_CHAT_UPLOAD_FILE_SIZE + 1,
    );
    fireEvent.change(fileInput, { target: { files: [tooLargeFile] } });
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        title: expect.stringContaining('200 MB'),
        variant: 'destructive',
      }),
    );
    expect(apiPostMock).not.toHaveBeenCalled();
    expect(apiPutMock).not.toHaveBeenCalled();
  });

  it('uploads an exact 200 MiB file via picker in 25 bounded 8 MiB chunks without whole-file arrayBuffer or FileReader', async () => {
    const { getPeakParallelPuts } = installDefaultUploadApiMocks('ws-alpha');
    const readAsDataURLSpy = vi.spyOn(FileReader.prototype, 'readAsDataURL');
    const readAsArrayBufferSpy = vi.spyOn(FileReader.prototype, 'readAsArrayBuffer');

    const { box, fileInput, sendButton, onSend } = renderComposer({
      selectedWorkspaceId: 'ws-alpha',
    });

    const exact200MiB = makeStubFile(
      'archive-200m.zip',
      'application/zip',
      MAX_CHAT_UPLOAD_FILE_SIZE,
    );
    const wholeFileArrayBufferSpy = vi.fn(() =>
      Promise.reject(new Error('Whole-file arrayBuffer() must not be called on 200 MiB file')),
    );
    Object.defineProperty(exact200MiB, 'arrayBuffer', {
      value: wholeFileArrayBufferSpy,
      configurable: true,
    });
    fireEvent.change(fileInput, { target: { files: [exact200MiB] } });

    await waitFor(() => {
      expect(apiPutMock).toHaveBeenCalledTimes(25);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('attachment-uploading')).toBeNull();
    });

    expect(readAsDataURLSpy).not.toHaveBeenCalled();
    expect(readAsArrayBufferSpy).not.toHaveBeenCalled();
    expect(wholeFileArrayBufferSpy).not.toHaveBeenCalled();
    expect(getPeakParallelPuts()).toBeLessThanOrEqual(MAX_PARALLEL_PARTS);
    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/v1/dalton/chat-uploads',
      expect.objectContaining({
        filename: 'archive-200m.zip',
        mime_type: 'application/zip',
        size_bytes: MAX_CHAT_UPLOAD_FILE_SIZE,
        workspace_id: 'ws-alpha',
      }),
      expect.any(Object),
    );

    fireEvent.change(box, { target: { value: 'Inspect this archive' } });
    expect(sendButton()).not.toBeDisabled();
    fireEvent.click(sendButton());

    expect(onSend).toHaveBeenCalledTimes(1);
    const payload = onSend.mock.calls[0][0];
    expect(payload.files).toHaveLength(1);
    expect(payload.files[0]).toMatchObject({
      dataUrl: null,
      type: 'application/zip',
      uploadId: 'up-test-1',
    });

    const contexts = attachmentsToContexts(payload.files);
    expect(contexts).toEqual([
      {
        type: 'chat_upload',
        upload_id: 'up-test-1',
        name: 'archive-200m.zip',
        mime_type: 'application/zip',
        size: MAX_CHAT_UPLOAD_FILE_SIZE,
      },
    ]);
    expect(JSON.stringify(contexts)).not.toContain('data:');
  });

  it('accepts >30 MiB files via drag/drop and clipboard paste while keeping <=30 MiB files on inline dataUrl', async () => {
    installDefaultUploadApiMocks('ws-home');
    const readAsDataURLSpy = vi.spyOn(FileReader.prototype, 'readAsDataURL');

    const { container, box, sendButton, onSend } = renderComposer({ inHome: true });
    const dropSurface = container.querySelector('.chat-input-container') as HTMLElement;
    expect(dropSurface).not.toBeNull();

    const dropped36MiB = makeStubFile('dataset.parquet', 'application/octet-stream', 36 * 1024 * 1024);
    fireEvent.dragEnter(dropSurface, {
      dataTransfer: { files: [dropped36MiB], types: ['Files'] },
    });
    fireEvent.drop(dropSurface, {
      dataTransfer: { files: [dropped36MiB], types: ['Files'] },
    });

    await waitFor(() => {
      expect(apiPutMock).toHaveBeenCalledTimes(5);
    });

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/v1/dalton/chat-uploads',
      expect.objectContaining({
        filename: 'dataset.parquet',
        workspace_id: 'home',
      }),
      expect.any(Object),
    );
    expect(readAsDataURLSpy).not.toHaveBeenCalled();

    const pasted34MiB = makeStubFile('recording.wav', 'audio/wav', 34 * 1024 * 1024);
    fireEvent.paste(box, {
      clipboardData: {
        items: [
          {
            kind: 'file',
            getAsFile: () => pasted34MiB,
          },
        ],
      },
    });

    await waitFor(() => {
      expect(apiPutMock).toHaveBeenCalledTimes(10);
    });
    expect(readAsDataURLSpy).not.toHaveBeenCalled();

    const smallPng = new window.File(['png-inline-bytes'], 'chart.png', {
      type: 'image/png',
    });
    fireEvent.paste(box, {
      clipboardData: {
        items: [
          {
            kind: 'file',
            getAsFile: () => smallPng,
          },
        ],
      },
    });

    await waitFor(() => {
      expect(URL.createObjectURL).toHaveBeenCalledWith(smallPng);
      expect(readAsDataURLSpy).toHaveBeenCalledTimes(1);
      expect(screen.queryByTestId('attachment-uploading')).toBeNull();
      expect(sendButton()).not.toBeDisabled();
    });

    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledTimes(1);

    const contexts = attachmentsToContexts(onSend.mock.calls[0][0].files);
    expect(contexts).toHaveLength(3);
    expect(contexts[0]).toMatchObject({
      type: 'chat_upload',
      upload_id: 'up-test-1',
      name: 'dataset.parquet',
    });
    expect(contexts[1]).toMatchObject({
      type: 'chat_upload',
      upload_id: 'up-test-2',
      name: 'recording.wav',
    });
    expect(contexts[2]).toMatchObject({
      type: 'image',
      description: 'chart.png',
      data: expect.stringMatching(/^data:image\/png;base64,/),
    });
  });

  it('shows progress %, disables send/Enter/handle.send() while uploading, and aborts + calls DELETE on remove', async () => {
    const fileSize = 32 * 1024 * 1024; // 4 x 8 MiB parts -> 2 completed parts = exact 50%
    const inflightSignals: AbortSignal[] = [];

    apiPostMock.mockResolvedValue(
      okResponse(
        {
          upload_id: 'up-inflight-1',
          workspace_id: 'ws-1',
          filename: 'bundle.tar',
          mime_type: 'application/x-tar',
          size_bytes: fileSize,
          part_size: CHAT_UPLOAD_PART_SIZE,
          part_count: 4,
          state: 'uploading',
        },
        201,
      ),
    );
    apiPutMock.mockImplementation(
      (url: string, chunk: Blob, config?: RequestConfig) => {
        const partMatch = /\/parts\/(\d+)$/.exec(url);
        const partNumber = Number(partMatch?.[1] ?? 0);
        if (partNumber <= 2) {
          return Promise.resolve(
            okResponse({
              upload_id: 'up-inflight-1',
              part_number: partNumber,
              size_bytes: chunk.size,
              sha256: 'b'.repeat(64),
              state: 'uploading',
            }),
          );
        }
        if (config?.signal) {
          inflightSignals.push(config.signal);
        }
        return new Promise<ApiResponse<unknown>>((_resolve, reject) => {
          config?.signal?.addEventListener('abort', () => {
            reject(new ApiError('Request aborted', 'ERR_CANCELED'));
          });
        });
      },
    );
    apiDeleteMock.mockResolvedValue(okResponse({ upload_id: 'up-inflight-1', state: 'aborted' }));

    const { ref, box, fileInput, sendButton, onSend } = renderComposer({
      selectedWorkspaceId: 'ws-1',
    });

    fireEvent.change(box, { target: { value: 'Wait for upload' } });
    const file32MiB = makeStubFile('bundle.tar', 'application/x-tar', fileSize);
    fireEvent.change(fileInput, { target: { files: [file32MiB] } });

    await waitFor(() => {
      expect(screen.getByTestId('attachment-uploading')).toHaveTextContent('50%');
      expect(inflightSignals).toHaveLength(2);
    });

    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();

    let handleSent = true;
    act(() => {
      handleSent = ref.current!.send();
    });
    expect(handleSent).toBe(false);
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

    await waitFor(() => {
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/dalton/chat-uploads/up-inflight-1');
    });
    expect(inflightSignals.every((sig) => sig.aborted)).toBe(true);
  });

  it('surfaces upload failure with Retry button, keeps send disabled until retried, and succeeds on Retry', async () => {
    const fileSize = 36 * 1024 * 1024;
    const partCount = Math.ceil(fileSize / CHAT_UPLOAD_PART_SIZE); // 5 parts (4 x 8 MiB + 1 x 4 MiB)
    let shouldFail = true;
    apiPostMock.mockImplementation((url: string) => {
      if (url === '/api/v1/dalton/chat-uploads') {
        return Promise.resolve(
          okResponse(
            {
              upload_id: shouldFail ? 'up-fail-1' : 'up-retry-2',
              workspace_id: 'ws-1',
              filename: 'report.bin',
              mime_type: 'application/octet-stream',
              size_bytes: fileSize,
              part_size: CHAT_UPLOAD_PART_SIZE,
              part_count: partCount,
              state: 'uploading',
            },
            201,
          ),
        );
      }
      return Promise.resolve(
        okResponse({
          upload_id: 'up-retry-2',
          workspace_id: 'ws-1',
          filename: 'report.bin',
          mime_type: 'application/octet-stream',
          size_bytes: fileSize,
          part_size: CHAT_UPLOAD_PART_SIZE,
          part_count: partCount,
          state: 'ready',
          sha256: 'd'.repeat(64),
        }),
      );
    });

    apiPutMock.mockImplementation((url: string, chunk: Blob) => {
      if (shouldFail) {
        return Promise.reject(
          new ApiError('Bad part', 'ERR_BAD_REQUEST', {
            data: { detail: 'Part checksum mismatch' },
            status: 400,
            statusText: 'Bad Request',
            headers: {},
          }),
        );
      }
      const partMatch = /\/parts\/(\d+)$/.exec(url);
      return Promise.resolve(
        okResponse({
          upload_id: 'up-retry-2',
          part_number: Number(partMatch?.[1] ?? 1),
          size_bytes: chunk.size,
          sha256: 'd'.repeat(64),
          state: 'uploading',
        }),
      );
    });
    apiDeleteMock.mockResolvedValue(okResponse({ upload_id: 'up-fail-1', state: 'aborted' }));

    const { box, fileInput, sendButton, onSend } = renderComposer({
      selectedWorkspaceId: 'ws-1',
    });
    fireEvent.change(box, { target: { value: 'Check retry' } });

    const largeFile = makeStubFile('report.bin', 'application/octet-stream', fileSize);
    fireEvent.change(fileInput, { target: { files: [largeFile] } });

    const retryBtn = await screen.findByTestId('attachment-retry-upload');
    expect(screen.getByTestId('attachment-upload-error')).toBeInTheDocument();
    expect(sendButton()).toBeDisabled();

    const failedPutCalls = apiPutMock.mock.calls.length;
    expect(failedPutCalls).toBeGreaterThanOrEqual(1);
    shouldFail = false;
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.queryByTestId('attachment-upload-error')).toBeNull();
      expect(screen.queryByTestId('attachment-uploading')).toBeNull();
    });
    expect(apiPutMock).toHaveBeenCalledTimes(failedPutCalls + partCount);

    expect(sendButton()).not.toBeDisabled();
    fireEvent.click(sendButton());
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0].files[0].uploadId).toBe('up-retry-2');
  });

  it('restores sent uploadId via giveBack without re-uploading or calling DELETE on the sent upload', async () => {
    installDefaultUploadApiMocks('ws-restore');
    const { ref, box, fileInput, sendButton, onSend } = renderComposer({
      selectedWorkspaceId: 'ws-restore',
    });

    const largeFile = makeStubFile('model.safetensors', 'application/octet-stream', 40 * 1024 * 1024);
    fireEvent.change(fileInput, { target: { files: [largeFile] } });

    await waitFor(() => {
      expect(screen.queryByTestId('attachment-uploading')).toBeNull();
    });
    expect(apiPostMock).toHaveBeenCalledTimes(2); // initiate + complete

    fireEvent.change(box, { target: { value: 'First send attempt' } });
    fireEvent.click(sendButton());
    expect(onSend).toHaveBeenCalledTimes(1);
    const sentFiles = onSend.mock.calls[0][0].files;
    expect(apiDeleteMock).not.toHaveBeenCalled();

    act(() => {
      ref.current!.giveBack('First send attempt', sentFiles);
    });

    // Restoring a completed uploadId does not initiate a duplicate upload
    expect(apiPostMock).toHaveBeenCalledTimes(2);
    expect(sendButton()).not.toBeDisabled();

    fireEvent.click(sendButton());
    expect(onSend).toHaveBeenCalledTimes(2);
    expect(onSend.mock.calls[1][0].files[0].uploadId).toBe('up-test-1');
  });

  it('rejects mismatched server initiate metadata without uploading truncated parts and aborts session', async () => {
    apiPostMock.mockResolvedValue(
      okResponse(
        {
          upload_id: 'up-truncated-1',
          workspace_id: 'ws-1',
          filename: 'bundle.tar',
          mime_type: 'application/x-tar',
          size_bytes: 16 * 1024 * 1024, // Mismatched vs 36 MiB actual file
          part_size: CHAT_UPLOAD_PART_SIZE,
          part_count: 2,
          state: 'uploading',
        },
        201,
      ),
    );
    apiDeleteMock.mockResolvedValue(okResponse({ upload_id: 'up-truncated-1', state: 'aborted' }));

    const { box, fileInput, sendButton, onSend } = renderComposer({
      selectedWorkspaceId: 'ws-1',
    });
    fireEvent.change(box, { target: { value: 'Must not send truncated file' } });

    const file36MiB = makeStubFile('bundle.tar', 'application/x-tar', 36 * 1024 * 1024);
    fireEvent.change(fileInput, { target: { files: [file36MiB] } });

    await waitFor(() => {
      expect(screen.getByTestId('attachment-upload-error')).toBeInTheDocument();
    });
    expect(apiPutMock).not.toHaveBeenCalled();
    expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/dalton/chat-uploads/up-truncated-1');
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('renders steered large-file and file-only deliveries across live fold and history reload without model instructions', () => {
    const attachmentChip = {
      name: 'q4_margins_200m.parquet',
      type: 'file',
      mime_type: 'application/octet-stream',
      size: 200 * 1024 * 1024,
      status: 'uploaded',
      upload_id: 'up-steer-200m',
    };
    const fileOnlyChip = {
      name: 'audio_drop.wav',
      type: 'file',
      mime_type: 'audio/wav',
      size: 64 * 1024 * 1024,
      status: 'uploaded',
      upload_id: 'up-steer-fileonly',
    };

    // 1. Live fold claims optimistic waiting steer + adds unclaimed file-only steer from another tab
    const waitingSteer: UserMessage = {
      id: 'user-live-steer-1',
      role: 'user',
      content: 'Compare Q4 vs Q3 margins',
      contentType: 'text',
      timestamp: new Date(1700000000000),
      isStreaming: false,
      steering: true,
      requestKey: 'req-steer-1',
      turnIndex: 0,
    };
    const deliveryPayload = {
      messages: [
        {
          content: 'Compare Q4 vs Q3 margins',
          request_key: 'req-steer-1',
          timestamp: 1700000001,
          attachments: [attachmentChip],
        },
        {
          content: '',
          request_key: 'req-steer-fileonly',
          timestamp: 1700000002,
          attachments: [fileOnlyChip],
        },
      ],
    };

    const entries = steeringDeliveredEntries(deliveryPayload);
    let seq = 0;
    const liveFold = foldSteeringDelivery(
      [waitingSteer] as ChatMessage[],
      entries,
      0,
      (kind) => `live-${kind}-${++seq}`,
    );

    expect(liveFold.settledKeys).toEqual(['req-steer-1']);
    const userRows = liveFold.messages.filter((m): m is UserMessage => m.role === 'user');
    expect(userRows).toHaveLength(2);
    expect(userRows[0]).toMatchObject({
      id: 'user-live-steer-1',
      content: 'Compare Q4 vs Q3 margins',
      steering: false,
      steeringDelivered: true,
      attachments: [attachmentChip],
    });
    expect(userRows[0].content).not.toContain('<system-reminder>');
    expect(userRows[1]).toMatchObject({
      content: '',
      steeringDelivered: true,
      requestKey: 'req-steer-fileonly',
      attachments: [fileOnlyChip],
    });

    // 2. History reload replays both text+file steer, file-only steer, and file-only turn opener
    let historyMessages: MessageRecord[] = [];
    const setMessages: SetMessages = (updater) => {
      historyMessages = typeof updater === 'function' ? updater(historyMessages) : updater;
    };
    const assistantByPair = new Map<number, string>();
    const pairStateByPair = new Map<number, PairState>();
    const newMessagesStartIndexRef = { current: 0 };

    handleHistoryUserMessage({
      event: {
        content: '',
        timestamp: '2026-11-09T12:00:00Z',
        metadata: {
          attachments: [fileOnlyChip],
        },
      },
      pairIndex: 0,
      assistantMessagesByPair: assistantByPair,
      pairStateByPair,
      refs: {
        recentlySentTracker: { isRecentlySent: () => false },
        currentMessageRef: { current: null },
        newMessagesStartIndexRef,
      },
      setMessages,
    });

    handleHistorySteeringDelivered({
      event: deliveryPayload,
      pairIndex: 0,
      assistantMessagesByPair: assistantByPair,
      pairStateByPair,
      refs: { newMessagesStartIndexRef },
      setMessages,
    });

    const replayedUsers = historyMessages.filter((m) => m.role === 'user');
    expect(replayedUsers).toHaveLength(3);
    // Turn opener (file-only send)
    expect(replayedUsers[0]).toMatchObject({
      id: 'history-user-0',
      content: '',
      isHistory: true,
      attachments: [fileOnlyChip],
    });
    // Steer 1 (user text + 200 MiB attachment, no <system-reminder>)
    expect(replayedUsers[1]).toMatchObject({
      content: 'Compare Q4 vs Q3 margins',
      steeringDelivered: true,
      isHistory: true,
      attachments: [attachmentChip],
    });
    expect(String(replayedUsers[1].content)).not.toContain('<system-reminder>');
    // Steer 2 (file-only steer)
    expect(replayedUsers[2]).toMatchObject({
      content: '',
      steeringDelivered: true,
      isHistory: true,
      attachments: [fileOnlyChip],
    });
  });
});
