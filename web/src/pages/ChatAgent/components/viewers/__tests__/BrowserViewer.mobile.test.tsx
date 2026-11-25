import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import '@/i18n';
import type { BrowserViewAnswer } from '../../../utils/api/sandbox';
import { BrowserViewSession } from '../../filePanel/browserStream';
import { BrowserViewer } from '../BrowserViewer';

class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  readyState = FakeSocket.OPEN;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  constructor() { FakeSocket.last = this; }
  send() {}
  close() {}
}

class FakeImage {
  naturalWidth = 400;
  naturalHeight = 300;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  set src(_: string) { this.onload?.(); }
}

let view: BrowserViewSession;
let stopWatching: (() => void) | null = null;
const originalMatchMedia = window.matchMedia;

function setCoarsePointer(matches: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn((query: string) => ({
      matches: query === '(pointer: coarse)' ? matches : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

function makeTouchList(touches: Array<{ identifier: number; clientX: number; clientY: number }>): TouchList {
  const list = Object.assign([...touches], {
    item: (index: number) => touches[index] ?? null,
  });
  return list as unknown as TouchList;
}

function dispatchTouch(
  target: EventTarget,
  type: 'touchstart' | 'touchmove' | 'touchend',
  touches: Array<{ identifier: number; clientX: number; clientY: number }>,
  changedTouches = touches,
) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'touches', { value: makeTouchList(touches) });
  Object.defineProperty(event, 'changedTouches', { value: makeTouchList(changedTouches) });
  target.dispatchEvent(event);
  return event;
}

async function mountHeld(coarse: boolean, box = { width: 400, height: 300 }) {
  setCoarsePointer(coarse);
  vi.stubGlobal('WebSocket', FakeSocket);
  vi.stubGlobal('Image', FakeImage);
  view = new BrowserViewSession();
  const ask = vi.fn<() => Promise<BrowserViewAnswer>>(async () => ({
    state: 'running',
    url: 'http://9224.preview.localhost/b/t',
  }));
  render(<BrowserViewer view={view} />);
  await act(async () => {
    stopWatching = view.watch({
      ask,
      control: async () => true,
      navigate: async () => null,
      local: false,
    });
  });
  await act(async () => {
    FakeSocket.last?.onmessage?.({
      data: JSON.stringify({ type: 'tabs', tabs: [{ active: true, url: 'https://example.com/' }] }),
    });
    FakeSocket.last?.onmessage?.({
      data: JSON.stringify({ type: 'frame', seq: 1, data: 'AAAA', metadata: { deviceWidth: 800 } }),
    });
  });
  await act(async () => { view.takeControl(); });
  const canvas = screen.getByRole('img', { hidden: true });
  canvas.getBoundingClientRect = () => ({
    left: 0,
    top: 0,
    width: box.width,
    height: box.height,
    right: box.width,
    bottom: box.height,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  });
  const input = vi.spyOn(view, 'input');
  return { canvas, input };
}

afterEach(() => {
  act(() => { stopWatching?.(); });
  stopWatching = null;
  vi.unstubAllGlobals();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: originalMatchMedia,
  });
  vi.restoreAllMocks();
});

