import React, { useCallback, useEffect, useEffectEvent, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isImeComposing } from '@/lib/ime';
import { cn } from '@/lib/utils';
import type { Modifiers, MouseButton } from '../filePanel/browserProtocol';
<<<<<<< HEAD
// DALTON-HOOK(mobile-web): Keyboard icon for on-demand touch software keyboard toggle
import { Globe, Hand, Keyboard } from 'lucide-react';
import { isBlankPage, type BrowserPhase } from '../filePanel/browserPhase';
import { PHASE_CARDS } from './browserPhaseCards';
import type { BrowserViewSession } from '../filePanel/browserStream';
import { useBrowserViewState } from '../filePanel/useBrowserView';
// DALTON-HOOK(mobile-web): canvasScrollPoint maps letterbox swipes onto remote page scroll coordinates
import { canvasScrollPoint, keyInput, modifiersOf, mouseButton, pagePoint } from './browserInput';
=======
import { Hand } from 'lucide-react';
import { isTransient, startStateOf, type BrowserPhase, type BrowserStartState } from '../filePanel/browserPhase';
import { cardOf } from './browserPhaseCards';
import type { BrowserViewSession } from '../filePanel/browserStream';
import { useBrowserViewState } from '../filePanel/useBrowserView';
import { isHandBackKey, keyInput, modifiersOf, mouseButton, pagePoint } from './browserInput';
import { BrowserStartPage } from './BrowserStartPage';
>>>>>>> db284da5a
import { StatusCard } from './StatusCard';
import './BrowserViewer.css';

const NUDGE_MS = 4000;
// DALTON-HOOK(mobile-web): touch gesture constants and coarse-pointer detection
const TOUCH_SCROLL_THRESHOLD_PX = 8;
const TOUCH_MOUSE_GUARD_MS = 600;
const NO_MODIFIERS: Modifiers = { alt: false, ctrl: false, meta: false, shift: false };
const isCoarsePointer = () => typeof window !== 'undefined' && window.matchMedia('(pointer: coarse)').matches;

interface SingleTouchState {
  id: number;
  startX: number;
  startY: number;
  lastX: number;
  lastY: number;
  scrolling: boolean;
}

function findTouch(list: TouchList, id: number): Touch | null {
  for (let i = 0; i < list.length; i += 1) {
    if (list[i].identifier === id) return list[i];
  }
  return null;
}

/** A pointer sweep or a flick of the wheel is a stream of events; the page needs at
 *  most one a frame. Returns the scheduler and a flush that runs what is pending now. */
function useFrameCoalesced(run: () => void): [schedule: () => void, flush: () => void] {
  const pending = useRef<number | null>(null);
  const latest = useRef(run);
  useEffect(() => { latest.current = run; });
  useEffect(() => () => { if (pending.current !== null) cancelAnimationFrame(pending.current); }, []);
  const schedule = useCallback(() => {
    if (pending.current !== null) return;
    pending.current = requestAnimationFrame(() => {
      pending.current = null;
      latest.current();
    });
  }, []);
  const flush = useCallback(() => {
    if (pending.current === null) return;
    cancelAnimationFrame(pending.current);
    pending.current = null;
    latest.current();
  }, []);
  return [schedule, flush];
}

