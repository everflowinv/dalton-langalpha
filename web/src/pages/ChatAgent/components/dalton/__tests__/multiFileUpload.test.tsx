import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';

// ---------------------------------------------------------------------------
// useFileUpload — serial multi-file upload (DALTON multi-file-upload).
// ---------------------------------------------------------------------------

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    // Passthrough stand-in, but with the real summary template baked in for
    // the keys whose interpolated content the tests lock (count, file names,
    // reasons). Keys without an entry come through unchanged, as before.
    t: (key: string, opts?: Record<string, unknown>) => {
      const templates: Record<string, string> = {
        'filePanel.uploadFailedSummary': '{{count}} 个文件上传失败：{{details}}',
      };
      const template = templates[key] ?? key;
      if (!opts) return template;
      let out = template;
      for (const [k, v] of Object.entries(opts)) {
        out = out.replace(new RegExp(`{{\\s*${k}\\s*}}`, 'g'), String(v));
      }
      return out;
    },
  }),
}));

vi.mock('@/pages/ChatAgent/utils/workspaceUpload', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/utils/workspaceUpload')>();
  return {
    ...actual,
    uploadToWorkspace: vi.fn(),
  };
});

import { uploadToWorkspace } from '@/pages/ChatAgent/utils/workspaceUpload';
import { useFileUpload } from '../../filePanel/useFileUpload';

const mockUpload = vi.mocked(uploadToWorkspace);

function makeFile(name: string): File {
  return new File(['content'], name, { type: 'text/plain' });
}

function dropEvent(files: File[]): unknown {
  return {
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    dataTransfer: { types: ['Files'], files },
  };
}

function changeEvent(files: File[]): unknown {
  return { target: { files } };
}

// One macrotask turn: the loops resume on microtasks after `release()`, so a
// single timer tick is enough to observe their settled state. (`handleDrop`
// returns void — the batch promise it starts is not capturable — so tests
// wait on this instead of awaiting the batch.)
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  vi.clearAllMocks();
});

