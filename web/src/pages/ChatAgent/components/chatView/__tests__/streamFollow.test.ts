import { afterEach, describe, expect, it, vi } from 'vitest';
import { createStreamFollow, towardStart } from '../streamFollow';

// A scroller the test moves by hand: the end is scrollHeight - clientHeight.
function scroller(top: number, height: number) {
  const c = document.createElement('div');
  const box = { top, height };
  Object.defineProperty(c, 'scrollTop', { get: () => box.top, configurable: true });
  Object.defineProperty(c, 'scrollHeight', { get: () => box.height, configurable: true });
  Object.defineProperty(c, 'clientHeight', { get: () => 1000, configurable: true });
  return { c, box };
}

afterEach(() => vi.restoreAllMocks());

describe('createStreamFollow', () => {
  it('reads a clamp as layout: the follow holds', () => {
    const { c, box } = scroller(1000, 2000);
    const following = { current: true };
    const engine = createStreamFollow(c, following);
    // A row below folds 200px; the browser clamps the view onto the new end.
    box.height = 1800;
    box.top = 800;
    expect(engine.scrolled()).toBe(false);
    expect(following.current).toBe(true);
  });

  // A notch up while a row below folds: the fold takes back the distance the
  // notch put between the view and the end.
  it('reads a move up as the reader leaving while they pull up', () => {
    const { c, box } = scroller(1000, 2000);
    const following = { current: true };
    const engine = createStreamFollow(c, following);
    engine.pulled();
    box.height = 1950;
    box.top = 900;
    expect(engine.scrolled()).toBe(true);
    expect(following.current).toBe(false);
  });

  it('lets the pull lapse', () => {
    const { c, box } = scroller(1000, 2000);
    const following = { current: true };
    const engine = createStreamFollow(c, following);
    const now = vi.spyOn(performance, 'now').mockReturnValue(1000);
    engine.pulled();
    now.mockReturnValue(2000);
    box.height = 1800;
    box.top = 800;
    expect(engine.scrolled()).toBe(false);
    expect(following.current).toBe(true);
  });
});

describe('towardStart', () => {
  it.each([
    [new WheelEvent('wheel', { deltaY: -40 }), true],
    [new WheelEvent('wheel', { deltaY: 40 }), false],
    [new KeyboardEvent('keydown', { key: 'ArrowUp' }), true],
    [new KeyboardEvent('keydown', { key: 'PageUp' }), true],
    [new KeyboardEvent('keydown', { key: ' ', shiftKey: true }), true],
    [new KeyboardEvent('keydown', { key: ' ' }), false],
    [new KeyboardEvent('keydown', { key: 'Enter' }), false],
    [new Event('pointerdown'), false],
    [new Event('touchstart'), false],
  ])('%s -> %s', (e, up) => {
    expect(towardStart(e)).toBe(up);
  });
});
