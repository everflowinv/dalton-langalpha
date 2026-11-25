/**
 * Mobile P0 visualViewport tests: verifies that --visual-viewport-height,
 * --visual-viewport-offset-top, --visual-viewport-bottom-offset, and
 * --keyboard-overlap are published on documentElement for sheets, modals,
 * and standalone auth pages — including when the visual viewport pans
 * (offsetTop > 0) and on Android Chrome / WeChat where layout clientHeight
 * shrinks with the keyboard (overlap === 0) while dvh may not.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { trackVisualViewport } from '../visualViewport';

const LAYOUT_H = 844;
const KEYBOARD_UP_H = 490;

let clientHeightValue = LAYOUT_H;
const viewport = Object.assign(new EventTarget(), { height: LAYOUT_H, width: 390, offsetTop: 0, scale: 1 });
const root = document.documentElement;
const scrollTo = vi.fn();
const scrollIntoView = vi.fn();

function resizeViewport(next: Partial<typeof viewport>, eventType: 'resize' | 'scroll' = 'resize') {
  Object.assign(viewport, next);
  viewport.dispatchEvent(new Event(eventType));
}

function addInput(parent: HTMLElement): HTMLInputElement {
  const input = document.createElement('input');
  parent.appendChild(input);
  return input;
}

beforeAll(() => {
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
  Object.defineProperty(root, 'clientHeight', {
    configurable: true,
    get: () => clientHeightValue,
  });
  window.scrollTo = scrollTo;
  Element.prototype.scrollIntoView = scrollIntoView;
  trackVisualViewport();
});

afterEach(() => {
  clientHeightValue = LAYOUT_H;
  resizeViewport({ height: LAYOUT_H, width: 390, offsetTop: 0, scale: 1 });
  document.body.replaceChildren();
  vi.clearAllMocks();
});

describe('trackVisualViewport mobile overlays and auth pages', () => {
  it.each([
    ['a data-sheet dialog', () => {
      const sheet = document.createElement('div');
      sheet.setAttribute('role', 'dialog');
      sheet.dataset.sheet = '';
      document.body.appendChild(sheet);
      return sheet;
    }],
    ['CreateWorkspaceModal (.cwm-modal)', () => {
      const shell = document.createElement('div');
      shell.className = 'app-layout';
      const modal = document.createElement('div');
      modal.className = 'cwm-modal';
      modal.setAttribute('role', 'dialog');
      shell.appendChild(modal);
      document.body.appendChild(shell);
      return modal;
    }],
    ['a standalone auth page (.login-page)', () => {
      const page = document.createElement('div');
      page.className = 'login-page';
      document.body.appendChild(page);
      return page;
    }],
  ])('publishes visual-viewport variables without shrinking .app-layout for %s', (_, makeContainer) => {
    const input = addInput(makeContainer());
    input.focus();
    resizeViewport({ height: KEYBOARD_UP_H, offsetTop: 0 });

    expect(root.style.getPropertyValue('--visual-viewport-height')).toBe(`${KEYBOARD_UP_H}px`);
    expect(root.style.getPropertyValue('--visual-viewport-offset-top')).toBe('0px');
    expect(root.style.getPropertyValue('--visual-viewport-bottom-offset')).toBe(`${LAYOUT_H - KEYBOARD_UP_H}px`);
    expect(root.style.getPropertyValue('--keyboard-overlap')).toBe(`${LAYOUT_H - KEYBOARD_UP_H}px`);
    expect(root.dataset.keyboardOpen).toBeUndefined();
    expect(root.style.getPropertyValue('--keyboard-viewport-height')).toBe('');
    expect(scrollTo).not.toHaveBeenCalled();
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
  });

  it('publishes offsetTop and subtracts it from bottom-offset when the visual viewport pans down', () => {
    const sheet = document.createElement('div');
    sheet.dataset.sheet = '';
    document.body.appendChild(sheet);

    addInput(sheet).focus();
    resizeViewport({ height: 460, offsetTop: 110 });

    expect(root.style.getPropertyValue('--visual-viewport-height')).toBe('460px');
    expect(root.style.getPropertyValue('--visual-viewport-offset-top')).toBe('110px');
    // 844 - (110 + 460) = 274px so a 444px sheet sits in [126, 570], inside [110, 570].
    expect(root.style.getPropertyValue('--visual-viewport-bottom-offset')).toBe('274px');
    expect(root.style.getPropertyValue('--keyboard-overlap')).toBe('384px');

    // Visual viewport scroll updates offsets even without a resize event.
    resizeViewport({ offsetTop: 140 }, 'scroll');
    expect(root.style.getPropertyValue('--visual-viewport-offset-top')).toBe('140px');
    expect(root.style.getPropertyValue('--visual-viewport-bottom-offset')).toBe('244px');
  });

  it('keeps publishing --visual-viewport-height when layout clientHeight shrinks with the keyboard (Android Chrome / WeChat overlap === 0)', () => {
    const page = document.createElement('div');
    page.className = 'login-page';
    document.body.appendChild(page);

    addInput(page).focus();
    clientHeightValue = 460;
    resizeViewport({ height: 460, offsetTop: 0 });

    expect(root.style.getPropertyValue('--visual-viewport-height')).toBe('460px');
    expect(root.style.getPropertyValue('--keyboard-overlap')).toBe('');
    expect(root.style.getPropertyValue('--visual-viewport-offset-top')).toBe('');
    expect(root.style.getPropertyValue('--visual-viewport-bottom-offset')).toBe('');
    expect(root.dataset.keyboardOpen).toBeUndefined();
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
  });

  it('reveals an overlay field only once across viewport height tweaks', () => {
    const sheet = document.createElement('div');
    sheet.dataset.sheet = '';
    document.body.appendChild(sheet);

    addInput(sheet).focus();
    resizeViewport({ height: KEYBOARD_UP_H });
    resizeViewport({ height: KEYBOARD_UP_H - 12 });

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it('clears overlay variables on pinch zoom and clears keyboard offsets when the keyboard closes', () => {
    const sheet = document.createElement('div');
    sheet.dataset.sheet = '';
    document.body.appendChild(sheet);

    addInput(sheet).focus();
    resizeViewport({ height: KEYBOARD_UP_H, offsetTop: 80 });
    expect(root.style.getPropertyValue('--visual-viewport-height')).toBe(`${KEYBOARD_UP_H}px`);
    expect(root.style.getPropertyValue('--visual-viewport-offset-top')).toBe('80px');
    expect(root.style.getPropertyValue('--visual-viewport-bottom-offset')).toBe(`${LAYOUT_H - 80 - KEYBOARD_UP_H}px`);

    resizeViewport({ scale: 1.5 });
    expect(root.style.getPropertyValue('--visual-viewport-height')).toBe('');
    expect(root.style.getPropertyValue('--visual-viewport-offset-top')).toBe('');
    expect(root.style.getPropertyValue('--visual-viewport-bottom-offset')).toBe('');
    expect(root.style.getPropertyValue('--keyboard-overlap')).toBe('');

    resizeViewport({ height: KEYBOARD_UP_H, offsetTop: 0, scale: 1 });
    expect(root.style.getPropertyValue('--visual-viewport-height')).toBe(`${KEYBOARD_UP_H}px`);

    resizeViewport({ height: LAYOUT_H, offsetTop: 0, scale: 1 });
    expect(root.style.getPropertyValue('--visual-viewport-height')).toBe(`${LAYOUT_H}px`);
    expect(root.style.getPropertyValue('--visual-viewport-offset-top')).toBe('');
    expect(root.style.getPropertyValue('--visual-viewport-bottom-offset')).toBe('');
    expect(root.style.getPropertyValue('--keyboard-overlap')).toBe('');
  });
});
