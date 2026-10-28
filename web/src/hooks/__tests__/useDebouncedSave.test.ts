import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useDebouncedSave } from '../useDebouncedSave';
// DALTON-HOOK(multi-file-upload): verify stale-build reload is held across debounced and queued saves
import { reloadHeld } from '@/lib/reloadHolds';
import { runAuthResets } from '@/lib/authResets';
import { checkForNewBuild, reloadIfStale, __resetStaleBuildForTests } from '@/lib/staleBuild';

describe('useDebouncedSave', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    runAuthResets();
    __resetStaleBuildForTests();
    sessionStorage.clear();
  });
  afterEach(() => {
    vi.useRealTimers();
    runAuthResets();
    __resetStaleBuildForTests();
    sessionStorage.clear();
  });

  it('starts with idle status', () => {
    const saveFn = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));
    expect(result.current.status).toBe('idle');
  });

  it('trigger() debounces then transitions idle → saving → saved → idle', async () => {
    const saveFn = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));

    act(() => { result.current.trigger(); });
    expect(saveFn).not.toHaveBeenCalled();

    await act(async () => { vi.advanceTimersByTime(500); });
    expect(saveFn).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe('saved');

    act(() => { vi.advanceTimersByTime(2000); });
    expect(result.current.status).toBe('idle');
  });

  it('rapid trigger() calls coalesce into a single save', async () => {
    const saveFn = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));

    act(() => { result.current.trigger(); });
    act(() => { vi.advanceTimersByTime(200); });
    act(() => { result.current.trigger(); });
    act(() => { vi.advanceTimersByTime(200); });
    act(() => { result.current.trigger(); });

    await act(async () => { vi.advanceTimersByTime(500); });
    expect(saveFn).toHaveBeenCalledTimes(1);
  });

  it('flush() fires immediately and cancels pending debounce', async () => {
    const saveFn = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));

    act(() => { result.current.trigger(); });
    await act(async () => { result.current.flush(); });
    expect(saveFn).toHaveBeenCalledTimes(1);

    await act(async () => { vi.advanceTimersByTime(500); });
    expect(saveFn).toHaveBeenCalledTimes(1);
  });

  it('transitions to error status when saveFn rejects', async () => {
    const saveFn = vi.fn().mockRejectedValue(new Error('network'));
    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));

    await act(async () => { result.current.flush(); });
    expect(result.current.status).toBe('error');

    act(() => { vi.advanceTimersByTime(3000); });
    expect(result.current.status).toBe('idle');
  });

  it('queues a re-save when flush is called during an in-flight save', async () => {
    let resolveFirst!: () => void;
    const saveFn = vi.fn()
      .mockImplementationOnce(() => new Promise<void>(r => { resolveFirst = r; }))
      .mockResolvedValue(undefined);
    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));

    // Start first save
    act(() => { result.current.flush(); });
    expect(saveFn).toHaveBeenCalledTimes(1);

    // Second flush while first is in-flight — should queue, not start
    act(() => { result.current.flush(); });
    expect(saveFn).toHaveBeenCalledTimes(1);

    // Complete first save — queued save should fire
    await act(async () => { resolveFirst(); });
    expect(saveFn).toHaveBeenCalledTimes(2);
  });

  it('still runs the save queued during an in-flight save after unmount', async () => {
    let resolveFirst!: () => void;
    const saveFn = vi.fn()
      .mockImplementationOnce(() => new Promise<void>(r => { resolveFirst = r; }))
      .mockResolvedValue(undefined);
    const { result, unmount } = renderHook(() => useDebouncedSave(saveFn, 500));

    act(() => { result.current.flush(); });
    act(() => { result.current.flush(); });
    unmount();

    await act(async () => { resolveFirst(); });
    expect(saveFn).toHaveBeenCalledTimes(2);
  });

  it('goes back to idle, not saved, when the save wrote nothing', async () => {
    const saveFn = vi.fn().mockResolvedValue(false);
    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));

    await act(async () => { result.current.flush(); });
    expect(result.current.status).toBe('idle');
  });

  it('cleans up timers on unmount', async () => {
    const saveFn = vi.fn().mockResolvedValue(undefined);
    const { result, unmount } = renderHook(() => useDebouncedSave(saveFn, 500));

    act(() => { result.current.trigger(); });
    expect(reloadHeld()).toBe(true);
    unmount();
    expect(reloadHeld()).toBe(false);

    await act(async () => { vi.advanceTimersByTime(1000); });
    expect(saveFn).not.toHaveBeenCalled();
  });

  // DALTON-HOOK(multi-file-upload): section switch must not reload while a debounced or queued save is unfinished
  it('holds stale-build reload across debounced, in-flight, and queued saves, then reloads on the next switch', async () => {
    document.head.querySelectorAll('script[type="module"]').forEach((n) => n.remove());
    const script = document.createElement('script');
    script.type = 'module';
    script.src = `${window.location.origin}/assets/index-CURRENT.js`;
    document.head.appendChild(script);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ build: 'index-NEWER.js' }),
    }));
    await checkForNewBuild();
    const reloadedAway = () => sessionStorage.getItem('__la_switch_reload__');

    let resolveFirst!: () => void;
    let resolveSecond!: () => void;
    const saveFn = vi.fn()
      .mockImplementationOnce(() => new Promise<void>(r => { resolveFirst = r; }))
      .mockImplementationOnce(() => new Promise<void>(r => { resolveSecond = r; }));

    const { result } = renderHook(() => useDebouncedSave(saveFn, 500));

    // 1. Debounce timer pending -> section switch does not reload
    act(() => { result.current.trigger(); });
    expect(reloadHeld()).toBe(true);
    reloadIfStale();
    expect(reloadedAway()).toBeNull();

    // 2. Timer fires -> first save in flight; queue a second save behind it
    act(() => { vi.advanceTimersByTime(500); });
    expect(saveFn).toHaveBeenCalledTimes(1);
    act(() => { result.current.flush(); });
    expect(reloadHeld()).toBe(true);
    reloadIfStale();
    expect(reloadedAway()).toBeNull();

    // 3. First save finishes -> queued second save starts immediately; reload is still held
    await act(async () => { resolveFirst(); });
    expect(saveFn).toHaveBeenCalledTimes(2);
    expect(reloadHeld()).toBe(true);
    reloadIfStale();
    expect(reloadedAway()).toBeNull();

    // 4. Queued second save finishes -> hold is released, and the next section switch reloads
    await act(async () => { resolveSecond(); });
    expect(reloadHeld()).toBe(false);
    expect(reloadedAway()).toBeNull();

    reloadIfStale();
    expect(reloadedAway()).toBe('index-CURRENT.js');
    vi.unstubAllGlobals();
  });
});
