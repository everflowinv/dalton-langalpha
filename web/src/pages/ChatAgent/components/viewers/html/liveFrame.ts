import { useCallback, useEffect, useEffectEvent, useMemo, useState } from 'react';
import { useOwnFileGrant } from '@/hooks/useOwnFileGrant';
import { useWorkspaceFileGrant } from '@/hooks/useWorkspaceFileGrant';
import { notePageLoads, notePageOpen } from '@/lib/pageLoads';
import { useWorkspaceFolders } from '../../../contexts/WorkspaceContext';
import type { FrameSource, HtmlFrameDocState } from './BridgedFrame';
import { docStatesEqual } from './pageProtocol'; // DALTON-HOOK(mobile-web)
import { useBridgeMessage, type PageBridge } from './usePageBridge';
import { SERVED_HTML_SANDBOX } from './sandbox';
import { buildServeUrl, type ServeInject } from './wsfilesUrl';

interface LiveFrameOptions {
  workspaceId: string;
  /** Workspace-relative, or as the panel's tab holds it. */
  filePath: string;
  /** A serve prefix the page was handed (a share's, `/a/`'s `frame_base`); omitted, the owner's grant. */
  prefix?: string | null;
  /** Without `prefix`, a grant no other frame shares (`useOwnFileGrant`), so what it reports is the files as they are now. */
  ownGrant?: boolean;
  /** The publish review's own load of the page, whose report stands for the page while it is open (`notePageOpen`). */
  scout?: boolean;
  inject: ServeInject;
  /**
   * Which bytes of the file to show. A new one loads the file again, over the
   * old one with the place kept; the same one keeps the browser's copy.
   */
  revision?: string;
  /** The section a reference pointed at when the frame first loaded. */
  anchor?: string | null;
  /** Counts the asks for that section, which makes a repeat one a new ask. */
  anchorSeq?: number | null;
  /** Where a link to another workspace file opens. */
  onOpenLink?: FrameLinkHandler | null;
}

export interface LiveFrame {
  /** Null until the prefix is known. */
  source: FrameSource | null;
  /** The owner's grant never arrived, so nothing will load until `retry`. */
  failed: boolean;
  retry: () => void;
  /** The frame's bridge once it has handed one over (`onBridge`). */
  bridge: PageBridge | null;
  /** For the frame's `onBridge`. */
  // DALTON-HOOK(mobile-web): frameKey prevents stale frames from writing into a newly activated tab
  onBridge: (bridge: PageBridge, frameKey?: string | null) => void;
  // DALTON-HOOK(mobile-web): active frame document state (pending immediately on tab switch) and attachment flag
  docState: HtmlFrameDocState;
  hasDocState: boolean;
  onDocStateChange: (state: HtmlFrameDocState, frameKey?: string | null) => void;
}

/**
 * A workspace file framed with its bridge. The fragment lands the first load
 * of a section; a later ask goes to the document (`useFragmentScroll`), which
 * keeps its place otherwise. What the document reports loading is kept for the
 * publish review under the file it says it is: a tab may hold another spelling
 * of the path, and a link may have led the frame to another file.
 */
