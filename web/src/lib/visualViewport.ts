/**
 * Fits the app shell to what an on-screen keyboard leaves visible.
 *
 * iOS never resizes the layout viewport or `dvh` for the keyboard. It pans the
 * whole page instead, which takes the shell's own header off the top and puts
 * content under the status bar, and react-aria measures its popovers from the
 * panned page, so a composer menu has no height left. The shell takes the
 * visible height here and the pan is undone.
 *
 * Only a field in the shell is fitted this way. An overlay floats over the shell
 * and doesn't shrink with it, so Safari's pan is what reveals a field there, and
 * a shrunken shell would leave the page too short to pan.
 */

/** More than Safari's toolbars can account for, less than any keyboard. */
const KEYBOARD_MIN_PX = 120;

/** A pinch shrinks the visual viewport too, and that is not a keyboard. */
const PINCH_SCALE = 1.01;

/** How far the keyboard reaches over the layout viewport, 0 with none up. */
function keyboardOverlap(): number {
  const viewport = window.visualViewport;
  if (!viewport || viewport.scale > PINCH_SCALE) return 0;
  // Not `innerHeight`: Safari drops it to the visible height partway through the
  // keyboard's arrival, and the overlap would read as nothing with the page panned.
  const overlap = document.documentElement.clientHeight - viewport.height;
  return overlap > KEYBOARD_MIN_PX ? overlap : 0;
}

/**
 * Whether text is coming from an on-screen keyboard. Its return key carries no
 * modifier, so a field that sends on Enter could never take a line break there.
 * A hardware keyboard covers nothing, on a tablet too, so Enter still sends.
 */
export function onScreenKeyboardUp(): boolean {
  return keyboardOverlap() > 0;
}

/** Kept clear above the keyboard: Safari's address pill floats right at the visible edge. */
const KEYBOARD_CLEARANCE_PX = 8;

/**
 * How far `el` can grow downward before it reaches the clearance above the
 * keyboard, negative once past it, and null while the shell is not fitted.
 * Something in a scrolling route, like a field that grows downward, can run past.
 */
export function roomAboveKeyboard(el: Element): number | null {
  const viewport = window.visualViewport;
  if (!viewport || document.documentElement.dataset.keyboardOpen === undefined) return null;
  return viewport.height - KEYBOARD_CLEARANCE_PX - el.getBoundingClientRect().bottom;
}

const fitListeners = new Set<() => void>();

/**
 * Called once the shell has taken a new height for the keyboard or given it back,
 * and when focus moves to another field under the same keyboard.
 */
export function onKeyboardFit(listener: () => void): () => void {
  fitListeners.add(listener);
  return () => fitListeners.delete(listener);
}

// A composer's picker opens from the fitted composer and is measured from it,
// wherever it portals; any other dialog floats over the shell.
function inShell(el: Element | null): el is Element {
  if (el?.closest('[data-composer-panel]')) return true;
  return !!el?.closest('.app-layout') && !el.closest('[role="dialog"], [data-sheet]');
}

export function trackVisualViewport(): void {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const root = document.documentElement;
  let revealed: Element | null = null;
  // DALTON-HOOK(mobile-web): track revealed field inside sheets/modals/auth pages and baseline height for Android/WeChat resizes-content fallback
  let revealedOverlay: Element | null = null;
  let baselineHeight = viewport.height;
  let baselineWidth = viewport.width ?? window.innerWidth;
  let fittedTo: number | null = null;

  const notify = (height: number | null) => {
    fittedTo = height;
    for (const listener of fitListeners) listener();
  };

  const sync = () => {
    const overlap = keyboardOverlap();
    const active = document.activeElement;
    // DALTON-HOOK(mobile-web): publish visual-viewport height and vertical offsets (offsetTop + bottom inset) for sheets, modals and standalone auth pages, including Android/WeChat where layout clientHeight shrinks with the keyboard (overlap === 0) while dvh may not
    const unpinched = viewport.scale <= PINCH_SCALE && viewport.height > 0;
    if (unpinched) {
      const currentWidth = viewport.width ?? window.innerWidth;
      if (currentWidth !== baselineWidth || viewport.height > baselineHeight) {
        baselineWidth = currentWidth;
        baselineHeight = viewport.height;
      }
      const offsetTop = Math.max(0, Math.round(viewport.offsetTop ?? 0));
      const bottomOffset = Math.max(0, Math.round(root.clientHeight - (offsetTop + viewport.height)));
      root.style.setProperty('--visual-viewport-height', `${Math.round(viewport.height)}px`);
      if (offsetTop > 0 || bottomOffset > 0) {
        root.style.setProperty('--visual-viewport-offset-top', `${offsetTop}px`);
        root.style.setProperty('--visual-viewport-bottom-offset', `${bottomOffset}px`);
      } else {
        root.style.removeProperty('--visual-viewport-offset-top');
        root.style.removeProperty('--visual-viewport-bottom-offset');
      }
    } else {
      root.style.removeProperty('--visual-viewport-height');
      root.style.removeProperty('--visual-viewport-offset-top');
      root.style.removeProperty('--visual-viewport-bottom-offset');
    }
    if (overlap) {
      root.style.setProperty('--keyboard-overlap', `${overlap}px`);
    } else {
      root.style.removeProperty('--keyboard-overlap');
    }
    const overlayKeyboardUp = overlap > 0 || (unpinched && (baselineHeight - viewport.height) > KEYBOARD_MIN_PX);
    if (!overlayKeyboardUp) {
      revealedOverlay = null;
    }
    if (!overlap || !inShell(active)) {
      delete root.dataset.keyboardOpen;
      root.style.removeProperty('--keyboard-viewport-height');
      revealed = null;
      if (
        overlayKeyboardUp
        && active instanceof Element
        && active !== revealedOverlay
        && active.closest('.login-page, [data-sheet], .cwm-modal')
      ) {
        active.scrollIntoView({ block: 'nearest' });
        revealedOverlay = active;
      }
      if (fittedTo !== null) notify(null);
      return;
    }
    revealedOverlay = null;
    root.dataset.keyboardOpen = '';
    root.style.setProperty('--keyboard-viewport-height', `${viewport.height}px`);

    // Safari revealed the field by panning, so a field low in a scrolling route
    // sits under the keyboard once the pan is gone. Once per field: a textarea
    // that grows under a finger must not fight the user's own scrolling.
    const newField = active !== revealed;
    if (newField) {
      active.scrollIntoView({ block: 'nearest' });
      revealed = active;
    }
    window.scrollTo(0, 0);
    if (newField || viewport.height !== fittedTo) notify(viewport.height);
  };

  // DALTON-HOOK(mobile-web): seed --visual-viewport-height on init so fallback holds before the first resize
  sync();
  viewport.addEventListener('resize', sync);
  viewport.addEventListener('scroll', sync);
  // Focus can cross between the shell and an overlay with the keyboard staying up.
  window.addEventListener('focusin', sync);
}
