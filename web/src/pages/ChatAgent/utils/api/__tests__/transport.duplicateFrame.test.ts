/**
 * A retransmit that raced past the route's duplicate check is refused inside
 * the stream. The send paths reach their caller with the route's own 409 for
 * it, so the adopt or retry the 409 gets runs, and the refusal never reaches
 * the processor as a failed turn.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('@/api/client', () => ({
  api: { defaults: { baseURL: '' } },
}));
vi.mock('@/lib/supabase', () => ({
  supabase: { auth: { getSession: async () => ({ data: { session: null } }) } },
}));
vi.mock('@/config/hostMode', () => ({ isPlatformMode: false, HOST_MODE: 'oss' }));

function sse(frames: Array<{ event: string; data: Record<string, unknown> }>) {
  const body = frames.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
  const bytes = new TextEncoder().encode(body);
  let sent = false;
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    body: {
      getReader: () => ({
        read: async () => (sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: bytes })),
      }),
    },
  } as unknown as Response;
}

const DUPLICATE = {
  event: 'error',
  data: {
    thread_id: 'thread-2',
    error: 'This request was already accepted; reconnect to the existing run instead of resending.',
    type: 'workflow_error',
    error_type: 'duplicate_request',
    code: 'duplicate_request',
    run_id: 'run-1',
    run_status: 'in_progress',
    run_thread_id: 'thread-1',
  },
};

describe('postSSEStream: a duplicate refused in the stream', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('rejects with the route 409, naming the copy and the thread it runs on', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(sse([DUPLICATE]));
    const { postSSEStream } = await import('../transport');
    const onEvent = vi.fn();

    await expect(postSSEStream('/api/v1/threads/thread-2/messages', {}, { onEvent })).rejects.toMatchObject({
      status: 409,
      errorInfo: { code: 'duplicate_request', run_id: 'run-1', run_status: 'in_progress', thread_id: 'thread-1' },
    });
    expect(onEvent).not.toHaveBeenCalled();
  });

  it('says so when the copy went into another turn as a steer, and only then', async () => {
    const { postSSEStream } = await import('../transport');
    const refusal = async (frame: { event: string; data: Record<string, unknown> }) => {
      (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(sse([frame]));
      return postSSEStream('/api/v1/threads/thread-1/messages', {}, { onEvent: vi.fn() }).catch((err: { errorInfo: Record<string, unknown> }) => err.errorInfo);
    };

    expect(await refusal({ ...DUPLICATE, data: { ...DUPLICATE.data, steered: true } })).toMatchObject({ run_id: 'run-1', steered: true });
    expect(await refusal(DUPLICATE)).not.toHaveProperty('steered');
    // DALTON-HOOK(antigravity-series): R5 — preserve adopted run's llm_model from SSE duplicate_request frame
    expect(await refusal({ ...DUPLICATE, data: { ...DUPLICATE.data, llm_model: 'gemini-4-neon-high-oauth' } }))
      .toMatchObject({ run_id: 'run-1', llm_model: 'gemini-4-neon-high-oauth' });
  });

  it('passes any other failure frame to the processor', async () => {
    const failure = { event: 'error', data: { thread_id: 'thread-1', error: 'boom', type: 'workflow_error' } };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(sse([failure]));
    const { postSSEStream } = await import('../transport');
    const onEvent = vi.fn();

    await expect(postSSEStream('/api/v1/threads/thread-1/messages', {}, { onEvent })).resolves.toMatchObject({ aborted: false });
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ event: 'error', error: 'boom' }));
  });

  // DALTON-HOOK(antigravity-series): R2 — stream-level START refusal frames must not trigger onRunIdResolved
  it.each([
    ['stale_fork', { error_type: 'admission_conflict', code: 'stale_fork', error: 'the branch moved' }],
    ['running', { error_type: 'admission_conflict', code: 'running', error: 'another turn is running' }],
    ['workspace_moving', { error_type: 'admission_conflict', code: 'workspace_moving', error: 'workspace is moving' }],
    ['writer_capacity', { error_type: 'capacity_limit', code: 'writer_capacity', error: 'at capacity' }],
    ['scope_changed', { error_type: 'scope_changed', code: 'scope_changed', error: 'scope changed' }],
  ])('does not invoke onRunIdResolved when the first SSE frame is a %s refusal', async (_label, payload) => {
    const refusalFrame = {
      event: 'error',
      data: {
        thread_id: 'thread-1',
        type: 'workflow_error',
        ...payload,
      },
    };
    const resp = sse([refusalFrame]);
    (resp as unknown as { headers: { get: (k: string) => string | null } }).headers = {
      get: (k: string) => (k.toLowerCase() === 'content-location' ? '/api/v1/threads/thread-1/stream?run_id=run-refused' : null),
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(resp);
    const { postSSEStream } = await import('../transport');
    const onEvent = vi.fn();
    const onRunIdResolved = vi.fn();

    await expect(postSSEStream('/api/v1/threads/thread-1/messages', {}, { onEvent, onRunIdResolved })).resolves.toMatchObject({ aborted: false });
    expect(onRunIdResolved).not.toHaveBeenCalled();
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ event: 'error', code: payload.code }));
  });

  // DALTON-HOOK(antigravity-series): R2 — unstarted retry/resend and error frames without run_id must not invoke onRunIdResolved
  it.each([
    [
      'retry/resend without run_id',
      {
        event: 'retry',
        data: {
          message: 'Temporary error occurred, you can retry or resume the turn',
          thread_id: 'thread-1',
          auto_retry: true,
          error_type: 'database_error',
          error_class: 'OperationalError',
          retry_count: 1,
          max_retries: 3,
          recovery: 'resend',
        },
      },
    ],
    [
      'error without run_id',
      {
        event: 'error',
        data: {
          thread_id: 'thread-1',
          error: 'Temporary error occurred, please retry.',
          type: 'workflow_error',
          error_type: 'database_error',
          recovery: 'resend',
        },
      },
    ],
  ])('does not invoke onRunIdResolved when the first SSE frame is %s', async (_label, frame) => {
    const resp = sse([frame]);
    (resp as unknown as { headers: { get: (k: string) => string | null } }).headers = {
      get: (k: string) => (k.toLowerCase() === 'content-location' ? '/api/v1/threads/thread-1/messages/stream?run_id=run-preallocated' : null),
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(resp);
    const { postSSEStream } = await import('../transport');
    const onEvent = vi.fn();
    const onRunIdResolved = vi.fn();

    await expect(postSSEStream('/api/v1/threads/thread-1/messages', {}, { onEvent, onRunIdResolved })).resolves.toMatchObject({ aborted: false });
    expect(onRunIdResolved).not.toHaveBeenCalled();
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ event: frame.event, recovery: 'resend' }));
  });

  // DALTON-HOOK(antigravity-series): R2 — a normal non-hold frame (or retry WITH run_id) still triggers onRunIdResolved
  it('invokes onRunIdResolved once a non-hold non-refusal frame or started retry frame arrives', async () => {
    const resp = sse([{ event: 'metadata', data: { thread_id: 'thread-1', run_id: 'run-admitted' } }]);
    (resp as unknown as { headers: { get: (k: string) => string | null } }).headers = {
      get: (k: string) => (k.toLowerCase() === 'content-location' ? '/api/v1/threads/thread-1/stream?run_id=run-admitted' : null),
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(resp);
    const { postSSEStream } = await import('../transport');
    const onEvent = vi.fn();
    const onRunIdResolved = vi.fn();

    await expect(postSSEStream('/api/v1/threads/thread-1/messages', {}, { onEvent, onRunIdResolved })).resolves.toMatchObject({ aborted: false });
    expect(onRunIdResolved).toHaveBeenCalledWith('run-admitted', 'thread-1');

    const retryWithRun = sse([{
      event: 'retry',
      data: { thread_id: 'thread-1', auto_retry: true, retry_count: 1, max_retries: 3, recovery: 'retry', run_id: 'run-started' },
    }]);
    (retryWithRun as unknown as { headers: { get: (k: string) => string | null } }).headers = {
      get: (k: string) => (k.toLowerCase() === 'content-location' ? '/api/v1/threads/thread-1/stream?run_id=run-started' : null),
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(retryWithRun);
    onRunIdResolved.mockClear();
    await expect(postSSEStream('/api/v1/threads/thread-1/messages', {}, { onEvent, onRunIdResolved })).resolves.toMatchObject({ aborted: false });
    expect(onRunIdResolved).toHaveBeenCalledWith('run-started', 'thread-1');
  });
});