export function useLiveFrame({
  workspaceId, filePath, prefix: given = null, ownGrant = false, scout = false, inject, revision, anchor = null, anchorSeq = null,
  onOpenLink = null,
}: LiveFrameOptions): LiveFrame {
  const root = useWorkspaceFolders()?.rootDir;
  const shared = useWorkspaceFileGrant(given || ownGrant ? null : workspaceId);
  const own = useOwnFileGrant(given || !ownGrant ? null : workspaceId);
  const grant = ownGrant ? own : shared;
  const prefix = given ?? grant.data?.prefix;
  // The viewer stays mounted from tab to tab, and the panel from workspace to
  // workspace, where the same path is another file. So a file's first fragment
  // is held with its file and never lands on the next one.
  const file = `${workspaceId}:${filePath}`;
  const [first, setFirst] = useState({ file, anchor });
  if (first.file !== file) setFirst({ file, anchor });
  const firstAnchor = first.file === file ? first.anchor : anchor;
  // DALTON-HOOK(mobile-web): derive frameKey independent of prefix so bridge attachment works even before/without a grant, while still isolating tab switches
  const activeKey = `${inject}:${file}:${revision ?? ''}`;
  const source = useMemo<FrameSource | null>(() => {
    if (!prefix) return null;
    const url = buildServeUrl(prefix, filePath, { inject, root, revision });
    return {
      key: activeKey,
      src: firstAnchor ? `${url}#${encodeURIComponent(firstAnchor)}` : url,
      sandbox: SERVED_HTML_SANDBOX,
    };
  }, [prefix, activeKey, filePath, inject, root, revision, firstAnchor]);
  // DALTON-HOOK(mobile-web): key bridge and docState by active frameKey so tab switches immediately return pending/null
  const [bridgeEntry, setBridgeEntry] = useState<{ key: string | null; bridge: PageBridge | null }>({
    key: null,
    bridge: null,
  });
  const [docEntry, setDocEntry] = useState<{ key: string | null; state: HtmlFrameDocState }>({
    key: null,
    state: { kind: 'pending' },
  });
  const onBridge = useCallback(
    (next: PageBridge, frameKey?: string | null) => {
      const resolvedKey = frameKey !== undefined ? frameKey : (next.frameKey !== undefined ? next.frameKey : activeKey);
      if (resolvedKey !== activeKey) return;
      setBridgeEntry({ key: resolvedKey, bridge: next });
    },
    [activeKey],
  );
  const onDocStateChange = useCallback(
    (next: HtmlFrameDocState, frameKey?: string | null) => {
      const resolvedKey = frameKey !== undefined ? frameKey : activeKey;
      if (resolvedKey !== activeKey) return;
      setDocEntry((prev) =>
        prev.key === resolvedKey && docStatesEqual(prev.state, next)
          ? prev
          : { key: resolvedKey, state: next },
      );
    },
    [activeKey],
  );
  const bridge = bridgeEntry.key === activeKey ? bridgeEntry.bridge : null;
  // DALTON-HOOK(mobile-web): distinguish a live BridgedFrame that has reported state from an unattached/mocked frame
  const hasDocState = docEntry.key !== null;
  const docState: HtmlFrameDocState =
    docEntry.key === activeKey ? docEntry.state : { kind: 'pending' };
  useFragmentScroll(bridge, anchor, anchorSeq);
  useFrameLinks(bridge, onOpenLink);
  useBridgeMessage(bridge, 'page:loads', (message) => {
    if (bridge) notePageLoads(workspaceId, message.path, message.paths, bridge);
  });
  // Under the file the document says it is, as its report is: a link with a
  // query leads the frame to another file in place.
  const shown = bridge?.path ?? null;
  useEffect(
    () => (bridge && shown ? notePageOpen(workspaceId, shown, bridge, { scout }) : undefined),
    [bridge, workspaceId, shown, scout],
  );
  return {
    source,
    // A failed renewal keeps the grant it already had, and the frame keeps
    // loading under it; only a grant that never arrived is an error to show.
    failed: !prefix && !!grant.error,
    retry: () => void grant.refetch(),
    bridge,
    // DALTON-HOOK(mobile-web): return frameKey-guarded onBridge and unified docState callbacks
    onBridge,
    docState,
    hasDocState,
    onDocStateChange,
  };
}

/** Opens a workspace file a document linked to, and the section the link named or an empty string. */
export type FrameLinkHandler = (path: string, fragment: string) => void;

/**
 * Follows a link to another workspace file (`page:open-file`). A page can name
 * any path, so it is heard only on the heels of a click inside a frame, which
 * is what following a link is: a click on the app counts as activation too, and
 * the one that opened the panel must not let a page open another file.
 */
export function useFrameLinks(bridge: PageBridge | null, onOpen: FrameLinkHandler | null | undefined): void {
  useBridgeMessage(bridge, 'page:open-file', (message) => {
    if (!(document.activeElement instanceof HTMLIFrameElement)) return;
    if (navigator.userActivation && !navigator.userActivation.isActive) return;
    onOpen?.(message.path, message.fragment);
  });
}

/**
 * Scrolls the document to the section a reference asked for, every time it
 * asks: a second reference to the section the URL already names moves neither
 * `src` nor the frame's location, so only the document can land it.
 * `anchorSeq` counts the asks.
 */
function useFragmentScroll(bridge: PageBridge | null, anchor: string | null, anchorSeq: number | null): void {
  const request = anchor ? `${anchorSeq ?? ''}\u0000${anchor}` : null;
  const ask = useEffectEvent(() => {
    if (anchor && bridge?.ready) bridge.post({ type: 'page:scroll-to', fragment: anchor });
  });
  useEffect(() => { ask(); }, [request, bridge?.ready]);
}
