import { useCallback, useEffect, useRef, useState } from 'react';
import { NavigationType, useLocation, useNavigationType, useSearchParams } from 'react-router';
import { displaySpelling } from '@/lib/bars';
import { useEditSearchParams } from '@/hooks/useEditSearchParams';
import type { SendOptions } from '../../ChatAgent/session/types';
import type { WidgetSendParts } from '../../ChatAgent/utils/widgetPrompt';
import { getMarketThreadId, setMarketThreadId, clearMarketThreadId } from '../utils/threadPersistence';
import { readMarketViewRoute } from '../utils/marketRoute';

/** A widget's startThread, carried into the new chat it opens. */
export interface WidgetFirstSend {
  text: string;
  parts: WidgetSendParts;
  /** The model and settings of the chat it came from, which the new thread keeps. */
  options: SendOptions;
}

export interface MarketThreadRoute {
  /** Keys the chat: a new one remounts it, so its engine starts on `threadId`. */
  key: string;
  /** The thread the chat opens on; `__default__` for a new chat. */
  threadId: string;
  /** What the chat was opened to send first; only `startThread` opens one with it. */
  firstSend?: WidgetFirstSend;
  selectThread: (threadId: string) => void;
  startNewChat: () => void;
  /** A new chat that sends `first` once mounted. */
  startThread: (first: WidgetFirstSend) => void;
}

/** A thread to open, with what the chat opened on it sends first. Replaced
 *  whole on every move, so no other move can inherit a pending first send. */
interface Opened {
  /** A thread id, or `__default__#<n>`: the suffix forces a remount even
   *  when already on a new chat, and is stripped before the engine sees it. */
  init: string;
  firstSend?: WidgetFirstSend;
}

let freshChatSeq = 0;
const freshChat = (): Opened => ({ init: `__default__#${++freshChatSeq}` });

interface PendingRouteSync {
  threadId: string | null;
  expectSymbol: boolean;
  expectWorkspace: boolean;
}

/**
 * Which thread the market chat shows. URL `?thread=` wins, then localStorage
 * keyed by (workspace, symbol), then a new chat. Tab / route navigations
 * restore the target URL's `?thread=` (even across workspaces or between two
 * threads on the same symbol & workspace). In-tab user symbol switches restore
 * the last thread seen for the symbol in this workspace; in-tab workspace/scope
 * changes and explicit new chats start a clean slate and drop `?thread`.
 */
