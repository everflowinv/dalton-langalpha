import React, { useEffect, useEffectEvent, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Loader } from '@/components/ui/loader';
import { DURATION } from '@/lib/motion';
import { useTranslation } from 'react-i18next';
import { AppBridge, type BridgeOptions } from '@/lib/appBridge/AppBridge';
// DALTON-HOOK(mobile-web): normalize workspace-relative paths when classifying current vs linked docs
import { artifactPathKey } from '@/hooks/useArtifact';
import { HostedFrame } from '../HostedFrame';
import { framedDirectly, messageOrigin, sendFrameTo } from '../frameHost';
import { frameSwap, isReady, isShown, NO_SWAP, type FramedDoc, type FrameSwap, type FrameSwapAction } from './frameSwap';
// DALTON-HOOK(mobile-web): export unified HtmlFrameDocState for viewer/header state machine
import { SETTLE_CAP_MS, type HtmlFrameDocState, type PageToViewer } from './pageProtocol';
import { SERVED_HTML_PERMISSIONS } from './sandbox';
import { usePageBridge, type PageBridge } from './usePageBridge';
import { withServeView } from './wsfilesUrl';
import './BridgedFrame.css';

export type { HtmlFrameDocState } from './pageProtocol';

type RawFrameDoc =
  | { kind: 'pending' }
  | { kind: 'path'; path: string }
  | { kind: 'root' }
  | { kind: 'unbridged' };

type ReadyCandidate = {
  docId: string;
  raw: RawFrameDoc;
  claimsRoot: boolean;
};

// DALTON-HOOK(mobile-web): left-anchored pathless version root entries (mirrors core.js DOC_ROOT);
// only a host-initiated navigation whose framed URL matches this pattern may accept `root: true`.
const VERSION_ROOT_PATH =
  /^\/(?:(?:api\/v1\/(?:artifacts\/g\/[^/]+|dalton\/monitors\/(?:g|public)\/[^/]+)|g\/[^/]+)\/)?v\/[0-9]+\/?$/;

function isVersionRootUrl(src: string | null | undefined, expectedDocKey: string | null): boolean {
  if (!src || expectedDocKey) return false;
  try {
    const url = new URL(src, window.location.href);
    return VERSION_ROOT_PATH.test(url.pathname);
  } catch {
    return false;
  }
}

function matchesHostVersionRoot(
  frame: FramedDoc | null,
  expectedDocKey: string | null,
  el: HTMLIFrameElement | null,
): boolean {
  if (!frame || expectedDocKey) return false;
  // DALTON-HOOK(mobile-web): never recognize `root: true` in an opaque-origin sandbox;
  // require the host to read the frame's actual same-origin location and confirm it matches the version root.
  if (frame.origin === 'null' || !framedDirectly(frame.src) || !el) return false;
  let expectedUrl: URL;
  try {
    expectedUrl = new URL(frame.src, window.location.href);
  } catch {
    return false;
  }
  if (!VERSION_ROOT_PATH.test(expectedUrl.pathname)) return false;

  const norm = (p: string) => p.replace(/\/+$/, '');
  try {
    const href = el.contentWindow?.location?.href;
    if (!href || href === 'about:blank') return false;
    const actualUrl = new URL(href);
    return actualUrl.origin === expectedUrl.origin && norm(actualUrl.pathname) === norm(expectedUrl.pathname);
  } catch {
    return false;
  }
}

/**
 * How long a loaded document may keep building itself before it replaces the
 * one on screen anyway: a document that never goes still (a chart that
 * animates) would otherwise never be shown. Past the bridge's own cap on
 * waiting, so one that settles at all settles first; the one on screen stays
 * readable meanwhile.
 */
const REVEAL_BACKSTOP_MS = SETTLE_CAP_MS + 500;
/**
 * How long a loaded document has to say hello before it is taken for one
 * without the bridge. A document's bridge says it as its first script runs,
 * before the load; this covers the relay through a frame host.
 */
const UNBRIDGED_GRACE_MS = 2000;
/**
 * How long a ready document waits before it fades in. A frame drawn at all
 * but no opacity is a few frames behind on its own picture: faded in at
 * once, it first showed the document from before it was scrolled back, for a
 * frame. Measured gone at 40ms; this leaves room.
 */