describe('BrowserViewer touch controls', () => {
  it('does not auto-focus the hidden keyboard textarea on coarse pointers, and toggles it on demand via the keyboard button', async () => {
    await mountHeld(true);
    const keys = screen.getByRole('textbox', { hidden: true });
    expect(document.activeElement).not.toBe(keys);

    const kbdBtn = screen.getByRole('button', { name: /Show keyboard|呼出键盘/i });
    expect(kbdBtn).toHaveAttribute('aria-pressed', 'false');

    fireEvent.mouseDown(kbdBtn);
    fireEvent.click(kbdBtn);
    expect(document.activeElement).toBe(keys);
    expect(kbdBtn).toHaveAttribute('aria-pressed', 'true');

    fireEvent.mouseDown(kbdBtn);
    fireEvent.click(kbdBtn);
    expect(document.activeElement).not.toBe(keys);
    expect(kbdBtn).toHaveAttribute('aria-pressed', 'false');
  });

  it('sends a single remote click per tap, cancels touchstart, and ignores trailing synthetic mouse events', async () => {
    const { canvas, input } = await mountHeld(true);
    const keys = screen.getByRole('textbox', { hidden: true });

    let startEv!: Event;
    let endEv!: Event;
    act(() => {
      startEv = dispatchTouch(canvas, 'touchstart', [{ identifier: 1, clientX: 100, clientY: 50 }]);
      endEv = dispatchTouch(canvas, 'touchend', [], [{ identifier: 1, clientX: 100, clientY: 50 }]);
      // Simulate browser-synthesized compatibility mouse events right after touchend.
      fireEvent.mouseDown(canvas, { clientX: 100, clientY: 50, button: 0 });
      fireEvent.mouseUp(window, { clientX: 100, clientY: 50, button: 0 });
    });

    expect(startEv.defaultPrevented).toBe(true);
    expect(endEv.defaultPrevented).toBe(true);
    expect(document.activeElement).not.toBe(keys);
    expect(input.mock.calls.map(([ev]) => ev)).toEqual([
      expect.objectContaining({ kind: 'mouse', event: 'down', button: 'left', x: 200, y: 100 }),
      expect.objectContaining({ kind: 'mouse', event: 'up', button: 'left', x: 200, y: 100 }),
    ]);
  });

  it('ignores mouse events explicitly flagged as touch-synthesized via sourceCapabilities', async () => {
    const { canvas, input } = await mountHeld(true);

    act(() => {
      const ev = new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: 100, clientY: 50, button: 0 });
      Object.defineProperty(ev, 'sourceCapabilities', { value: { firesTouchEvents: true } });
      canvas.dispatchEvent(ev);
    });

    expect(input).not.toHaveBeenCalled();
  });

  it('does not send a click on a short flick whose last touchmove stayed under threshold but touchend crossed it', async () => {
    const { canvas, input } = await mountHeld(true);

    act(() => {
      dispatchTouch(canvas, 'touchstart', [{ identifier: 1, clientX: 100, clientY: 100 }]);
      // 4px move (< 8px threshold), then lift-off at 15px total displacement.
      dispatchTouch(canvas, 'touchmove', [{ identifier: 1, clientX: 100, clientY: 104 }]);
      dispatchTouch(canvas, 'touchend', [], [{ identifier: 1, clientX: 100, clientY: 115 }]);
      fireEvent.mouseDown(canvas, { clientX: 100, clientY: 115, button: 0 });
      fireEvent.mouseUp(window, { clientX: 100, clientY: 115, button: 0 });
    });

    const clicks = input.mock.calls.filter(([ev]) => ev.kind === 'mouse' && (ev.event === 'down' || ev.event === 'up'));
    expect(clicks).toHaveLength(0);
  });

  it('maps a single-finger swipe to remote mouseWheel scroll instead of a click', async () => {
    const rafCallbacks: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      rafCallbacks.push(cb);
      return rafCallbacks.length;
    });
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    const { canvas, input } = await mountHeld(true);

    act(() => {
      dispatchTouch(canvas, 'touchstart', [{ identifier: 1, clientX: 100, clientY: 200 }]);
      dispatchTouch(canvas, 'touchmove', [{ identifier: 1, clientX: 100, clientY: 140 }]);
      dispatchTouch(canvas, 'touchend', [], [{ identifier: 1, clientX: 100, clientY: 140 }]);
      fireEvent.mouseDown(canvas, { clientX: 100, clientY: 140, button: 0 });
      fireEvent.mouseUp(window, { clientX: 100, clientY: 140, button: 0 });
      for (const cb of rafCallbacks) cb(16);
    });

    expect(input).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'mouse',
      event: 'wheel',
      deltaX: 0,
      deltaY: 120,
    }));
    const clicks = input.mock.calls.filter(([ev]) => ev.kind === 'mouse' && (ev.event === 'down' || ev.event === 'up'));
    expect(clicks).toHaveLength(0);
  });

  it('scrolls the remote page when a single-finger swipe starts in the blank letterbox area below the frame', async () => {
    const rafCallbacks: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      rafCallbacks.push(cb);
      return rafCallbacks.length;
    });
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    // Portrait 390x700 canvas with a 400x300 frame contained at top (shown height = 292.5px; y > 293 is letterbox).
    const { canvas, input } = await mountHeld(true, { width: 390, height: 700 });

    act(() => {
      dispatchTouch(canvas, 'touchstart', [{ identifier: 1, clientX: 195, clientY: 560 }]);
      dispatchTouch(canvas, 'touchmove', [{ identifier: 1, clientX: 195, clientY: 480 }]);
      dispatchTouch(canvas, 'touchend', [], [{ identifier: 1, clientX: 195, clientY: 480 }]);
      for (const cb of rafCallbacks) cb(16);
    });

    expect(input).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'mouse',
      event: 'wheel',
      deltaX: 0,
      x: 400,
    }));
    const wheelEvent = input.mock.calls.map(([ev]) => ev).find((ev) => ev.kind === 'mouse' && ev.event === 'wheel');
    expect(wheelEvent && wheelEvent.kind === 'mouse' && wheelEvent.event === 'wheel' ? wheelEvent.deltaY : 0).toBeGreaterThan(0);
    const clicks = input.mock.calls.filter(([ev]) => ev.kind === 'mouse' && (ev.event === 'down' || ev.event === 'up'));
    expect(clicks).toHaveLength(0);
  });

  it('does not register touch gesture listeners on fine-pointer hybrid laptops and preserves desktop auto-focus', async () => {
    const addSpy = vi.spyOn(HTMLCanvasElement.prototype, 'addEventListener');
    const { canvas, input } = await mountHeld(false);
    const keys = screen.getByRole('textbox', { hidden: true });
    expect(document.activeElement).toBe(keys);

    const registeredTypes = addSpy.mock.calls.map(([type]) => type);
    expect(registeredTypes).not.toContain('touchstart');
    expect(registeredTypes).not.toContain('touchmove');
    expect(registeredTypes).not.toContain('touchend');

    // Touch events on a fine-pointer touchscreen laptop must not trigger coarse-pointer remote scrolling/clicking.
    act(() => {
      const startEv = dispatchTouch(canvas, 'touchstart', [{ identifier: 1, clientX: 100, clientY: 200 }]);
      dispatchTouch(canvas, 'touchmove', [{ identifier: 1, clientX: 100, clientY: 120 }]);
      dispatchTouch(canvas, 'touchend', [], [{ identifier: 1, clientX: 100, clientY: 120 }]);
      expect(startEv.defaultPrevented).toBe(false);
    });
    expect(input).not.toHaveBeenCalled();

    // Standard desktop mouse click still works normally.
    fireEvent.mouseDown(canvas, { clientX: 100, clientY: 50, button: 0 });
    fireEvent.mouseUp(window, { clientX: 100, clientY: 50, button: 0 });
    expect(input).toHaveBeenCalledWith(expect.objectContaining({ kind: 'mouse', event: 'down', button: 'left', x: 200, y: 100 }));
  });

  it('forwards touch-synthesized compatibility mouse clicks (firesTouchEvents: true) on fine-pointer touchscreen laptops', async () => {
    const { canvas, input } = await mountHeld(false);

    act(() => {
      const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: 100, clientY: 50, button: 0 });
      Object.defineProperty(down, 'sourceCapabilities', { value: { firesTouchEvents: true } });
      canvas.dispatchEvent(down);

      const up = new MouseEvent('mouseup', { bubbles: true, cancelable: true, clientX: 100, clientY: 50, button: 0 });
      Object.defineProperty(up, 'sourceCapabilities', { value: { firesTouchEvents: true } });
      window.dispatchEvent(up);
    });

    expect(input.mock.calls.map(([ev]) => ev)).toEqual([
      expect.objectContaining({ kind: 'mouse', event: 'down', button: 'left', x: 200, y: 100 }),
      expect.objectContaining({ kind: 'mouse', event: 'up', button: 'left', x: 200, y: 100 }),
    ]);
  });
});