export function useMarketThreadRoute(workspaceId: string | null, symbol: string): MarketThreadRoute {
  const location = useLocation();
  const navigationType = useNavigationType();
  const [searchParams] = useSearchParams();
  const editSearchParams = useEditSearchParams();
  const initialRoute = readMarketViewRoute(searchParams);
  const [opened, setOpened] = useState<Opened>(() => ({
    init: initialRoute.threadId || (getMarketThreadId(workspaceId, symbol) ?? '__default__'),
  }));

  const dropThreadParam = useCallback(
    () => editSearchParams((params) => params.delete('thread'), { replace: true }),
    [editSearchParams],
  );

  const lastRouteKeyRef = useRef(location.key);
  const lastUrlTargetWsRef = useRef<string | null>(
    initialRoute.scope === 'all' ? null : initialRoute.workspaceId,
  );
  const routeSyncRef = useRef<PendingRouteSync | null>(null);
  const lastSymbolRef = useRef(symbol);
  // localStorage is keyed by (workspace, symbol), so the previous scope's
  // pointer stays intact and is reachable via the history dropdown.
  const prevWorkspaceRef = useRef<string | null>(workspaceId);

  useEffect(() => {
    const route = readMarketViewRoute(searchParams);
    const urlThreadId = route.threadId;
    const urlSymbol = route.symbol ? displaySpelling(route.symbol) : null;
    const urlTargetWs = route.scope === 'all' ? null : route.workspaceId;

    const isNewNav = location.key !== lastRouteKeyRef.current;
    const isRouteNav = isNewNav && navigationType !== NavigationType.Replace;
    const prevUrlTargetWs = lastUrlTargetWsRef.current;
    if (isNewNav) {
      lastRouteKeyRef.current = location.key;
      lastUrlTargetWsRef.current = urlTargetWs;
    }

    const symbolChanged = lastSymbolRef.current !== symbol;
    if (symbolChanged) lastSymbolRef.current = symbol;

    const prevWorkspace = prevWorkspaceRef.current;
    const workspaceChanged = prevWorkspace !== workspaceId;
    if (workspaceChanged) prevWorkspaceRef.current = workspaceId;
    const switchedWorkspace = workspaceChanged && prevWorkspace !== null && workspaceId !== null;

    if (isRouteNav) {
      const expectSymbol = !symbolChanged && Boolean(urlSymbol && urlSymbol !== symbol);
      const expectWorkspace = !switchedWorkspace && (
        urlTargetWs !== null ? urlTargetWs !== workspaceId : prevUrlTargetWs !== null
      );
      routeSyncRef.current =
        expectSymbol || expectWorkspace
          ? { threadId: urlThreadId, expectSymbol, expectWorkspace }
          : null;

      if (urlThreadId) {
        setOpened((cur) => (cur.init === urlThreadId ? cur : { init: urlThreadId }));
      } else if (switchedWorkspace) {
        setOpened(freshChat());
      } else if (symbolChanged) {
        const restored = getMarketThreadId(workspaceId, symbol);
        setOpened({ init: restored ?? '__default__' });
      } else {
        setOpened((cur) => (cur.init.startsWith('__default__') ? cur : freshChat()));
      }
      return;
    }

    const pending = routeSyncRef.current;
    if (pending && (symbolChanged || switchedWorkspace)) {
      const nextExpectSymbol = symbolChanged ? false : pending.expectSymbol;
      const nextExpectWorkspace = switchedWorkspace ? false : pending.expectWorkspace;
      routeSyncRef.current =
        nextExpectSymbol || nextExpectWorkspace
          ? { ...pending, expectSymbol: nextExpectSymbol, expectWorkspace: nextExpectWorkspace }
          : null;

      if (pending.threadId) {
        setOpened((cur) => (cur.init === pending.threadId ? cur : { init: pending.threadId! }));
      } else if (switchedWorkspace) {
        setOpened(freshChat());
      } else if (symbolChanged) {
        const restored = getMarketThreadId(workspaceId, symbol);
        setOpened({ init: restored ?? '__default__' });
      }
      return;
    }

    if (switchedWorkspace) {
      routeSyncRef.current = null;
      setOpened(freshChat());
      dropThreadParam();
      return;
    }

    if (symbolChanged) {
      routeSyncRef.current = null;
      const restored = getMarketThreadId(workspaceId, symbol);
      setOpened({ init: restored ?? '__default__' });
      if (!restored) dropThreadParam();
    }
  }, [location.key, navigationType, searchParams, workspaceId, symbol, dropThreadParam]);

  const selectThread = useCallback((threadId: string) => {
    routeSyncRef.current = null;
    setMarketThreadId(workspaceId, symbol, threadId);
    setOpened({ init: threadId });
  }, [workspaceId, symbol]);

  const openFresh = useCallback((firstSend?: WidgetFirstSend) => {
    routeSyncRef.current = null;
    clearMarketThreadId(workspaceId, symbol);
    setOpened({ ...freshChat(), ...(firstSend ? { firstSend } : {}) });
    dropThreadParam();
  }, [workspaceId, symbol, dropThreadParam]);

  const startNewChat = useCallback(() => openFresh(), [openFresh]);

  return {
    key: opened.init,
    threadId: opened.init.split('#')[0],
    firstSend: opened.firstSend,
    selectThread,
    startNewChat,
    startThread: openFresh,
  };
}
