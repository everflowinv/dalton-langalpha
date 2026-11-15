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
  foldFailedSteer,
  foldSteeringDelivery,
  steeringDeliveredEntries,
} from '@/pages/ChatAgent/session/stream/steeringDelivery';
import {
  createStreamEventProcessor,
  type StreamRouterDeps,
} from '@/pages/ChatAgent/session/stream/processStreamEvent';
import type { StreamRuntime } from '@/pages/ChatAgent/session/runtime';
import { createAssistantMessage } from '@/pages/ChatAgent/hooks/utils/messageHelpers';
import { createRequestKeyTracker } from '@/pages/ChatAgent/hooks/utils/requestKey';
import { createSubagentHistoryStore } from '@/pages/ChatAgent/session/subagents/historyStore';
import {
  handleHistorySteeringDelivered,
  handleHistorySteeringReturned,
  handleHistoryUserMessage,
} from '@/pages/ChatAgent/session/history/historyHandlers';
import type { ChatMessage, UserMessage } from '@/types/chat';
import type { MessageRecord, SetMessages } from '@/pages/ChatAgent/hooks/utils/types';
import type { PairState, StreamProcessorRefs } from '@/pages/ChatAgent/session/types';
import { useChatMessages } from '@/pages/ChatAgent/hooks/useChatMessages';