/** The browser's live frames, with the reader's mouse and keys going to the page. */
export function BrowserViewer({
  view,
  computerName,
}: {
  view: BrowserViewSession;
  /** The computer the browser runs on, for the start page; null where not shown. */
  computerName: string | null;
}): React.ReactElement {
  const { t } = useTranslation();
  const { phase, control, pageUrl, agent, visiting } = useBrowserViewState(view);
  const holding = control === 'held';
  // A click on a page that is not theirs yet says how to make it so, for a moment.
  const [nudgedAt, setNudgedAt] = useState<number | null>(null);
  useEffect(() => {
    if (nudgedAt === null) return;
    const timer = setTimeout(() => setNudgedAt(null), NUDGE_MS);
    return () => clearTimeout(timer);
  }, [nudgedAt]);
  const nudged = nudgedAt !== null && !holding;

  const canvas = useRef<HTMLCanvasElement | null>(null);
  const attachCanvas = (element: HTMLCanvasElement | null) => {
    canvas.current = element;
    view.attachCanvas(element);
  };

  // The frame as of the event, which may be newer than this render's.
  const point = (clientX: number, clientY: number) => {
    const { frame } = view.getSnapshot();
    const target = canvas.current;
    if (!frame || !target) return null;
    return pagePoint(clientX, clientY, target.getBoundingClientRect(), frame);
  };
  // DALTON-HOOK(mobile-web): allow single-finger scroll from anywhere on the canvas, including letterbox space
  const scrollPoint = (clientX: number, clientY: number) => {
    const { frame } = view.getSnapshot();
    const target = canvas.current;
    if (!frame || !target) return null;
    return canvasScrollPoint(clientX, clientY, target.getBoundingClientRect(), frame);
  };

  const held = useRef<MouseButton | undefined>(undefined);
  // Where the pointer last was on the page, for a release that lands off the
  // frame: the page lets go where the drag left it, not where it began.
  const last = useRef<{ x: number; y: number } | null>(null);
  const moved = useRef<{ x: number; y: number; modifiers: Modifiers } | null>(null);
  const [scheduleMove, flushMove] = useFrameCoalesced(() => {
    if (!moved.current) return;
    view.input({ kind: 'mouse', event: 'move', button: held.current, ...moved.current });
    moved.current = null;
  });

  const wheel = useRef<{ x: number; y: number; deltaX: number; deltaY: number; modifiers: Modifiers } | null>(null);
  const [scheduleWheel] = useFrameCoalesced(() => {
    if (!wheel.current) return;
    view.input({ kind: 'mouse', event: 'wheel', ...wheel.current });
    wheel.current = null;
  });

  // Attached by hand: React's wheel listener is passive, and scrolling the page
  // inside the frame must not scroll the panel around it.
  useEffect(() => {
    const target = canvas.current;
    if (!target) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (!holding) return;
      const at = point(e.clientX, e.clientY);
      if (!at) return;
      const line = e.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16 : 1;
      wheel.current = {
        ...at,
        deltaX: (wheel.current?.deltaX ?? 0) + e.deltaX * line,
        deltaY: (wheel.current?.deltaY ?? 0) + e.deltaY * line,
        modifiers: modifiersOf(e),
      };
      scheduleWheel();
    };
    target.addEventListener('wheel', onWheel, { passive: false });
    return () => target.removeEventListener('wheel', onWheel);
  });

  const keys = useRef<HTMLTextAreaElement>(null);
  // DALTON-HOOK(mobile-web): subscribe to primary coarse-pointer media query so touch listeners only register on touch-primary devices
  const [coarse, setCoarse] = useState(isCoarsePointer);
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mql = window.matchMedia('(pointer: coarse)');
    setCoarse(mql.matches);
    const onChange = (event: MediaQueryListEvent) => setCoarse(event.matches);
    mql.addEventListener?.('change', onChange);
    return () => mql.removeEventListener?.('change', onChange);
  }, []);
  // DALTON-HOOK(mobile-web): track software keyboard focus for on-demand toggle button on touch devices
  const [keyboardOpen, setKeyboardOpen] = useState(false);
  const keyboardActive = holding && keyboardOpen;
  // Keys go to the page from the moment it is theirs, with no click first.
  // DALTON-HOOK(mobile-web): skip auto-focus on coarse-pointer devices so taking control does not force open the soft keyboard
  useEffect(() => {
    if (holding && !coarse) keys.current?.focus({ preventScroll: true });
  }, [holding, coarse]);

  // DALTON-HOOK(mobile-web): map single-finger touch tap to remote click (without forcing soft keyboard) and single-finger drag to remote mouseWheel scroll, only under pointer: coarse
  const touchState = useRef<SingleTouchState | null>(null);
  const lastTouchAt = useRef<number>(-Infinity);
  useEffect(() => {
    if (!holding) touchState.current = null;
  }, [holding]);
  const isSyntheticTouchMouse = (nativeEvent: MouseEvent): boolean => {
    // Only discard touch-synthesized mouse events when our own coarse-pointer touch listeners are active;
    // on fine-pointer touchscreen laptops, let the browser's compatibility mouse events reach the remote page.
    if (!coarse) return false;
    const caps = (nativeEvent as MouseEvent & { sourceCapabilities?: { firesTouchEvents?: boolean } }).sourceCapabilities;
    if (caps?.firesTouchEvents) return true;
    return touchState.current !== null || (Date.now() - lastTouchAt.current) < TOUCH_MOUSE_GUARD_MS;
  };
  const touchScrollScale = () => {
    const { frame } = view.getSnapshot();
    const target = canvas.current;
    if (!frame || !target) return 1;
    const box = target.getBoundingClientRect();
    const scale = Math.min(box.width / frame.width, box.height / frame.height);
    const shownWidth = frame.width * scale;
    if (shownWidth <= 0) return 1;
    return Math.min(4, Math.max(1, frame.pageWidth / shownWidth));
  };
  const onTouchStart = useEffectEvent((e: TouchEvent) => {
    lastTouchAt.current = Date.now();
    if (e.touches.length !== 1) {
      touchState.current = null;
      return;
    }
    if (!holding) {
      if (control === 'none' && phase === 'live') {
        e.preventDefault();
        setNudgedAt(e.timeStamp);
      }
      return;
    }
    // Non-passive preventDefault suppresses compatibility mousedown/mouseup events synthesized after touchend.
    e.preventDefault();
    const t = e.touches[0];
    touchState.current = {
      id: t.identifier,
      startX: t.clientX,
      startY: t.clientY,
      lastX: t.clientX,
      lastY: t.clientY,
      scrolling: false,
    };
  });
  const onTouchMove = useEffectEvent((e: TouchEvent) => {
    lastTouchAt.current = Date.now();
    const state = touchState.current;
    if (!holding || !state) return;
    if (e.touches.length !== 1) {
      touchState.current = null;
      return;
    }
    const current = findTouch(e.touches, state.id);
    if (!current) return;
    e.preventDefault();
    const dx = current.clientX - state.lastX;
    const dy = current.clientY - state.lastY;
    if (!state.scrolling && Math.hypot(current.clientX - state.startX, current.clientY - state.startY) >= TOUCH_SCROLL_THRESHOLD_PX) {
      state.scrolling = true;
    }
    if (state.scrolling) {
      const at = scrollPoint(current.clientX, current.clientY) ?? scrollPoint(state.startX, state.startY);
      if (at) {
        const scale = touchScrollScale();
        wheel.current = {
          ...at,
          deltaX: (wheel.current?.deltaX ?? 0) - dx * scale,
          deltaY: (wheel.current?.deltaY ?? 0) - dy * scale,
          modifiers: NO_MODIFIERS,
        };
        scheduleWheel();
      }
      state.lastX = current.clientX;
      state.lastY = current.clientY;
    }
  });
  const onTouchEnd = useEffectEvent((e: TouchEvent) => {
    lastTouchAt.current = Date.now();
    const state = touchState.current;
    if (!state) return;
    const ended = findTouch(e.changedTouches, state.id);
    if (!ended) return;
    touchState.current = null;
    if (!holding) return;
    e.preventDefault();
    // Re-check total displacement at lift-off so short flicks whose last touchmove stayed under threshold never click.
    const totalMove = Math.hypot(ended.clientX - state.startX, ended.clientY - state.startY);
    if (state.scrolling || totalMove >= TOUCH_SCROLL_THRESHOLD_PX) return;
    const at = point(ended.clientX, ended.clientY);
    if (!at) return;
    flushMove();
    view.input({ kind: 'mouse', event: 'down', button: 'left', clickCount: 1, modifiers: NO_MODIFIERS, ...at });
    view.input({ kind: 'mouse', event: 'up', button: 'left', clickCount: 1, modifiers: NO_MODIFIERS, ...at });
  });
  const onTouchCancel = useEffectEvent(() => {
    lastTouchAt.current = Date.now();
    touchState.current = null;
  });
  useEffect(() => {
    if (!coarse) return;
    const target = canvas.current;
    if (!target) return;
    const handleStart = (e: TouchEvent) => onTouchStart(e);
    const handleMove = (e: TouchEvent) => onTouchMove(e);
    const handleEnd = (e: TouchEvent) => onTouchEnd(e);
    const handleCancel = () => onTouchCancel();
    target.addEventListener('touchstart', handleStart, { passive: false });
    target.addEventListener('touchmove', handleMove, { passive: false });
    target.addEventListener('touchend', handleEnd, { passive: false });
    target.addEventListener('touchcancel', handleCancel);
    return () => {
      target.removeEventListener('touchstart', handleStart);
      target.removeEventListener('touchmove', handleMove);
      target.removeEventListener('touchend', handleEnd);
      target.removeEventListener('touchcancel', handleCancel);
    };
  }, [coarse]);

  // Each button's release, listened for on the window while the hold it was
  // pressed under lasts. Once the hold ends the session has let the buttons go
  // itself, and a release arriving under a later hold would be another press's.
  const releases = useRef(new Map<MouseButton, (up: MouseEvent) => void>());
  useEffect(() => {
    if (!holding) return;
    const pressed = releases.current;
    return () => {
      for (const onUp of pressed.values()) window.removeEventListener('mouseup', onUp);
      pressed.clear();
      held.current = undefined;
    };
  }, [holding]);

  const onMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    // The default would move focus off the field the keys arrive in.
    e.preventDefault();
    // DALTON-HOOK(mobile-web): ignore browser-synthesized mouse events emitted after a touch sequence
    if (isSyntheticTouchMouse(e.nativeEvent)) return;
    if (!holding) {
      if (control === 'none' && phase === 'live') setNudgedAt(e.timeStamp);
      return;
    }
    // DALTON-HOOK(mobile-web): on touch devices, open the software keyboard only via the explicit keyboard button
    if (!coarse) keys.current?.focus({ preventScroll: true });
    const at = point(e.clientX, e.clientY);
    if (!at) return;
    const button = mouseButton(e.button);
    // A move still waiting for its frame went before this press, without the button.
    flushMove();
    held.current = button;
    last.current = at;
    view.input({ kind: 'mouse', event: 'down', button, clickCount: e.detail || 1, modifiers: modifiersOf(e), ...at });
    // On the window: a drag that ends off the frame must still let go. Each
    // button lets go of its own press, whatever else is down.
    const pressed = releases.current;
    const onUp = (up: MouseEvent) => {
      if (mouseButton(up.button) !== button) return;
      window.removeEventListener('mouseup', onUp);
      pressed.delete(button);
      // The drag's last move lands where the drag ended, under the button.
      flushMove();
      held.current = pressed.keys().next().value;
      const end = point(up.clientX, up.clientY) ?? last.current ?? at;
      view.input({ kind: 'mouse', event: 'up', button, clickCount: up.detail || 1, modifiers: modifiersOf(up), ...end });
    };
    const earlier = pressed.get(button);
    if (earlier) window.removeEventListener('mouseup', earlier);
    pressed.set(button, onUp);
    window.addEventListener('mouseup', onUp);
  };

  const onMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    // DALTON-HOOK(mobile-web): ignore touch-synthesized mousemove events
    if (!holding || isSyntheticTouchMouse(e.nativeEvent)) return;
    const at = point(e.clientX, e.clientY);
    if (!at) return;
    last.current = at;
    moved.current = { ...at, modifiers: modifiersOf(e) };
    scheduleMove();
  };

  const sendText = (text: string) => {
    if (text) view.input({ kind: 'text', text });
  };

  // Keys arrive in a hidden field rather than on the canvas, because text an input
  // method composes comes with no key of its own and only reaches something editable.
  // Everything goes to the page, which is what a reader expects of a focused frame,
  // except paste: it arrives as an event of its own only if its key is let through.
  const isPasteKey = (e: React.KeyboardEvent) => (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'v';
  const stage = useRef<HTMLDivElement>(null);
  // The page takes the stage's size, so it fills the panel and is drawn one to
  // one rather than shrunk to fit.
  useEffect(() => {
    const element = stage.current;
    if (!element) return;
    const measure = () => {
      const { width, height } = element.getBoundingClientRect();
      view.setViewSize({ width, height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      observer.disconnect();
      view.setViewSize(null);
    };
  }, [view]);
  const onKey = (eventType: 'down' | 'up') => (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    e.stopPropagation();
    if (isImeComposing(e)) return;
    if (isHandBackKey(e)) {
      e.preventDefault();
      if (eventType === 'up') return;
      // The Shift the page saw go down is let go with the hold.
      view.releaseControl();
      // Focus stays on the viewer for the next Tab to move on.
      stage.current?.focus({ preventScroll: true });
      return;
    }
    if (isPasteKey(e)) return;
    e.preventDefault();
    view.input(keyInput(eventType, e.nativeEvent));
  };
  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    e.preventDefault();
    sendText(e.clipboardData.getData('text/plain'));
  };
  // A key's text went with the key, so what lands in the field is only what came
  // without one. Browsers disagree on whether input follows compositionend, so the
  // field is emptied there and a late input finds nothing to send twice.
  const onInput = (e: React.FormEvent<HTMLTextAreaElement>) => {
    if ((e.nativeEvent as InputEvent).isComposing) return;
    sendText(e.currentTarget.value);
    e.currentTarget.value = '';
  };
  const onCompositionEnd = (e: React.CompositionEvent<HTMLTextAreaElement>) => {
    sendText(e.data);
    e.currentTarget.value = '';
  };

  // No page to show: a blank tab, no browser open, or no computer running,
  // which going somewhere opens. The start page stays while a visit is on its
  // way through the phases that open the browser, saying where it set out from.
  const startNow = startStateOf(phase, pageUrl);
  const [setOutFrom, setSetOutFrom] = useState<BrowserStartState | null>(startNow);
  if (startNow !== null && startNow !== setOutFrom) setSetOutFrom(startNow);
  const start = startNow ?? (visiting !== null && isTransient(phase) ? setOutFrom : null);
  const showing = (phase === 'live' || phase === 'reconnecting') && start === null;
  return (
    <div className="browser-viewer">
      {holding && (
        <div className="browser-viewer-strip" role="status">
          <Hand className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 truncate">
            {t('filePanel:browser.heldNote', 'You have the browser. The agent waits until you hand it back.')}
          </span>
          <span className="browser-viewer-strip-keys">{t('filePanel:browser.heldKeys', 'Shift+Esc to hand back')}</span>
          {/* DALTON-HOOK(mobile-web): explicit on-demand software keyboard toggle on touch devices */}
          <button
            type="button"
            className="browser-viewer-kbd-btn tap-target"
            aria-pressed={keyboardActive}
            aria-label={keyboardActive ? t('filePanel:browser.hideKeyboard') : t('filePanel:browser.showKeyboard')}
            title={keyboardActive ? t('filePanel:browser.hideKeyboard') : t('filePanel:browser.showKeyboard')}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              if (keyboardActive) keys.current?.blur();
              else keys.current?.focus({ preventScroll: true });
            }}
          >
            <Keyboard className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{t('filePanel:browser.keyboard')}</span>
          </button>
        </div>
      )}
      <div ref={stage} className="browser-viewer-stage" tabIndex={-1}>
      <canvas
        ref={attachCanvas}
        className={cn('browser-viewer-canvas', phase === 'reconnecting' && 'is-stale', !holding && 'is-watching')}
        title={showing && !holding ? t('filePanel:browser.watchingHint') : undefined}
        style={showing ? undefined : { visibility: 'hidden' }}
        role="img"
        aria-label={t('filePanel:browser.label')}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onContextMenu={(e) => e.preventDefault()}
      />
      {holding && (
        <textarea
          ref={keys}
          className="browser-viewer-keys"
          aria-label={t('filePanel:browser.label')}
          autoCapitalize="off"
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          onKeyDown={onKey('down')}
          onKeyUp={onKey('up')}
          // DALTON-HOOK(mobile-web): mirror hidden field focus state on the touch keyboard toggle button
          onFocus={() => setKeyboardOpen(true)}
          // A key let go after focus left goes to wherever it went.
          onBlur={() => { setKeyboardOpen(false); view.releaseKeys(); }}
          onPaste={onPaste}
          onInput={onInput}
          onCompositionEnd={onCompositionEnd}
        />
      )}
      {start ? (
        <BrowserStartPage view={view} state={start} computerName={computerName} />
      ) : (
        <BrowserNotice view={view} phase={phase} />
      )}
      {(nudged || (agent && phase === 'live')) && (
        <div className="browser-viewer-foot">
          {nudged && (
            <span className="browser-viewer-pill">
              {t('filePanel:browser.watchingHint')}
              <button type="button" className="browser-viewer-pill-action tap-target" onClick={() => view.takeControl()}>
                {t('filePanel:browser.takeControl')}
              </button>
            </span>
          )}
          {agent && phase === 'live' && (
            <span className="browser-viewer-pill" role="status">
              {/* Running, as a thread's dot says it. */}
              <span className="browser-viewer-dot animate-pulse motion-reduce:animate-none" aria-hidden="true" />
              <span className="browser-viewer-who">{t('filePanel:browser.agent', 'Agent')}</span>
              <span className="min-w-0 truncate">
                {t(`filePanel:browser.doing.${agent.key}`, { detail: agent.detail ?? '' })}
              </span>
            </span>
          )}
        </div>
      )}
      </div>
    </div>
  );
}

function BrowserNotice({ view, phase }: { view: BrowserViewSession; phase: BrowserPhase }): React.ReactElement | null {
  const { t } = useTranslation();
  const card = cardOf(phase);
  if (!card) return null;
  const { action } = card;
  return (
    <StatusCard
      icon={card.icon}
      title={t(card.titleKey)}
      body={card.bodyKey && t(card.bodyKey)}
      action={action && { label: t(action.labelKey), run: { open: () => view.open(), retry: () => view.retry(), chrome: () => view.getChrome() }[action.run] }}
    />
  );
}