const REVEAL_HOLD_MS = 80;
/** The document being replaced is dropped once the next has faded in over it. */
const PREVIOUS_HOLD_MS = (DURATION.quick + DURATION.exit) * 1000;
/** The loading cover stays mounted while it fades out. */
const COVER_HOLD_MS = DURATION.exit * 1000;

/**
 * The ground each file last reported, so the next frame of it opens on the
 * file's colour rather than the app's: a dark report under the light theme
 * otherwise flashes white behind its loading cover each time it is opened.
 */
const knownGrounds = new Map<string, string | null>();

/** What to frame, and how. */
export interface FrameSource {
  /**
   * Which document this is: the live file at one content, or one version. A
   * new key loads in a second frame over the one on screen; a new `src` under
   * the same key (a renewed grant) reloads nothing.
   */
  key: string;
  src: string;
  /** Which origin the document posts from follows from it: opaque without `allow-same-origin`. */
  sandbox: string;
}

/** The document the frame holds. */
export interface FrameView {
  /** The `FrameSource.key` it was loaded for, which can trail the source while the next one loads. */
  key: string;
  /**
   * The document is the one on screen: loaded, and past the cover or the
   * document it replaces. Until then what it reports is not what the reader
   * sees, so nothing is drawn or opened against it.
   */
  shown: boolean;
}

interface BridgedFrameProps {
  /** Null while what to frame is not known yet: the cover shows. */
  source: FrameSource | null;
  title: string;
  /** The file the ground is remembered for. */
  groundKey: string;
  /** Shown instead of the frame: a failure, or a version that does not exist. */
  notice?: React.ReactNode;
  /**
   * Drawn over the frame, in the app's DOM rather than the document's, with
   * the document the frame holds. The layer passes pointer events through;
   * what it draws takes them back where it needs them. Its box is the frame's,
   * so a rect the document reports needs no conversion.
   */
  overlay?: (bridge: PageBridge, view: FrameView) => React.ReactNode;
  /** Handed the frame's bridge whenever it changes, for whoever holds the frame. */
  // DALTON-HOOK(mobile-web): include frameKey on onBridge so callers reject stale frames across tab switches
  onBridge?: (bridge: PageBridge, frameKey: string | null) => void;
  /**
   * DALTON-HOOK(mobile-web): normalized workspace-relative key of the document
   * opened for this frame, used to distinguish `current` from `linked`.
   */
  expectedDocKey?: string | null;
  /** DALTON-HOOK(mobile-web): optional workspace rootDir for path normalization. */
  docRoot?: string | null;
  /**
   * DALTON-HOOK(mobile-web): reports the frame's document state
   * (`pending` / `current` / `bridged_root` / `linked` / `unbridged`) paired with `frameKey`.
   */
  onDocStateChange?: (state: HtmlFrameDocState, frameKey: string | null) => void;
  /**
   * A document the frame loaded, a link inside it followed included, that
   * never said hello: not a bridged document, so most likely an error page
   * (a version's files answer 404 once an Update or an unshare ended it).
   */
  onUnbridged?: () => void;
  /** The document on screen, or about to be, fired its `load`: what it fetches to draw is in. */
  onLoad?: () => void;
  /**
   * Answer the LangAlpha kit (`lib/appBridge`) for the owner, so a page built
   * on it shows their data here as on their Dashboard. `grantKey` is the
   * file's `/a/` code, which keys what the owner let it read. `active` is the
   * bridge's: false while the frame is mounted out of sight.
   */
  kit?: Pick<BridgeOptions, 'grantKey' | 'active'>;
}

/**
 * A document that speaks the page bridge, framed so another document of the
 * same file (the agent's edit reloading the live file, a version picked)
 * builds itself in a second frame over the one on screen, all but
 * transparent, until it has been scrolled to where the reader was; then it
 * fades in. A swap that blanked the frame showed the reader a bare sheet and
 * the top of the document each time.
 *
 * The frame's address is pinned when the key changes, not whenever the
 * source's `src` does: the document in the frame is already loaded, and a new
 * address would reload it under the reader.
 */
