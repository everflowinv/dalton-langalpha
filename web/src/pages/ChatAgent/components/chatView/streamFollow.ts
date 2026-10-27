import { useCallback, useEffect, useLayoutEffect, useRef, type RefObject } from 'react';
import { prefersReducedMotion } from '@/lib/reducedMotion';
import { AT_BOTTOM_PX, NEAR_BOTTOM_PX, createSettleWindow, isNearBottom } from '../../utils/scrollHelpers';
import { anchorTop, findMessageElement, type AnchorPart } from '../../utils/scrollDom';

/**
 * Following a streaming transcript. A reader near the bottom is moved to the
 * new bottom in the frame the transcript grows, instantly: a timer of smooth
 * scrolls lagged a fast stream, absorbed the wheel input of a reader trying to
 * leave, and when a block landed mid-animation it stopped short, read as
 * "left", and never followed again. Direction decides intent: an upward scroll
 * leaves unless it ends at the very bottom, so a notch at a time gets away, and
 * a downward one rejoins inside the band. A fold that clamps scrollTop moves up
 * too, but lands on the bottom, so it keeps following.
 */
export interface StreamFollow {
  /** Reads a scroll event. False for a follow's own, which decides nothing.
   *  `judge` false leaves the flag alone, for a landing something else chose. */
  scrolled(judge?: boolean): boolean;
  /** Moves a following reader to the new bottom, for growth. */
  follow(): void;
}

export function createStreamFollow(c: HTMLElement, following: { current: boolean }): StreamFollow {
  let lastTop = c.scrollTop;
  // The scroll event of a follow arrives a frame after it. A block that lands
  // in between, a chart or a table, leaves the bottom further away than the
  // band, so that event is the follow's own and changes nothing. A reader who
  // scrolls in between is measured from where the follow put them, or a notch
  // up from there would read as a move down and be pulled back.
  let followTop: number | null = null;
  return {
    scrolled(judge = true) {
      const from = followTop ?? lastTop;
      const own = followTop !== null && Math.abs(c.scrollTop - followTop) < 1;
      followTop = null;
      lastTop = c.scrollTop;
      if (own) return false;
      if (!judge) return true;
      const metrics = { scrollTop: c.scrollTop, scrollHeight: c.scrollHeight, clientHeight: c.clientHeight };
      following.current = isNearBottom(metrics, c.scrollTop < from ? AT_BOTTOM_PX : NEAR_BOTTOM_PX);
      return true;
    },
    follow() {
      if (!following.current) return;
      const bottom = c.scrollHeight - c.clientHeight;
      if (bottom <= c.scrollTop) return;
      followTop = bottom;
      c.scrollTo({ top: bottom });
    },
  };
}

/** What the follow policy (useTranscriptFollow) asks of a scroll engine. */
export interface StreamFollowControls {
  /** Follows again from the bottom wherever the reader was, for a turn the
   *  reader starts. */
  rejoin(): void;
  /** Brings the start of reply bubble `id` under the viewport top, for a turn
   *  that finished under the 'reply_start' preference. */
  landOnReply(id: string, behavior: 'auto' | 'smooth'): void;
}

// What a reader does to scroll: a wheel or trackpad, a touch, the scrollbar or
// a click, a key. A landing's own scrolls cannot be told from the reader's by
// position (a smooth one passes through any), so one of these ends the hold.
export const INTENT_EVENTS = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;

/**
 * The follow for a scroller that keeps no position to restore, so the first
 * observation follows too and a thread opens at its end. `shown` re-attaches
 * it when the content mounts. Later growth is followed only while `active`:
 * in a settled transcript it is the reader's own doing, a row opened, and
 * stays where they put it. `landOnReply` moves only a reader the follow was
 * carrying and stops following, and a reply that fits on screen moves nothing.
 * `reveal` is a link's jump to a message (`onRevealMessage`).
 */