describe('useFileUpload — multi-file upload', () => {
  it('uploads every dropped file serially and refreshes the list exactly once', async () => {
    let active = 0;
    let maxConcurrent = 0;
    // One microtask yield per upload, not a timer: `await act(...)` drains
    // microtask chains but not timer turns, and a timer-based mock left the
    // batch mid-flight here — its tail then leaked into the next test's call
    // counts. The single yield still catches a parallel starter, whose three
    // increments all land before the first continuation runs.
    mockUpload.mockImplementation(async () => {
      active++;
      maxConcurrent = Math.max(maxConcurrent, active);
      await Promise.resolve();
      active--;
      return { path: 'uploaded' };
    });
    const onRefreshFiles = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useFileUpload({ workspaceId: 'ws-1', onRefreshFiles }));

    await act(async () => {
      result.current.handleDrop(dropEvent([
        makeFile('a.txt'),
        makeFile('b.txt'),
        makeFile('c.txt'),
      ]) as React.DragEvent);
    });

    expect(mockUpload).toHaveBeenCalledTimes(3);
    expect(mockUpload.mock.calls.map((c) => c[1].name)).toEqual([
      'a.txt', 'b.txt', 'c.txt',
    ]);
    expect(maxConcurrent).toBe(1);
    expect(onRefreshFiles).toHaveBeenCalledTimes(1);
    expect(result.current.uploadProgress).toBeNull();
    expect(result.current.uploadStatus).toBeNull();
    expect(result.current.uploadError).toBeNull();
  });

  it('continues past a failed file, summarises failures, still refreshes once', async () => {
    mockUpload.mockImplementation(async (_ws: string, file: File) => {
      if (file.name === 'bad.txt') {
        // 413 without a detail — the front-of-server limit the page can't know.
        throw { response: { status: 413, data: {} }, message: 'Request failed with status code 413' };
      }
      return { path: file.name };
    });
    const onRefreshFiles = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useFileUpload({ workspaceId: 'ws-1', onRefreshFiles }));

    await act(async () => {
      result.current.handleDrop(dropEvent([
        makeFile('good-1.txt'),
        makeFile('bad.txt'),
        makeFile('good-2.txt'),
      ]) as React.DragEvent);
    });

    // The failed file did not stop the batch.
    expect(mockUpload).toHaveBeenCalledTimes(3);
    expect(onRefreshFiles).toHaveBeenCalledTimes(1);
    // The summary locks its content: count, file name, and the 413-without-
    // detail wording derived from the file's size.
    expect(result.current.uploadError).toBe(
      '1 个文件上传失败：bad.txt (File is too large to upload (0.0 MB).)',
    );
    expect(result.current.uploadProgress).toBeNull();
  });

  it('does not refresh the list when every upload failed', async () => {
    mockUpload.mockRejectedValue(new Error('nope'));
    const onRefreshFiles = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useFileUpload({ workspaceId: 'ws-1', onRefreshFiles }));

    await act(async () => {
      result.current.handleDrop(dropEvent([makeFile('a.txt'), makeFile('b.txt')]) as React.DragEvent);
    });

    expect(mockUpload).toHaveBeenCalledTimes(2);
    expect(onRefreshFiles).not.toHaveBeenCalled();
    expect(result.current.uploadError).toBe('2 个文件上传失败：a.txt (nope); b.txt (nope)');
  });

  it('uploads every file picked through the multi-select input', async () => {
    mockUpload.mockResolvedValue({ path: 'uploaded' });
    const onRefreshFiles = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useFileUpload({ workspaceId: 'ws-1', onRefreshFiles }));

    await act(async () => {
      result.current.handleFileInputChange(changeEvent([
        makeFile('one.md'),
        makeFile('two.md'),
      ]) as React.ChangeEvent<HTMLInputElement>);
    });

    expect(mockUpload).toHaveBeenCalledTimes(2);
    expect(onRefreshFiles).toHaveBeenCalledTimes(1);
  });

  it('exposes the batch position while uploading', async () => {
    let release: (() => void) | null = null;
    mockUpload.mockImplementation(() => new Promise<{ path: string }>((resolve) => {
      release = () => resolve({ path: 'uploaded' });
    }));
    const { result } = renderHookWithProviders(() =>
      useFileUpload({ workspaceId: 'ws-1' }));

    let batch: Promise<void> = Promise.resolve();
    act(() => {
      batch = result.current.handleUpload([makeFile('first.txt'), makeFile('second.txt')]);
    });
    expect(result.current.uploadStatus).toMatchObject({
      current: 1, total: 2, fileName: 'first.txt', percent: 0,
    });
    expect(result.current.uploadProgress).toBe(0);

    await act(async () => {
      release?.();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(result.current.uploadStatus).toMatchObject({
      current: 2, total: 2, fileName: 'second.txt',
    });

    await act(async () => {
      release?.();
      await batch;
    });
    expect(result.current.uploadStatus).toBeNull();
    expect(result.current.uploadProgress).toBeNull();
  });

  it('files dropped while a batch runs join the same queue: one loop, one refresh, growing total', async () => {
    let release: (() => void) | null = null;
    mockUpload.mockImplementation(() => new Promise<{ path: string }>((resolve) => {
      release = () => resolve({ path: 'uploaded' });
    }));
    const onRefreshFiles = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useFileUpload({ workspaceId: 'ws-1', onRefreshFiles }));

    act(() => {
      result.current.handleDrop(dropEvent([
        makeFile('a.txt'),
        makeFile('b.txt'),
      ]) as React.DragEvent);
    });
    expect(result.current.uploadStatus).toMatchObject({ current: 1, total: 2, fileName: 'a.txt' });

    // A second drop mid-run: no second loop, and the announced total grows
    // right away — done so far (0) + a.txt in flight (1) + b/c/d queued (3).
    act(() => {
      result.current.handleDrop(dropEvent([
        makeFile('c.txt'),
        makeFile('d.txt'),
      ]) as React.DragEvent);
    });
    expect(result.current.uploadStatus).toMatchObject({ current: 1, total: 4, fileName: 'a.txt' });

    await act(async () => {
      release?.();
      await settle();
    });
    expect(result.current.uploadStatus).toMatchObject({ current: 2, total: 4, fileName: 'b.txt' });

    await act(async () => {
      release?.();
      await settle();
    });
    expect(result.current.uploadStatus).toMatchObject({ current: 3, total: 4, fileName: 'c.txt' });

    await act(async () => {
      release?.();
      await settle();
    });
    expect(result.current.uploadStatus).toMatchObject({ current: 4, total: 4, fileName: 'd.txt' });

    await act(async () => {
      release?.();
      await settle();
    });
    expect(mockUpload.mock.calls.map((c) => c[1].name)).toEqual([
      'a.txt', 'b.txt', 'c.txt', 'd.txt',
    ]);
    expect(onRefreshFiles).toHaveBeenCalledTimes(1);
    expect(result.current.uploadError).toBeNull();
  });
});