// DALTON-HOOK(mobile-web): accept expectedDocKey, docRoot, and onDocStateChange props
export function BridgedFrame({
  source, title, groundKey, notice, overlay, onBridge, expectedDocKey = null, docRoot = null, onDocStateChange, onUnbridged, onLoad, kit,
}: BridgedFrameProps): React.ReactElement {
  const { t } = useTranslation();
  const frameKey = source?.key ?? null;
  const [swapState, dispatch] = useReducer(frameSwap, NO_SWAP);
  // Read from here on as it is about to be, so this pass does not act on a
  // frame already replaced (it would mount the first load's cover).
  let swap: FrameSwap = swapState;
  if (source && swap.current?.doc.key !== source.key) {
    const action: FrameSwapAction = {
      type: 'frame',
      doc: {
        key: source.key,
        src: source.src,
        origin: messageOrigin(source.src, source.sandbox),
        sandbox: source.sandbox,
      },
    };
    swap = frameSwap(swap, action);
    dispatch(action);
  }
  const frame = frameKey && swap.current?.doc.key === frameKey ? swap.current.doc : null;
  const current = frame?.key ?? null;
  const behind = swap.behind;
  const loaded = !!frame && !!swap.current?.loaded;
  const ready = !!frame && isReady(swap);
  const shown = !!frame && isShown(swap);

  useEffect(() => {
    if (!ready || !behind || !current) return;
    const timer = setTimeout(() => dispatch({ type: 'held', key: current }), REVEAL_HOLD_MS);
    return () => clearTimeout(timer);
  }, [ready, behind, current]);
  useEffect(() => {
    if (!loaded || !behind || ready || !current) return;
    const timer = setTimeout(() => dispatch({ type: 'overdue', key: current }), REVEAL_BACKSTOP_MS);
    return () => clearTimeout(timer);
  }, [loaded, behind, ready, current]);
  const behindKey = behind?.key ?? null;
  useEffect(() => {
    if (!shown || !behindKey) return;
    const timer = setTimeout(() => dispatch({ type: 'drop', key: behindKey }), PREVIOUS_HOLD_MS);
    return () => clearTimeout(timer);
  }, [shown, behindKey]);

  const frameRef = useRef<HTMLIFrameElement>(null);
  const bridge = usePageBridge({ frameRef, origin: frame?.origin ?? null, frameKey: current });
  // DALTON-HOOK(mobile-web): emit frameKey alongside bridge to prevent cross-tab bleed
  const reportBridge = useEffectEvent((b: PageBridge, key: string | null) => onBridge?.(b, key));
  useEffect(() => { reportBridge(bridge, current); }, [bridge, current]);

  // Where the reader was, carried to the next document, and the ground the
  // document paints, drawn behind the frame so a swap or a load on a dark
  // document does not flash the app's own.
  const lastScroll = useRef<{ x: number; y: number } | null>(null);
  const carry = useRef<{ key: string; at: { x: number; y: number } | null; done: boolean } | null>(null);
  useEffect(() => {
    if (current) carry.current = { key: current, at: lastScroll.current, done: false };
  }, [current]);
  const [ground, setGround] = useState<string | null>(() => knownGrounds.get(groundKey) ?? null);
  const { subscribe, post } = bridge;
  // The same document under a renewed grant reloads nothing, so it is told.
  const address = source?.key === current ? source?.src : null;
  useEffect(() => {
    if (address) post({ type: 'page:address', src: address });
  }, [address, post]);
  // A load waiting for its hello, or a hello that came before its load. The
  // flags answer at once, since a hello can follow its load before React
  // renders again; `loads` restarts the wait for each load.
  // DALTON-HOOK(mobile-web): treat page:ready as a candidate only and publish confirmed doc state
  // once page:pong matches both the latest load's navSeq and a recorded candidate docId.
  const candidatesRef = useRef<{ key: string; docs: Map<string, ReadyCandidate> } | null>(null);
  const waiting = useRef(false);
  const navSeqRef = useRef(0);
  const currentKeyRef = useRef<string | null>(current);
  const syncedKeyRef = useRef<string | null | undefined>(undefined);
  const isHostVersionRoot = isVersionRootUrl(frame?.src, expectedDocKey);
  const rootNavAllowedRef = useRef<{ key: string | null; allowed: boolean }>({
    key: null,
    allowed: false,
  });
  const initialLoadSeenRef = useRef<{ key: string | null; seen: boolean }>({
    key: null,
    seen: false,
  });
  useLayoutEffect(() => {
    currentKeyRef.current = current;
    if (syncedKeyRef.current !== current) {
      syncedKeyRef.current = current;
      candidatesRef.current = null;
      waiting.current = false;
      navSeqRef.current += 1;
      rootNavAllowedRef.current = {
        key: current,
        allowed: current !== null && isHostVersionRoot,
      };
      initialLoadSeenRef.current = { key: current, seen: false };
    } else if (!isHostVersionRoot && rootNavAllowedRef.current.allowed) {
      rootNavAllowedRef.current = { key: current, allowed: false };
    }
  }, [current, isHostVersionRoot]);
  // The address of the document this frame was last sent from to take its
  // view again, until the next document says hello.
  const carried = useRef<string | null>(null);
  const [loads, setLoads] = useState(0);
  // DALTON-HOOK(mobile-web): track frame document state scoped to `current` frameKey
  const [docTracking, setDocTracking] = useState<{ key: string | null; raw: RawFrameDoc }>({
    key: current,
    raw: { kind: 'pending' },
  });
  const rawDoc: RawFrameDoc = docTracking.key === current ? docTracking.raw : { kind: 'pending' };
  const docState = useMemo<HtmlFrameDocState>(() => {
    if (rawDoc.kind === 'pending') return { kind: 'pending' };
    if (rawDoc.kind === 'root') return { kind: 'bridged_root' };
    if (rawDoc.kind === 'unbridged') return { kind: 'unbridged', currentUrl: null };
    if (expectedDocKey && artifactPathKey(rawDoc.path, docRoot) !== expectedDocKey) {
      return { kind: 'linked', path: rawDoc.path };
    }
    return { kind: 'current', path: rawDoc.path };
  }, [rawDoc, expectedDocKey, docRoot]);
  const reportDocState = useEffectEvent((state: HtmlFrameDocState, key: string | null) => {
    onDocStateChange?.(state, key);
  });
  useEffect(() => {
    reportDocState(docState, current);
  }, [docState, current]);

  const onFrameLoad = (key: string) => {
    dispatch({ type: 'loaded', key });
    onLoad?.();
    if (key !== currentKeyRef.current) return;
    // DALTON-HOOK(mobile-web): every load bumps navSeqRef and starts a grace wait; if candidate
    // page:ready docs exist, ping the frame so only the live loaded document's matching pong confirms state.
    if (initialLoadSeenRef.current.key === key && initialLoadSeenRef.current.seen) {
      rootNavAllowedRef.current = { key, allowed: false };
    } else {
      initialLoadSeenRef.current = { key, seen: true };
    }
    navSeqRef.current += 1;
    const seq = navSeqRef.current;
    waiting.current = true;
    setDocTracking({ key, raw: { kind: 'pending' } });
    if (candidatesRef.current?.key === key && candidatesRef.current.docs.size > 0) {
      post({ type: 'page:ping', seq });
    }
    setLoads((n) => n + 1);
  };
  // A link in the document led somewhere that speaks no bridge (an image, a
  // raw error): what is drawn over the document no longer sits on anything.
  const [offPageOf, setOffPageOf] = useState<string | null>(null);
  const unbridged = useEffectEvent((key: string | null) => {
    // DALTON-HOOK(mobile-web): clear stale bridge.path, revoke host root eligibility, and report unbridged
    if (key !== null) rootNavAllowedRef.current = { key, allowed: false };
    candidatesRef.current = null;
    bridge.clearPath?.();
    setOffPageOf(key);
    setDocTracking({ key, raw: { kind: 'unbridged' } });
    onUnbridged?.();
  });
  useEffect(() => {
    if (!waiting.current) return;
    // DALTON-HOOK(mobile-web): verify navigation sequence & frameKey before firing unbridged timeout
    const seq = navSeqRef.current;
    const key = currentKeyRef.current;
    const timer = setTimeout(() => {
      if (!waiting.current || navSeqRef.current !== seq || currentKeyRef.current !== key) return;
      waiting.current = false;
      unbridged(key);
    }, UNBRIDGED_GRACE_MS);
    return () => clearTimeout(timer);
  }, [loads]);
  useEffect(() => {
    // DALTON-HOOK(mobile-web): reset off-page overlay and doc state on frameKey change
    setOffPageOf(null);
    setDocTracking((prev) => (prev.key === current ? prev : { key: current, raw: { kind: 'pending' } }));
  }, [current]);

  const onMessage = useEffectEvent((message: PageToViewer) => {
    switch (message.type) {
      case 'page:scroll': {
        lastScroll.current = { x: message.x, y: message.y };
        const at = carry.current?.key === current ? carry.current.at : null;
        if (current && at && at.y > 0 && !message.moving && Math.abs(message.y - at.y) <= 1 && Math.abs(message.x - at.x) <= 1) {
          dispatch({ type: 'landed', key: current });
        }
        return;
      }
      case 'page:settled':
        if (current) dispatch({ type: 'settled', key: current });
        return;
      case 'page:ground':
        knownGrounds.set(groundKey, message.color);
        setGround(message.color);
        return;
      case 'page:pong': {
        // DALTON-HOOK(mobile-web): require valid matching docId on both page:ready and page:pong
        // before confirming doc state or publishing bridge.path.
        if (!waiting.current || message.seq !== navSeqRef.current || current === null) {
          return;
        }
        const bucket = candidatesRef.current;
        if (!bucket || bucket.key !== current) return;
        const docId = typeof message.docId === 'string' && message.docId.length > 0 ? message.docId : null;
        if (!docId) return;
        const candidate = bucket.docs.get(docId);
        if (!candidate || candidate.docId !== docId) return;

        let confirmedRaw = candidate.raw;
        if (candidate.claimsRoot) {
          const allowRoot =
            rootNavAllowedRef.current.key === current &&
            rootNavAllowedRef.current.allowed &&
            matchesHostVersionRoot(frame, expectedDocKey, frameRef.current);
          confirmedRaw = allowRoot ? { kind: 'root' } : { kind: 'unbridged' };
        }
        rootNavAllowedRef.current = { key: current, allowed: false };

        if (confirmedRaw.kind === 'path') {
          bridge.setPath?.(confirmedRaw.path);
          setOffPageOf(null);
        } else if (confirmedRaw.kind === 'root') {
          bridge.clearPath?.();
          setOffPageOf(null);
        } else {
          bridge.clearPath?.();
          setOffPageOf(current);
        }

        waiting.current = false;
        // Retain only the confirmed candidate in case this same document's load fires after it
        // answered an earlier wait's ping; prune all other stale candidates.
        bucket.docs.clear();
        bucket.docs.set(docId, { docId, raw: confirmedRaw, claimsRoot: candidate.claimsRoot });
        setDocTracking({ key: current, raw: confirmedRaw });
        return;
      }
      case 'page:frame-loads': {
        // A script or a refresh took this frame to another file without the
        // view, so its document carries only what a frame inside a page gets.
        // Loaded again with it, as a link would have gone, once per document:
        // what that one sent before it went still arrives. It answered, so it
        // counts as its hello.
        // A frame host's document is a version's, which is always bridged.
        const next = frame && framedDirectly(frame.src)
          ? withServeView(message.doc, new URL(frame.src, window.location.href).href)
          : null;
        const el = frameRef.current;
        if (!next || !el || carried.current === message.doc) return;
        carried.current = message.doc;
        // DALTON-HOOK(mobile-web): invalidate prior candidates/root state before reloading with view
        navSeqRef.current += 1;
        waiting.current = false;
        candidatesRef.current = null;
        if (current !== null) rootNavAllowedRef.current = { key: current, allowed: false };
        setDocTracking({ key: current, raw: { kind: 'pending' } });
        sendFrameTo(el, next);
        return;
      }
      case 'page:ready': {
        carried.current = null;
        if (current === null) return;
        // DALTON-HOOK(mobile-web): record page:ready as a candidate only when a valid docId is present;
        // never publish state or cancel the active load wait until a page:pong matching both navSeq and docId confirms it.
        const docId = typeof message.docId === 'string' && message.docId.length > 0 ? message.docId : null;
        if (docId) {
          let candidate: ReadyCandidate;
          if (message.pathState === 'path' && message.path) {
            candidate = { docId, raw: { kind: 'path', path: message.path }, claimsRoot: false };
          } else if (message.pathState === 'root') {
            candidate = { docId, raw: { kind: 'root' }, claimsRoot: true };
          } else {
            candidate = { docId, raw: { kind: 'unbridged' }, claimsRoot: false };
          }

          let bucket = candidatesRef.current;
          if (!bucket || bucket.key !== current) {
            bucket = { key: current, docs: new Map() };
            candidatesRef.current = bucket;
          }
          bucket.docs.delete(docId);
          bucket.docs.set(docId, candidate);
          if (bucket.docs.size > 8) {
            const oldestKey = bucket.docs.keys().next().value;
            if (oldestKey !== undefined) bucket.docs.delete(oldestKey);
          }

          if (waiting.current) {
            post({ type: 'page:ping', seq: navSeqRef.current });
          }
        }

        // The first hello of a new frame, not a link inside it to another file.
        const pending = carry.current;
        if (!pending || pending.key !== current || pending.done) return;
        pending.done = true;
        if (pending.at) post({ type: 'page:restore-scroll', x: pending.at.x, y: pending.at.y });
        return;
      }
      default:
    }
  });
  useEffect(() => subscribe((message) => onMessage(message)), [subscribe]);

  // A first load has nothing to hold on screen, so it waits under a cover,
  // which fades once the document is in.
  const loading = !notice && !behind && (!frame || !loaded);
  const [coverHeld, setCoverHeld] = useState(loading);
  if (loading && !coverHeld) setCoverHeld(true);
  useEffect(() => {
    if (loading || !coverHeld) return;
    const timer = setTimeout(() => setCoverHeld(false), COVER_HOLD_MS);
    return () => clearTimeout(timer);
  }, [loading, coverHeld]);

  // Appended in order and never moved: a frame moved in the DOM reloads.
  const frames = notice ? [] : [behind, frame].filter((f): f is FramedDoc => f !== null);

  return (
    <div
      className="bridged-frame"
      style={ground ? ({ '--page-ground': ground } as React.CSSProperties) : undefined}
    >
      {frames.map((f) => {
        const isCurrent = f.key === current;
        return (
          <HostedFrame
            // A new frame per load rather than a new address on the same one:
            // changing a frame's src adds a step to the browser's history, and
            // Back would then walk the file's documents instead of the app.
            key={f.key}
            ref={isCurrent ? frameRef : undefined}
            src={f.src}
            className="bridged-frame-doc"
            data-state={!isCurrent ? 'leaving' : shown || !behind ? 'shown' : 'pending'}
            title={title}
            sandbox={f.sandbox}
            allow={SERVED_HTML_PERMISSIONS}
            // A crafted link's query must not reach a page that reads its referrer.
            referrerPolicy="no-referrer"
            relay
            tabIndex={isCurrent ? undefined : -1}
            aria-hidden={isCurrent ? undefined : true}
            onLoad={isCurrent ? () => onFrameLoad(f.key) : undefined}
          />
        );
      })}
      {kit && frame && !notice && (
        <div className="bridged-frame-kit">
          <AppBridge frameRef={frameRef} src={frame.src} sandbox={frame.sandbox} loaded={loaded} grantKey={kit.grantKey} active={kit.active} subject="page" />
        </div>
      )}
      {frame && !notice && overlay && (
        <div className="bridged-frame-overlay">{overlay(bridge, { key: frame.key, shown: shown && offPageOf !== frame.key })}</div>
      )}
      {coverHeld && !notice && (
        <div className="bridged-frame-cover" data-state={loading ? 'open' : 'closed'}>
          <Loader size={24} className="text-(--color-text-tertiary)" label={t('common.loading')} />
        </div>
      )}
      {notice && <div className="bridged-frame-cover">{notice}</div>}
    </div>
  );
}