export function useStreamFollow(
  scroller: RefObject<HTMLElement | null>,
  content: RefObject<HTMLElement | null>,
  shown: boolean,
  active: boolean,
): StreamFollowControls & { reveal(id: string): boolean } {
  const activeRef = useRef(active);
  const attachedRef = useRef<(StreamFollowControls & { settled(): void; reveal(id: string): boolean }) | null>(null);
  // The commit that ends a turn or a history load carries its last growth,
  // and the observer reports it only after this has marked the transcript
  // settled. Followed here instead, where layout already measures it, and
  // for one settle window after, while the typewriter types out what it still
  // held and late media lands.
  useLayoutEffect(() => {
    if (activeRef.current && !active) attachedRef.current?.settled();
    activeRef.current = active;
  }, [active]);
  useEffect(() => {
    const c = scroller.current;
    const el = content.current;
    if (!c || !el) return;
    const following = { current: true };
    const stream = createStreamFollow(c, following);
    // A landed reply, or a message a link jumped to, is held under the
    // viewport top through the main chat's settle window, re-measured on every
    // resize: the settled turn folds its work away above the reply right after
    // it lands.
    let held: { id: string; part?: AnchorPart; top: number } | null = null;
    const settle = createSettleWindow(() => {
      held = null;
    });
    // After a turn ends, its growth is still followed until the transcript
    // has been quiet for the settle window or the reader does anything.
    let tail = false;
    const tailWindow = createSettleWindow(() => {
      tail = false;
    });
    const release = () => {
      held = null;
      settle.clear();
      tail = false;
      tailWindow.clear();
    };
    let lastHeight = -1;
    const ro = new ResizeObserver((entries) => {
      const height = entries[0]?.contentRect.height ?? lastHeight;
      const follow = lastHeight < 0 || (height > lastHeight && (activeRef.current || tail));
      lastHeight = height;
      if (held) {
        const top = anchorTop(c, held.id, held.part);
        if (top == null) {
          release();
          return;
        }
        // Growth below the reply moves nothing, and leaves a smooth landing
        // still under way alone.
        if (Math.abs(top - held.top) >= 1) {
          held.top = top;
          c.scrollTo({ top });
        }
        settle.arm();
      } else if (follow) {
        stream.follow();
        if (tail) tailWindow.arm();
      }
    });
    const onScroll = () => { stream.scrolled(!held); };
    c.addEventListener('scroll', onScroll, { passive: true });
    for (const type of INTENT_EVENTS) c.addEventListener(type, release, { passive: true });
    ro.observe(el);
    attachedRef.current = {
      settled() {
        tail = true;
        tailWindow.arm();
        stream.follow();
      },
      rejoin() {
        release();
        following.current = true;
        stream.follow();
      },
      landOnReply(id, behavior) {
        if (!following.current) return;
        const top = anchorTop(c, id, 'reply');
        if (top == null || top >= c.scrollHeight - c.clientHeight - 1) return;
        following.current = false;
        held = { id, part: 'reply', top };
        c.scrollTo({ top, behavior });
        settle.arm();
      },
      reveal(id) {
        const el = findMessageElement(c, id);
        const top = el && anchorTop(c, id);
        if (!el || top == null) return false;
        // Under the viewport top and held there, as the main chat's jump
        // lands; a message too near the end for that is where the follow
        // already reads.
        const bottom = c.scrollHeight - c.clientHeight;
        const atEnd = top >= bottom - 1;
        following.current = atEnd;
        held = atEnd ? null : { id, top };
        c.scrollTo({ top: Math.min(top, bottom), behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
        if (!atEnd) settle.arm();
        el.focus({ preventScroll: true });
        return true;
      },
    };
    return () => {
      release();
      attachedRef.current = null;
      c.removeEventListener('scroll', onScroll);
      for (const type of INTENT_EVENTS) c.removeEventListener(type, release);
      ro.disconnect();
    };
  }, [scroller, content, shown]);
  const rejoin = useCallback(() => attachedRef.current?.rejoin(), []);
  const landOnReply = useCallback(
    (id: string, behavior: 'auto' | 'smooth') => attachedRef.current?.landOnReply(id, behavior),
    [],
  );
  const reveal = useCallback((id: string) => attachedRef.current?.reveal(id) ?? false, []);
  return { rejoin, landOnReply, reveal };
}