const {
  apiPostMock,
  apiPutMock,
  apiGetMock,
  apiDeleteMock,
  toastSpy,
  getWorkflowStatusMock,
  replayThreadHistoryMock,
  sendChatMessageStreamMock,
  fetchThreadTurnsMock,
  getThreadFeedbackMock,
  watchThreadMock,
} = vi.hoisted(() => ({
  apiPostMock: vi.fn(),
  apiPutMock: vi.fn(),
  apiGetMock: vi.fn(),
  apiDeleteMock: vi.fn(),
  toastSpy: vi.fn(),
  getWorkflowStatusMock: vi.fn(),
  replayThreadHistoryMock: vi.fn(),
  sendChatMessageStreamMock: vi.fn(),
  fetchThreadTurnsMock: vi.fn(),
  getThreadFeedbackMock: vi.fn(),
  watchThreadMock: vi.fn(),
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

vi.mock('@/pages/ChatAgent/utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/utils/api')>();
  return {
    ...actual,
    getSkills: vi.fn().mockResolvedValue([]),
    getModelMetadata: vi.fn().mockResolvedValue({}),
    getWorkflowStatus: getWorkflowStatusMock,
    replayThreadHistory: replayThreadHistoryMock,
    sendChatMessageStream: sendChatMessageStreamMock,
    fetchThreadTurns: fetchThreadTurnsMock,
    getThreadFeedback: getThreadFeedbackMock,
    watchThread: watchThreadMock,
  };
});

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

  let smallAttSeq = 0;
  apiPostMock.mockImplementation((url: string, body?: Record<string, unknown>) => {
    // DALTON-HOOK(chat-large-upload): support upstream <=30 MiB staged attachment calls alongside >30 MiB chunked uploads
    if (url === '/api/v1/attachments') {
      smallAttSeq += 1;
      return Promise.resolve(
        okResponse(
          {
            attachment_id: `att-test-${smallAttSeq}`,
            mode: 'relay',
            max_bytes: 256 * 1024 * 1024,
          },
          201,
        ),
      );
    }
    const stagedContentMatch = /^\/api\/v1\/attachments\/([^/]+)\/content$/.exec(url);
    if (stagedContentMatch) {
      return Promise.resolve(
        okResponse({
          attachment_id: stagedContentMatch[1],
          state: 'ready',
          name: 'file',
          size: 16,
          mime_type: 'application/octet-stream',
          error: null,
        }),
      );
    }
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

  // DALTON-HOOK(chat-large-upload): keep >30 MiB files on Dalton chunked uploads while <=30 MiB files take upstream #782 staged upload
  it('accepts >30 MiB files via drag/drop and clipboard paste while routing <=30 MiB files through staged attachments', async () => {
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
      expect(screen.queryByTestId('attachment-uploading')).toBeNull();
      expect(sendButton()).not.toBeDisabled();
    });
    expect(readAsDataURLSpy).not.toHaveBeenCalled();

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
    expect(contexts[2]).toEqual({
      type: 'image',
      attachment_id: 'att-test-1',
      description: 'chart.png',
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

  it('preserves mixed [200 MiB + PNG + PDF] steer attachment order and surfaces permanent steer attachment failures live and on reload without duplication', () => {
    const mixedAttachments = [
      {
        name: 'dataset_200m.parquet',
        type: 'file',
        mime_type: 'application/octet-stream',
        size: 200 * 1024 * 1024,
        status: 'uploaded',
        upload_id: 'up-mixed-200m',
      },
      {
        name: 'margin_chart.png',
        type: 'image',
        mime_type: 'image/png',
        size: 16384,
        status: 'uploaded',
        upload_id: 'up-mixed-png',
      },
      {
        name: 'board_memo.pdf',
        type: 'pdf',
        mime_type: 'application/pdf',
        size: 32768,
        status: 'uploaded',
        upload_id: 'up-mixed-pdf',
      },
    ];

    // 1. Mixed 200 MiB + inline PNG + inline PDF preserves exact order on live delivery & replay
    const mixedDelivery = {
      messages: [
        {
          content: 'Cross-check all three files',
          request_key: 'req-mixed-1',
          timestamp: 1700000100,
          attachments: mixedAttachments,
        },
      ],
    };
    const mixedFold = foldSteeringDelivery(
      [],
      steeringDeliveredEntries(mixedDelivery),
      0,
      (kind) => `live-mixed-${kind}`,
    );
    expect(mixedFold.messages).toHaveLength(1);
    expect((mixedFold.messages[0] as UserMessage).attachments?.map((a) => a.name)).toEqual([
      'dataset_200m.parquet',
      'margin_chart.png',
      'board_memo.pdf',
    ]);
    expect((mixedFold.messages[0] as UserMessage).attachments?.map((a) => a.type)).toEqual([
      'file',
      'image',
      'pdf',
    ]);

    // 2. Permanent steer attachment failure lacking turn_index surfaces live via createStreamEventProcessor
    const assistantBubble = createAssistantMessage('assistant-turn-2', 2);
    const optimisticSteer: UserMessage = {
      id: 'user-optimistic-failed-steer',
      role: 'user',
      content: 'Use retired attachment',
      contentType: 'text',
      timestamp: new Date(1700000200000),
      isStreaming: false,
      steering: true,
      requestKey: 'req-failed-steer-1',
      turnIndex: 2,
    };
    const failedEvent = {
      event: 'steering_returned' as const,
      reason: 'attachment_failed',
      error: 'Attachment has expired or was removed',
      error_code: 'http_410',
      request_key: 'req-failed-steer-1',
      content: 'Use retired attachment',
      attachments: [mixedAttachments[0]],
      timestamp: 1700000205,
      job_id: 'job-failed-1',
    };

    let liveMessages: ChatMessage[] = [assistantBubble, optimisticSteer];
    let capturedMessageError: unknown = null;
    const requestKeyTracker = createRequestKeyTracker();
    requestKeyTracker.take('send|thread-1|Use retired attachment', {
      alongside: true,
      key: 'req-failed-steer-1',
    });

    const streamRuntime: StreamRuntime = {
      workspaceId: 'ws-1',
      threadId: 'thread-1',
      t: (key: string) => key,
      queryClient: new QueryClient(),
      updateSubagentCard: null,
      onWorkspaceCreated: null,
      onFileArtifact: null,
      onPreviewUrl: null,
      setMessages: (updater) => {
        liveMessages = typeof updater === 'function' ? updater(liveMessages) : updater;
      },
      queueMessages: (updater) => {
        liveMessages = updater(liveMessages);
      },
      flushMessages: () => {},
      setIsLoading: vi.fn(),
      setPendingInterrupt: vi.fn(),
      setTokenUsage: vi.fn(),
      setNextSummary: vi.fn(),
      setMessageError: (updater) => {
        capturedMessageError = typeof updater === 'function'
          ? updater(capturedMessageError as string | null)
          : updater;
      },
      setIsCompacting: vi.fn(),
      setCompactionTokens: vi.fn(),
      setFallbackSuggestion: vi.fn(),
      setThreadId: vi.fn(),
      setWorkspaceStarting: vi.fn(),
      setTurnHeld: vi.fn(),
      contentOrderCounterRef: { current: 0 },
      currentReasoningIdRef: { current: null },
      currentToolCallIdRef: { current: null },
      currentMessageRef: { current: 'assistant-turn-2' },
      currentRunIdRef: { current: 'run-turn-2' },
      latestRunIdRef: { current: 'run-turn-2' },
      latestRunEpochRef: { current: 1 },
      steeringAtOrderRef: { current: null },
      pendingPTCBackfillRef: { current: new Map() },
      threadIdRef: { current: 'thread-1' },
      lastEventIdRef: { current: null },
      pendingInterruptIdsRef: { current: new Set() },
      renderedInterruptIdsRef: { current: new Set() },
      unresolvedHistoryInterruptRef: { current: [] },
      terminalTaskOutcomesRef: { current: new Map() },
      subagentHistory: createSubagentHistoryStore(),
      subagentTokenUsageRef: { current: {} },
      requestKeyRef: { current: requestKeyTracker },
      offloadBatchRef: { current: { args: 0, reads: 0, timer: null } },
      nextSummaryOwedRef: { current: false },
    };

    const routerDeps: StreamRouterDeps = {
      applyFallbackSuggestion: vi.fn(),
      applyModelStatus: vi.fn(),
      clearModelStatus: vi.fn(),
      insertNotification: vi.fn(),
      releaseStreamOwnership: vi.fn(),
      attachSubagentMux: vi.fn(),
      setMarketWatch: vi.fn(),
      refreshMarketWatch: vi.fn(),
      refreshWakeups: vi.fn(),
      refreshCommands: vi.fn(),
      refreshWorkspaceFolder: null,
      armReportBack: vi.fn(),
      refreshWorkspaces: vi.fn(),
      refreshClaudeCode: vi.fn(),
      refreshClaudeCodePlan: vi.fn(), // DALTON-HOOK(chat-large-upload): upstream #740 dep
    };

    const processorRefs: StreamProcessorRefs = {
      contentOrderCounterRef: streamRuntime.contentOrderCounterRef,
      currentReasoningIdRef: streamRuntime.currentReasoningIdRef,
      currentToolCallIdRef: streamRuntime.currentToolCallIdRef,
      steeringAtOrderRef: streamRuntime.steeringAtOrderRef,
    };

    const processor = createStreamEventProcessor(
      streamRuntime,
      routerDeps,
      'assistant-turn-2',
      processorRefs,
      () => null,
    );

    processor(failedEvent);
    expect(capturedMessageError).toBe('Attachment has expired or was removed');

    const liveFailedUsers1 = liveMessages.filter((m): m is UserMessage => m.role === 'user');
    expect(liveFailedUsers1).toHaveLength(1);
    expect(liveFailedUsers1[0]).toMatchObject({
      id: 'user-optimistic-failed-steer',
      role: 'user',
      content: 'Use retired attachment',
      steering: false,
      steeringDelivered: false,
      queueRefused: true,
      queueError: 'Attachment has expired or was removed',
      requestKey: 'req-failed-steer-1',
      turnIndex: 2,
      attachments: [mixedAttachments[0]],
    });

    // Repeated live failure handling does not duplicate the bubble
    processor(failedEvent);
    const liveFailedUsers2 = liveMessages.filter((m): m is UserMessage => m.role === 'user');
    expect(liveFailedUsers2).toHaveLength(1);
    expect(foldFailedSteer(liveFailedUsers2, failedEvent, 2)).toHaveLength(1);

    // 3. History reload replaces the live bubble and stays idempotent across repeated reloads
    let historyMessages: MessageRecord[] = [...(liveMessages as unknown as MessageRecord[])];
    const setMessages: SetMessages = (updater) => {
      historyMessages = typeof updater === 'function' ? updater(historyMessages) : updater;
    };
    const newMessagesStartIndexRef = { current: historyMessages.length };
    const settledKeys: Array<string | null> = [];

    handleHistorySteeringReturned({
      event: failedEvent,
      pairIndex: 2,
      refs: { newMessagesStartIndexRef },
      setMessages,
      onCopySettled: (key) => settledKeys.push(key),
    });

    expect(settledKeys).toEqual(['req-failed-steer-1']);
    const historyFailedUsers1 = historyMessages.filter((m) => m.role === 'user');
    expect(historyFailedUsers1).toHaveLength(1);
    expect(historyFailedUsers1[0]).toMatchObject({
      id: 'history-steering-failed-2-req-failed-steer-1',
      role: 'user',
      content: 'Use retired attachment',
      isHistory: true,
      steering: false,
      steeringDelivered: false,
      queueRefused: true,
      queueError: 'Attachment has expired or was removed',
      requestKey: 'req-failed-steer-1',
      attachments: [mixedAttachments[0]],
    });

    // Second reload with the same replayed event never duplicates the failed bubble
    handleHistorySteeringReturned({
      event: failedEvent,
      pairIndex: 2,
      refs: { newMessagesStartIndexRef },
      setMessages,
    });
    expect(historyMessages.filter((m) => m.role === 'user')).toHaveLength(1);
  });

  it('surfaces parked steer failure after run_end in the already-open useChatMessages client without starting another model run or manual reload', async () => {
    const parkedAttachment = {
      name: 'revoked_parked_200m.parquet',
      type: 'file',
      mime_type: 'application/octet-stream',
      size: 200 * 1024 * 1024,
      status: 'uploaded',
      upload_id: 'up-parked-403',
    };

    let statusFailedSteers: Array<Record<string, unknown>> = [];
    let statusPendingSteers: Array<{
      content: string;
      timestamp?: number;
      request_key?: string;
      attachments?: Array<Record<string, unknown>>;
    }> = [];
    let statusLatestTurn = -1;

    getWorkflowStatusMock.mockImplementation(async () => ({
      can_reconnect: false,
      status: 'completed',
      latest_turn_index: statusLatestTurn,
      pending_steers: statusPendingSteers,
      failed_steers: statusFailedSteers,
    }));
    replayThreadHistoryMock.mockImplementation(
      async (_tid: string, onEvent: (ev: Record<string, unknown>) => void) => {
        onEvent({ event: 'replay_done', thread_id: 'thread-parked-1' });
        return { completed: true, aborted: false, disconnected: false };
      },
    );
    fetchThreadTurnsMock.mockResolvedValue({ turns: [] });
    getThreadFeedbackMock.mockResolvedValue([]);
    watchThreadMock.mockReturnValue({ abort: vi.fn() });

    let mainStreamOnEvent: ((ev: Record<string, unknown>) => void) | null = null;
    let resolveMainStream:
      | ((res: { completed: boolean; aborted: boolean; disconnected: boolean }) => void)
      | null = null;
    let steeredRequestKey: string | undefined;

    sendChatMessageStreamMock.mockImplementation(
      (
        _msg: string,
        _wsId: string,
        _tId: string,
        opts: {
          onEvent: (ev: Record<string, unknown>) => void;
          onThreadIdResolved?: (tid: string) => void;
          onRunIdResolved?: (rid: string) => void;
          requestKey?: string;
          steerOnly?: boolean;
        },
      ) => {
        if (opts.steerOnly) {
          steeredRequestKey = opts.requestKey;
          opts.onRunIdResolved?.('run-turn-0');
          opts.onEvent({
            event: 'steering_accepted',
            position: 1,
            content: 'Check revoked_parked_200m.parquet',
            attachments: [parkedAttachment],
          });
          return Promise.resolve({ completed: true, aborted: false, disconnected: false });
        }
        mainStreamOnEvent = opts.onEvent;
        opts.onThreadIdResolved?.('thread-parked-1');
        opts.onRunIdResolved?.('run-turn-0');
        opts.onEvent({
          event: 'user_message',
          role: 'user',
          content: 'Analyze Q4 baseline',
          turn_index: 0,
        });
        opts.onEvent({
          event: 'message_chunk',
          role: 'assistant',
          content_type: 'text',
          content: 'Working on Q4 baseline...',
          turn_index: 0,
        });
        return new Promise((resolve) => {
          resolveMainStream = resolve;
        });
      },
    );

    let hookState: ReturnType<typeof useChatMessages> | null = null;
    function HookHarness() {
      hookState = useChatMessages('ws-1', 'thread-parked-1');
      return null;
    }

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <HookHarness />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(hookState).not.toBeNull();
      expect(hookState!.isLoadingThread).toBe(false);
    });

    // 1. Start Turn 0 live stream
    act(() => {
      void hookState!.handleSendMessage('Analyze Q4 baseline', null, null);
    });
    await waitFor(() => {
      expect(hookState!.isLoading).toBe(true);
      expect(mainStreamOnEvent).not.toBeNull();
    });

    // 2. While Turn 0 is active, send a steer with 200 MiB attachment; it is accepted and parked
    await act(async () => {
      const accepted = await hookState!.handleSendMessage(
        'Check revoked_parked_200m.parquet',
        [
          {
            type: 'chat_upload',
            upload_id: 'up-parked-403',
            name: parkedAttachment.name,
            mime_type: parkedAttachment.mime_type,
            size: parkedAttachment.size,
          },
        ],
        [parkedAttachment],
      );
      expect(accepted).toBe(true);
    });
    expect(steeredRequestKey).toBeTruthy();

    const waitingBeforeEnd = hookState!.messages.filter(
      (m): m is UserMessage => m.role === 'user' && m.requestKey === steeredRequestKey,
    );
    expect(waitingBeforeEnd).toHaveLength(1);
    expect(waitingBeforeEnd[0]).toMatchObject({
      content: 'Check revoked_parked_200m.parquet',
      steering: true,
      queuePosition: 1,
      attachments: [parkedAttachment],
    });
    expect(waitingBeforeEnd[0].queueError).toBeUndefined();

    // 3. Turn 0 emits run_end and closes; parked steer then fails in outbox (e.g., 403 permission revoked)
    statusLatestTurn = 0;
    statusPendingSteers = [];
    statusFailedSteers = [
      {
        reason: 'attachment_failed',
        error: 'Workspace write permission required for attachment upload.',
        error_code: 'http_403',
        request_key: steeredRequestKey,
        content: 'Check revoked_parked_200m.parquet',
        attachments: [parkedAttachment],
        timestamp: 1700000300,
        job_id: 'job-parked-403',
        turn_index: 0,
      },
    ];

    await act(async () => {
      mainStreamOnEvent!({
        event: 'run_end',
        thread_id: 'thread-parked-1',
        run_id: 'run-turn-0',
      });
      resolveMainStream!({ completed: true, aborted: false, disconnected: false });
    });

    // 4. Open client automatically reconciles after run_end without starting another model run or manual reload
    await waitFor(() => {
      const steerAfter = hookState!.messages.find(
        (m): m is UserMessage => m.role === 'user' && m.requestKey === steeredRequestKey,
      );
      expect(steerAfter).toBeDefined();
      expect(steerAfter).toMatchObject({
        content: 'Check revoked_parked_200m.parquet',
        steering: false,
        steeringDelivered: false,
        queueRefused: true,
        queueError: 'Workspace write permission required for attachment upload.',
        requestKey: steeredRequestKey,
        attachments: [parkedAttachment],
      });
      expect(hookState!.messageError).toBe(
        'Workspace write permission required for attachment upload.',
      );
    });

    // No extra model run was started (only Turn 0 + the steer POST)
    expect(sendChatMessageStreamMock).toHaveBeenCalledTimes(2);
    // User bubbles are never duplicated
    const allSteerBubbles = hookState!.messages.filter(
      (m): m is UserMessage => m.role === 'user' && m.requestKey === steeredRequestKey,
    );
    expect(allSteerBubbles).toHaveLength(1);
  });

  it('reconciles multiple waiting steers failing at different times, across transient offline errors, and past 6 seconds after run_end', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const att1 = {
        name: 'first_revoked_200m.bin',
        type: 'file',
        mime_type: 'application/octet-stream',
        size: 200 * 1024 * 1024,
        status: 'uploaded',
        upload_id: 'up-multi-1',
      };
      const att2 = {
        name: 'second_delayed_fail.pdf',
        type: 'pdf',
        mime_type: 'application/pdf',
        size: 4 * 1024 * 1024,
        status: 'uploaded',
        upload_id: 'up-multi-2',
      };

      let statusFailedSteers: Array<Record<string, unknown>> = [];
      let statusPendingSteers: Array<{
        content: string;
        timestamp?: number;
        request_key?: string;
        attachments?: Array<Record<string, unknown>>;
      }> = [];
      let statusLatestTurn = -1;
      let shouldThrowNetworkError = false;

      getWorkflowStatusMock.mockImplementation(async () => {
        if (shouldThrowNetworkError) {
          throw new Error('Network offline');
        }
        return {
          can_reconnect: false,
          status: 'completed',
          latest_turn_index: statusLatestTurn,
          pending_steers: statusPendingSteers,
          failed_steers: statusFailedSteers,
        };
      });
      replayThreadHistoryMock.mockImplementation(
        async (_tid: string, onEvent: (ev: Record<string, unknown>) => void) => {
          onEvent({ event: 'replay_done', thread_id: 'thread-parked-multi' });
          return { completed: true, aborted: false, disconnected: false };
        },
      );
      fetchThreadTurnsMock.mockResolvedValue({ turns: [] });
      getThreadFeedbackMock.mockResolvedValue([]);
      watchThreadMock.mockReturnValue({ abort: vi.fn() });

      let mainStreamOnEvent: ((ev: Record<string, unknown>) => void) | null = null;
      let resolveMainStream:
        | ((res: { completed: boolean; aborted: boolean; disconnected: boolean }) => void)
        | null = null;
      const steeredKeys: string[] = [];

      sendChatMessageStreamMock.mockImplementation(
        (
          msg: string,
          _wsId: string,
          _tId: string,
          opts: {
            onEvent: (ev: Record<string, unknown>) => void;
            onThreadIdResolved?: (tid: string) => void;
            onRunIdResolved?: (rid: string) => void;
            requestKey?: string;
            steerOnly?: boolean;
          },
        ) => {
          if (opts.steerOnly) {
            if (opts.requestKey) steeredKeys.push(opts.requestKey);
            opts.onRunIdResolved?.('run-multi-0');
            const isFirst = steeredKeys.length === 1;
            opts.onEvent({
              event: 'steering_accepted',
              position: steeredKeys.length,
              content: msg,
              attachments: [isFirst ? att1 : att2],
            });
            return Promise.resolve({ completed: true, aborted: false, disconnected: false });
          }
          mainStreamOnEvent = opts.onEvent;
          opts.onThreadIdResolved?.('thread-parked-multi');
          opts.onRunIdResolved?.('run-multi-0');
          opts.onEvent({
            event: 'user_message',
            role: 'user',
            content: 'Run multi-steer baseline',
            turn_index: 0,
          });
          opts.onEvent({
            event: 'message_chunk',
            role: 'assistant',
            content_type: 'text',
            content: 'Processing baseline...',
            turn_index: 0,
          });
          return new Promise((resolve) => {
            resolveMainStream = resolve;
          });
        },
      );

      let hookState: ReturnType<typeof useChatMessages> | null = null;
      function HookHarness() {
        hookState = useChatMessages('ws-1', 'thread-parked-multi');
        return null;
      }

      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <HookHarness />
          </MemoryRouter>
        </QueryClientProvider>,
      );

      await waitFor(() => {
        expect(hookState).not.toBeNull();
        expect(hookState!.isLoadingThread).toBe(false);
      });

      act(() => {
        void hookState!.handleSendMessage('Run multi-steer baseline', null, null);
      });
      await waitFor(() => {
        expect(hookState!.isLoading).toBe(true);
        expect(mainStreamOnEvent).not.toBeNull();
      });

      // Admit two steers while Turn 0 is active
      await act(async () => {
        await hookState!.handleSendMessage(
          'First parked steer',
          [{ type: 'chat_upload', upload_id: 'up-multi-1', name: att1.name, size: att1.size }],
          [att1],
        );
        await hookState!.handleSendMessage(
          'Second delayed parked steer',
          [{ type: 'chat_upload', upload_id: 'up-multi-2', name: att2.name, size: att2.size }],
          [att2],
        );
      });
      expect(steeredKeys).toHaveLength(2);
      const [rk1, rk2] = steeredKeys;

      // Turn 0 ends while client is temporarily offline; both steers are still parked
      statusLatestTurn = 0;
      shouldThrowNetworkError = true;
      statusPendingSteers = [
        { content: 'First parked steer', request_key: rk1, attachments: [att1] },
        { content: 'Second delayed parked steer', request_key: rk2, attachments: [att2] },
      ];

      await act(async () => {
        mainStreamOnEvent!({
          event: 'run_end',
          thread_id: 'thread-parked-multi',
          run_id: 'run-multi-0',
        });
        resolveMainStream!({ completed: true, aborted: false, disconnected: false });
      });

      // Advance past initial poll (which rejects with Network offline)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(80);
      });

      // Network recovers and Steer 1 fails first while Steer 2 remains parked in pending_steers
      shouldThrowNetworkError = false;
      statusPendingSteers = [
        { content: 'Second delayed parked steer', request_key: rk2, attachments: [att2] },
      ];
      statusFailedSteers = [
        {
          reason: 'attachment_failed',
          error: 'First attachment failed (403).',
          error_code: 'http_403',
          request_key: rk1,
          content: 'First parked steer',
          attachments: [att1],
          timestamp: 1700000401,
          job_id: 'job-multi-1',
          turn_index: 0,
        },
      ];

      await act(async () => {
        window.dispatchEvent(new Event('online'));
        await vi.advanceTimersByTimeAsync(50);
      });

      await waitFor(() => {
        const b1 = hookState!.messages.find(
          (m): m is UserMessage => m.role === 'user' && m.requestKey === rk1,
        );
        const b2 = hookState!.messages.find(
          (m): m is UserMessage => m.role === 'user' && m.requestKey === rk2,
        );
        expect(b1).toMatchObject({
          steering: false,
          queueRefused: true,
          queueError: 'First attachment failed (403).',
        });
        // Steer 2 is still waiting
        expect(b2).toMatchObject({
          steering: true,
        });
        expect(b2?.queueError).toBeUndefined();
      });

      // Advance 8,000 ms (> 6s!) while Steer 2 is still parked and being verified by outbox drainer
      await act(async () => {
        await vi.advanceTimersByTimeAsync(8000);
      });

      // Now at t > 8s after run_end, Steer 2 fails permanently
      statusPendingSteers = [];
      statusFailedSteers = [
        ...statusFailedSteers,
        {
          reason: 'attachment_failed',
          error: 'Second attachment expired after 8s (410).',
          error_code: 'http_410',
          request_key: rk2,
          content: 'Second delayed parked steer',
          attachments: [att2],
          timestamp: 1700000409,
          job_id: 'job-multi-2',
          turn_index: 0,
        },
      ];

      await act(async () => {
        await vi.advanceTimersByTimeAsync(700);
      });

      await waitFor(() => {
        const b2After = hookState!.messages.find(
          (m): m is UserMessage => m.role === 'user' && m.requestKey === rk2,
        );
        expect(b2After).toMatchObject({
          content: 'Second delayed parked steer',
          steering: false,
          steeringDelivered: false,
          queueRefused: true,
          queueError: 'Second attachment expired after 8s (410).',
          requestKey: rk2,
          attachments: [att2],
        });
        expect(hookState!.messageError).toBe('Second attachment expired after 8s (410).');
      });

      // Once all waiting steers have settled, reconciliation stops polling
      const callsAfterAllSettled = getWorkflowStatusMock.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(getWorkflowStatusMock.mock.calls.length).toBe(callsAfterAllSettled);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reconciles /status failed_steers against the loaded 20-turn page window without duplicating older turns on loadOlderHistory', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const attOld = {
        name: 'turn2_expired.bin',
        type: 'file' as const,
        mime_type: 'application/octet-stream',
        size: 16 * 1024 * 1024,
        status: 'uploaded' as const,
        upload_id: 'up-paged-old',
      };
      const attMid = {
        name: 'turn10_revoked.pdf',
        type: 'pdf' as const,
        mime_type: 'application/pdf',
        size: 64 * 1024,
        status: 'uploaded' as const,
        upload_id: 'up-paged-mid',
      };
      const attCur = {
        name: 'turn24_parked.png',
        type: 'image' as const,
        mime_type: 'image/png',
        size: 8192,
        status: 'uploaded' as const,
        upload_id: 'up-paged-cur',
      };

      const failOld = {
        reason: 'attachment_failed',
        error: 'Turn 2 attachment expired (410).',
        error_code: 'http_410',
        request_key: 'rk-paged-turn-2',
        job_id: 'job-paged-turn-2',
        turn_index: 2,
        content: 'Old failed steer at turn 2',
        attachments: [attOld],
        timestamp: 1700000502,
      };
      const failMid = {
        reason: 'attachment_failed',
        error: 'Turn 10 attachment revoked (403).',
        error_code: 'http_403',
        request_key: 'rk-paged-turn-10',
        job_id: 'job-paged-turn-10',
        turn_index: 10,
        content: 'In-window failed steer at turn 10',
        attachments: [attMid],
        timestamp: 1700000510,
      };
      const failCur = {
        reason: 'attachment_failed',
        error: 'Turn 24 attachment expired (410).',
        error_code: 'http_410',
        request_key: 'rk-paged-turn-24',
        job_id: 'job-paged-turn-24',
        turn_index: 24,
        content: 'Current turn 24 parked steer',
        attachments: [attCur],
        timestamp: 1700000524,
      };

      let statusPendingSteers: Array<Record<string, unknown>> = [
        {
          content: 'Current turn 24 parked steer',
          request_key: 'rk-paged-turn-24',
          attachments: [attCur],
          timestamp: 1700000520,
        },
      ];
      let statusFailedSteers: Array<Record<string, unknown>> = [failOld, failMid];

      getWorkflowStatusMock.mockImplementation(async () => ({
        status: 'completed',
        can_reconnect: false,
        latest_turn_index: 24,
        pending_steers: statusPendingSteers,
        failed_steers: statusFailedSteers,
        active_tasks: [],
      }));

      replayThreadHistoryMock.mockImplementation(
        async (
          _tid: string,
          onEvent: (ev: Record<string, unknown>) => void,
          opts?: { limit?: number; beforeTurn?: number },
        ) => {
          if (opts?.beforeTurn === 5) {
            onEvent({
              event: 'history_page',
              first_turn_index: 0,
              has_more: false,
            });
            for (let t = 0; t <= 4; t++) {
              onEvent({
                event: 'user_message',
                role: 'user',
                content: `Question turn ${t}`,
                turn_index: t,
                run_id: `run-paged-${t}`,
              });
              onEvent({
                event: 'message_chunk',
                role: 'assistant',
                content_type: 'text',
                content: `Answer turn ${t}`,
                turn_index: t,
                finish_reason: 'stop',
              });
              if (t === 2) {
                onEvent({
                  event: 'steering_returned',
                  ...failOld,
                });
              }
            }
            onEvent({
              event: 'replay_done',
              thread_id: 'thread-paged-steers',
              latest_run_id: 'run-paged-4',
            });
            return { completed: true, aborted: false, disconnected: false };
          }

          // Initial newest 20-turn page: turns 5..24
          onEvent({
            event: 'history_page',
            first_turn_index: 5,
            has_more: true,
          });
          for (let t = 5; t <= 24; t++) {
            onEvent({
              event: 'user_message',
              role: 'user',
              content: `Question turn ${t}`,
              turn_index: t,
              run_id: `run-paged-${t}`,
            });
            onEvent({
              event: 'message_chunk',
              role: 'assistant',
              content_type: 'text',
              content: `Answer turn ${t}`,
              turn_index: t,
              finish_reason: 'stop',
            });
          }
          onEvent({
            event: 'replay_done',
            thread_id: 'thread-paged-steers',
            latest_run_id: 'run-paged-24',
          });
          return { completed: true, aborted: false, disconnected: false };
        },
      );

      let hookState: ReturnType<typeof useChatMessages> | null = null;
      function PagedHookHarness() {
        hookState = useChatMessages('ws-1', 'thread-paged-steers');
        return null;
      }

      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <PagedHookHarness />
          </MemoryRouter>
        </QueryClientProvider>,
      );

      await waitFor(() => {
        expect(hookState).not.toBeNull();
        expect(hookState!.isLoadingThread).toBe(false);
        // DALTON-HOOK(chat-large-upload): paging status is on the history snapshot
        expect(hookState!.olderHistory.snapshot.status).toBe('idle');
      });

      // 1. While only turns 5..24 are loaded:
      //    - Turn 2's failed steer (outside the [5..24] window) is NOT rendered or appended at the bottom.
      //    - Turn 10's failed steer (inside [5..24]) is inserted right after history-assistant-10 and before history-user-11.
      //    - Turn 24's pending steer is waiting at the end of turn 24.
      const msgsInitial = hookState!.messages;
      expect(
        msgsInitial.filter(
          (m) => m.role === 'user' && (m as UserMessage).requestKey === 'rk-paged-turn-2',
        ),
      ).toHaveLength(0);

      const idxAss10 = msgsInitial.findIndex((m) => m.id === 'history-assistant-10');
      const idxUser11 = msgsInitial.findIndex((m) => m.id === 'history-user-11');
      const idxFail10 = msgsInitial.findIndex(
        (m) => m.role === 'user' && (m as UserMessage).requestKey === 'rk-paged-turn-10',
      );
      expect(idxAss10).toBeGreaterThan(-1);
      expect(idxFail10).toBe(idxAss10 + 1);
      expect(idxUser11).toBe(idxFail10 + 1);

      const pending24 = msgsInitial.find(
        (m): m is UserMessage => m.role === 'user' && m.requestKey === 'rk-paged-turn-24',
      );
      expect(pending24).toMatchObject({
        content: 'Current turn 24 parked steer',
        steering: true,
        turnIndex: 24,
      });

      // 2. Turn 24's pending steer fails on the next /status poll while turns 0..4 are still unloaded:
      statusPendingSteers = [];
      statusFailedSteers = [failOld, failMid, failCur];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120);
      });

      await waitFor(() => {
        const curBubble = hookState!.messages.find(
          (m): m is UserMessage => m.role === 'user' && m.requestKey === 'rk-paged-turn-24',
        );
        expect(curBubble).toMatchObject({
          steering: false,
          queueRefused: true,
          queueError: 'Turn 24 attachment expired (410).',
          turnIndex: 24,
        });
      });

      // Turn 2's failed steer is STILL not appended while turns 0..4 are unloaded
      expect(
        hookState!.messages.filter(
          (m) => m.role === 'user' && (m as UserMessage).requestKey === 'rk-paged-turn-2',
        ),
      ).toHaveLength(0);

      // 3. Now load the older history page (turns 0..4) via olderHistory.load():
      await act(async () => {
        hookState!.olderHistory.load();
      });

      await waitFor(() => {
        // DALTON-HOOK(chat-large-upload): paging status is on the history snapshot
        expect(hookState!.olderHistory.snapshot.status).toBe('end');
      });

      // Turn 2's failed steer now appears EXACTLY ONCE, positioned at turn 2 (after history-assistant-2, before history-user-3)!
      const msgsAfterOlder = hookState!.messages;
      const matchesTurn2 = msgsAfterOlder.filter(
        (m) =>
          m.role === 'user' &&
          ((m as UserMessage).requestKey === 'rk-paged-turn-2' ||
            m.content === 'Old failed steer at turn 2'),
      );
      expect(matchesTurn2).toHaveLength(1);
      expect(matchesTurn2[0]).toMatchObject({
        id: 'history-steering-failed-2-rk-paged-turn-2',
        content: 'Old failed steer at turn 2',
        queueRefused: true,
        queueError: 'Turn 2 attachment expired (410).',
        turnIndex: 2,
        requestKey: 'rk-paged-turn-2',
      });

      const idxAss2 = msgsAfterOlder.findIndex((m) => m.id === 'history-assistant-2');
      const idxFail2 = msgsAfterOlder.findIndex(
        (m) => m.id === 'history-steering-failed-2-rk-paged-turn-2',
      );
      const idxUser3 = msgsAfterOlder.findIndex((m) => m.id === 'history-user-3');
      expect(idxAss2).toBeGreaterThan(-1);
      expect(idxFail2).toBe(idxAss2 + 1);
      expect(idxUser3).toBe(idxFail2 + 1);

      // Turn 10 and Turn 24 failed steers also appear exactly once in their respective positions
      expect(
        msgsAfterOlder.filter(
          (m) => m.role === 'user' && (m as UserMessage).requestKey === 'rk-paged-turn-10',
        ),
      ).toHaveLength(1);
      expect(
        msgsAfterOlder.filter(
          (m) => m.role === 'user' && (m as UserMessage).requestKey === 'rk-paged-turn-24',
        ),
      ).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