describe('useFileUpload — workspace switch / unmount guard', () => {
  function renderUploadHook(initial: { ws: string; refresh: () => void }) {
    return renderHook(
      (p: { ws: string; refresh: () => void }) =>
        useFileUpload({ workspaceId: p.ws, onRefreshFiles: p.refresh }),
      { initialProps: initial },
    );
  }

  it('switching mid-flight: files dropped for the new workspace upload to it at once, the old run refreshes only its own list', async () => {
    // One resolver per upload, in call order: a1.txt's must stay reachable
    // after b1.txt starts, so the test can land them independently.
    const releases: Array<() => void> = [];
    mockUpload.mockImplementation(() => new Promise<{ path: string }>((resolve) => {
      releases.push(() => resolve({ path: 'uploaded' }));
    }));
    const refreshA = vi.fn();
    const refreshB = vi.fn();
    const { result, rerender } = renderUploadHook({ ws: 'ws-A', refresh: refreshA });

    // A's a1.txt is in flight, a2.txt queued behind it.
    act(() => {
      result.current.handleDrop(dropEvent([
        makeFile('a1.txt'),
        makeFile('a2.txt'),
      ]) as React.DragEvent);
    });
    expect(result.current.uploadStatus).toMatchObject({ current: 1, total: 2, fileName: 'a1.txt' });

    // Switch while a1.txt is STILL in flight, then drop a file for B right
    // away: it must upload to B now, not wait for A's loop to wind down.
    rerender({ ws: 'ws-B', refresh: refreshB });
    expect(result.current.uploadStatus).toBeNull();
    expect(result.current.uploadError).toBeNull();

    act(() => {
      result.current.handleDrop(dropEvent([makeFile('b1.txt')]) as React.DragEvent);
    });
    expect(mockUpload).toHaveBeenNthCalledWith(2, 'ws-B', expect.any(File), expect.objectContaining({
      signal: expect.any(AbortSignal),
      onProgress: expect.any(Function),
    }));
    expect(result.current.uploadStatus).toMatchObject({ current: 1, total: 1, fileName: 'b1.txt' });

    // a1.txt lands: A's loop abandons, refreshes A's own list once (the
    // callback it captured at start), and writes no state — B's progress
    // line is untouched, a2.txt never starts.
    await act(async () => {
      releases[0]();
      await settle();
    });
    expect(mockUpload).toHaveBeenCalledTimes(2);
    expect(result.current.uploadStatus).toMatchObject({ current: 1, total: 1, fileName: 'b1.txt' });
    expect(result.current.uploadError).toBeNull();
    expect(refreshA).toHaveBeenCalledTimes(1);
    expect(refreshB).not.toHaveBeenCalled();

    // b1.txt lands: B's loop finishes normally, refreshing B's list once.
    await act(async () => {
      releases[1]();
      await settle();
    });
    expect(result.current.uploadStatus).toBeNull();
    expect(result.current.uploadError).toBeNull();
    expect(refreshA).toHaveBeenCalledTimes(1);
    expect(refreshB).toHaveBeenCalledTimes(1);
  });

  it('unmounting mid-upload: queued files dropped, the landed file still refreshes its own list', async () => {
    let release: (() => void) | null = null;
    mockUpload.mockImplementation(() => new Promise<{ path: string }>((resolve) => {
      release = () => resolve({ path: 'uploaded' });
    }));
    const onRefreshFiles = vi.fn();
    const { result, unmount } = renderUploadHook({ ws: 'ws-A', refresh: onRefreshFiles });

    act(() => {
      result.current.handleDrop(dropEvent([
        makeFile('a1.txt'),
        makeFile('a2.txt'),
      ]) as React.DragEvent);
    });
    expect(mockUpload).toHaveBeenCalledTimes(1);

    unmount();

    await act(async () => {
      release?.();
      await settle();
    });
    // a2.txt never started; a1 landed, so its loop's captured refresh ran —
    // a cache refresh for that workspace, no state written anywhere.
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(onRefreshFiles).toHaveBeenCalledTimes(1);
  });
});
