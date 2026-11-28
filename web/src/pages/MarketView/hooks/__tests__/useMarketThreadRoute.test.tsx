import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, NavigationType, Route, Routes, useLocation, useNavigate, useNavigationType, useSearchParams } from 'react-router';
import { useEffect, useRef, useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { useEditSearchParams } from '@/hooks/useEditSearchParams';
import { useMarketThreadRoute } from '../useMarketThreadRoute';
import { readMarketViewRoute } from '../../utils/marketRoute';
import { setMarketThreadId } from '../../utils/threadPersistence';

interface SentRecord {
  targetThreadId: string;
  workspaceId: string;
  symbol: string;
}

/**
 * Mirrors MarketChatPanel's keyed `<ChatBody>` lifecycle: starts on
 * `initialThreadId`, assigns a real thread ID on first send, persists it to
 * localStorage, and stamps `?thread=` via replace without changing the parent's
 * `opened.init` route key.
 */
function KeyedChatBodyStub({
  workspaceId,
  symbol,
  initialThreadId,
  nextThreadId,
  onMount,
  onSend,
}: {
  workspaceId: string;
  symbol: string;
  initialThreadId: string;
  nextThreadId: () => string;
  onMount: () => void;
  onSend: (rec: SentRecord) => void;
}) {
  const editSearchParams = useEditSearchParams();
  const [liveThreadId, setLiveThreadId] = useState(initialThreadId);
  const mountedSymbolRef = useRef(symbol);

  useEffect(() => {
    onMount();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!liveThreadId || liveThreadId === '__default__') return;
    if (symbol !== mountedSymbolRef.current) return;
    setMarketThreadId(workspaceId, symbol, liveThreadId);
    editSearchParams((params) => params.set('thread', liveThreadId), { replace: true });
  }, [liveThreadId, symbol, workspaceId, editSearchParams]);

  const handleSend = () => {
    onSend({ targetThreadId: liveThreadId, workspaceId, symbol });
    if (liveThreadId === '__default__') {
      setLiveThreadId(nextThreadId());
    }
  };

  return (
    <div>
      <span data-testid="live-thread">{liveThreadId}</span>
      <button type="button" data-testid="send-msg" onClick={handleSend}>
        send
      </button>
    </div>
  );
}

/**
 * Mirrors MarketView's parent-to-child route lifecycle: on a tab navigation,
 * `useSearchParams` updates in Render 1 while the parent's `symbol` and
 * `activeWorkspaceId` state update in the parent's `useEffect` (Render 2).
 */
function MarketRouteHarness({
  tabs,
  sentRecords,
  mountCountRef,
}: {
  tabs: Record<string, string>;
  sentRecords?: SentRecord[];
  mountCountRef?: { current: number };
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const navigationType = useNavigationType();
  const [searchParams] = useSearchParams();
  const editSearchParams = useEditSearchParams();
  const createdSeqRef = useRef(0);

  const opening = readMarketViewRoute(searchParams);
  const [symbol, setSymbol] = useState(opening.symbol || 'AAPL');
  const [workspaceId, setWorkspaceId] = useState<string>(opening.workspaceId || 'home-ws');
  const urlSymbolRef = useRef<string | null>(opening.symbol);
  const urlWsRef = useRef<string | null>(opening.workspaceId);

  useEffect(() => {
    const route = readMarketViewRoute(searchParams);
    if (route.symbol && route.symbol !== urlSymbolRef.current) {
      urlSymbolRef.current = route.symbol;
      if (navigationType !== NavigationType.Replace) {
        setSymbol(route.symbol);
      }
    }
    if (route.workspaceId !== urlWsRef.current) {
      urlWsRef.current = route.workspaceId;
      if (navigationType !== NavigationType.Replace) {
        setWorkspaceId(route.workspaceId ?? 'home-ws');
      }
    }
  }, [searchParams, navigationType]);

  const threadRoute = useMarketThreadRoute(workspaceId, symbol);

  const pickWorkspace = (nextWs: string | null) => {
    setWorkspaceId(nextWs ?? 'home-ws');
    editSearchParams(
      (params) => {
        if (nextWs) params.set('ws', nextWs);
        else params.delete('ws');
      },
      { replace: true },
    );
  };

  const pickSymbol = (nextSym: string) => {
    setSymbol(nextSym);
    editSearchParams((params) => params.set('symbol', nextSym), { replace: true });
  };

  return (
    <div>
      <span data-testid="loc">{location.pathname + location.search}</span>
      <span data-testid="symbol">{symbol}</span>
      <span data-testid="workspace">{workspaceId}</span>
      <span data-testid="thread">{threadRoute.threadId}</span>
      <span data-testid="route-key">{threadRoute.key}</span>
      <KeyedChatBodyStub
        key={`${workspaceId}:${threadRoute.key}`}
        workspaceId={workspaceId}
        symbol={symbol}
        initialThreadId={threadRoute.threadId}
        nextThreadId={() => `th-created-${++createdSeqRef.current}`}
        onMount={() => {
          if (mountCountRef) mountCountRef.current += 1;
        }}
        onSend={(rec) => {
          sentRecords?.push(rec);
        }}
      />
      {Object.entries(tabs).map(([id, url]) => (
        <button key={id} type="button" data-testid={`tab-${id}`} onClick={() => navigate(url)}>
          {id}
        </button>
      ))}
      <button type="button" data-testid="pick-ws-3" onClick={() => pickWorkspace('ws-3')}>
        pick-ws-3
      </button>
      <button type="button" data-testid="pick-home" onClick={() => pickWorkspace(null)}>
        pick-home
      </button>
      <button type="button" data-testid="pick-sym-msft" onClick={() => pickSymbol('MSFT')}>
        pick-msft
      </button>
      <button type="button" data-testid="new-chat" onClick={() => threadRoute.startNewChat()}>
        new-chat
      </button>
    </div>
  );
}

function renderHarness(
  initialEntry: string,
  tabs: Record<string, string>,
  opts?: { sentRecords?: SentRecord[]; mountCountRef?: { current: number } },
) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/market"
          element={
            <MarketRouteHarness
              tabs={tabs}
              sentRecords={opts?.sentRecords}
              mountCountRef={opts?.mountCountRef}
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe('useMarketThreadRoute tab switching & workspace changes', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('restores target URL ?thread= when switching between two threads on the same symbol and workspace', async () => {
    const tabA = '/market?symbol=AAPL&ws=ws-1&thread=th-A';
    const tabB = '/market?symbol=AAPL&ws=ws-1&thread=th-B';
    const tabFresh = '/market?symbol=AAPL&ws=ws-1';
    renderHarness(tabA, { a: tabA, b: tabB, fresh: tabFresh });

    expect(screen.getByTestId('thread').textContent).toBe('th-A');

    // Switch to another thread on the exact same symbol & workspace.
    fireEvent.click(screen.getByTestId('tab-b'));
    await waitFor(() => {
      expect(screen.getByTestId('thread').textContent).toBe('th-B');
    });
    expect(screen.getByTestId('loc').textContent).toBe(tabB);

    // Switch back to tab A.
    fireEvent.click(screen.getByTestId('tab-a'));
    await waitFor(() => {
      expect(screen.getByTestId('thread').textContent).toBe('th-A');
    });
    expect(screen.getByTestId('loc').textContent).toBe(tabA);

    // Switch to a fresh-chat tab on the same symbol & workspace (no ?thread=).
    fireEvent.click(screen.getByTestId('tab-fresh'));
    await waitFor(() => {
      expect(screen.getByTestId('thread').textContent).toBe('__default__');
    });
    expect(screen.getByTestId('loc').textContent).toBe(tabFresh);
  });

  it('preserves target URL ?thread= on cross-workspace tab switches (ws -> ws and ws -> Home -> ws)', async () => {
    const tab1 = '/market?symbol=AAPL&ws=ws-1&thread=th-1';
    const tab2 = '/market?symbol=NVDA&ws=ws-2&thread=th-2';
    const tabHome = '/market?symbol=GOOGL&thread=th-home';
    renderHarness(tab1, { t1: tab1, t2: tab2, home: tabHome });

    expect(screen.getByTestId('workspace').textContent).toBe('ws-1');
    expect(screen.getByTestId('thread').textContent).toBe('th-1');

    // Cross-workspace + cross-symbol tab switch: must NOT delete ?thread=th-2 or open blank chat.
    fireEvent.click(screen.getByTestId('tab-t2'));
    await waitFor(() => {
      expect(screen.getByTestId('workspace').textContent).toBe('ws-2');
      expect(screen.getByTestId('symbol').textContent).toBe('NVDA');
      expect(screen.getByTestId('thread').textContent).toBe('th-2');
    });
    expect(screen.getByTestId('loc').textContent).toBe(tab2);

    // Switch from workspace tab to Home tab with its own thread.
    fireEvent.click(screen.getByTestId('tab-home'));
    await waitFor(() => {
      expect(screen.getByTestId('workspace').textContent).toBe('home-ws');
      expect(screen.getByTestId('symbol').textContent).toBe('GOOGL');
      expect(screen.getByTestId('thread').textContent).toBe('th-home');
    });
    expect(screen.getByTestId('loc').textContent).toBe(tabHome);

    // Switch back to tab 1.
    fireEvent.click(screen.getByTestId('tab-t1'));
    await waitFor(() => {
      expect(screen.getByTestId('workspace').textContent).toBe('ws-1');
      expect(screen.getByTestId('symbol').textContent).toBe('AAPL');
      expect(screen.getByTestId('thread').textContent).toBe('th-1');
    });
    expect(screen.getByTestId('loc').textContent).toBe(tab1);
  });

  it('clears ?thread= and starts a fresh chat when the user actively switches workspace or starts a new chat', async () => {
    const tab1 = '/market?symbol=AAPL&ws=ws-1&thread=th-1';
    const tab2 = '/market?symbol=NVDA&ws=ws-2&thread=th-2';
    renderHarness(tab1, { t1: tab1, t2: tab2 });

    // First switch tabs to tab 2 so route settling runs and completes.
    fireEvent.click(screen.getByTestId('tab-t2'));
    await waitFor(() => {
      expect(screen.getByTestId('workspace').textContent).toBe('ws-2');
      expect(screen.getByTestId('thread').textContent).toBe('th-2');
    });

    // Now the user actively switches workspace in the current tab -> drops ?thread= and opens fresh chat.
    fireEvent.click(screen.getByTestId('pick-ws-3'));
    await waitFor(() => {
      expect(screen.getByTestId('workspace').textContent).toBe('ws-3');
      expect(screen.getByTestId('thread').textContent).toBe('__default__');
      expect(screen.getByTestId('loc').textContent).toBe('/market?symbol=NVDA&ws=ws-3');
    });

    // Switch back to tab 1, then actively start a new chat -> drops ?thread=.
    fireEvent.click(screen.getByTestId('tab-t1'));
    await waitFor(() => {
      expect(screen.getByTestId('thread').textContent).toBe('th-1');
    });
    fireEvent.click(screen.getByTestId('new-chat'));
    await waitFor(() => {
      expect(screen.getByTestId('thread').textContent).toBe('__default__');
      expect(screen.getByTestId('loc').textContent).toBe('/market?symbol=AAPL&ws=ws-1');
    });
  });

  it('restores saved thread or drops ?thread= on in-tab symbol switch', async () => {
    setMarketThreadId('ws-1', 'MSFT', 'th-msft-saved');
    const tab1 = '/market?symbol=AAPL&ws=ws-1&thread=th-1';
    renderHarness(tab1, { t1: tab1 });

    // In-tab symbol switch to MSFT restores saved thread.
    fireEvent.click(screen.getByTestId('pick-sym-msft'));
    await waitFor(() => {
      expect(screen.getByTestId('symbol').textContent).toBe('MSFT');
      expect(screen.getByTestId('thread').textContent).toBe('th-msft-saved');
    });

    // Switching workspace to Home drops ?thread= and starts fresh.
    act(() => {
      fireEvent.click(screen.getByTestId('pick-home'));
    });
    await waitFor(() => {
      expect(screen.getByTestId('workspace').textContent).toBe('home-ws');
      expect(screen.getByTestId('thread').textContent).toBe('__default__');
      expect(screen.getByTestId('loc').textContent).not.toContain('thread=');
    });
  });

  it('remounts chat for a fresh thread when opening the same Home symbol in a new tab after first send', async () => {
    const sentRecords: SentRecord[] = [];
    const mountCountRef = { current: 0 };
    const homeAapl = '/market?symbol=AAPL';
    renderHarness(homeAapl, { newAaplTab: homeAapl }, { sentRecords, mountCountRef });

    expect(screen.getByTestId('thread').textContent).toBe('__default__');
    expect(screen.getByTestId('live-thread').textContent).toBe('__default__');
    expect(mountCountRef.current).toBe(1);
    const initialKey = screen.getByTestId('route-key').textContent;

    // First send on Home chart creates a real thread ID and stamps ?thread= via replace,
    // while useMarketThreadRoute's opened.init remains on __default__.
    fireEvent.click(screen.getByTestId('send-msg'));
    await waitFor(() => {
      expect(screen.getByTestId('live-thread').textContent).toBe('th-created-1');
      expect(screen.getByTestId('loc').textContent).toBe('/market?symbol=AAPL&thread=th-created-1');
    });
    expect(screen.getByTestId('thread').textContent).toBe('__default__');
    expect(sentRecords).toEqual([
      { targetThreadId: '__default__', workspaceId: 'home-ws', symbol: 'AAPL' },
    ]);

    // Open the same symbol in a new tab from the command palette (no ?thread=).
    // Must remount ChatBody so the next message starts a brand-new thread instead of posting to th-created-1.
    fireEvent.click(screen.getByTestId('tab-newAaplTab'));
    await waitFor(() => {
      expect(mountCountRef.current).toBe(2);
      expect(screen.getByTestId('live-thread').textContent).toBe('__default__');
    });
    expect(screen.getByTestId('route-key').textContent).not.toBe(initialKey);
    expect(screen.getByTestId('loc').textContent).toBe('/market?symbol=AAPL');

    // Sending in the new tab starts a new conversation rather than targeting th-created-1.
    fireEvent.click(screen.getByTestId('send-msg'));
    await waitFor(() => {
      expect(screen.getByTestId('live-thread').textContent).toBe('th-created-2');
      expect(screen.getByTestId('loc').textContent).toBe('/market?symbol=AAPL&thread=th-created-2');
    });
    expect(sentRecords[1]).toEqual({
      targetThreadId: '__default__',
      workspaceId: 'home-ws',
      symbol: 'AAPL',
    });
  });

  it('keeps a fresh chat when switching across symbols to a same-workspace tab without ?thread= even if localStorage has an old thread', async () => {
    setMarketThreadId('ws-1', 'NVDA', 'th-nvda-old');
    const sentRecords: SentRecord[] = [];
    const tabAapl = '/market?symbol=AAPL&ws=ws-1&thread=th-aapl-1';
    const tabNvdaFresh = '/market?symbol=NVDA&ws=ws-1';
    const { unmount } = renderHarness(tabAapl, { aapl: tabAapl, nvdaFresh: tabNvdaFresh }, { sentRecords });

    expect(screen.getByTestId('workspace').textContent).toBe('ws-1');
    expect(screen.getByTestId('symbol').textContent).toBe('AAPL');
    expect(screen.getByTestId('live-thread').textContent).toBe('th-aapl-1');

    // Cross-symbol switch within the same workspace to a tab with no ?thread=.
    // Follow-up symbol alignment must NOT overwrite the fresh chat with th-nvda-old from localStorage.
    fireEvent.click(screen.getByTestId('tab-nvdaFresh'));
    await waitFor(() => {
      expect(screen.getByTestId('symbol').textContent).toBe('NVDA');
      expect(screen.getByTestId('workspace').textContent).toBe('ws-1');
    });
    expect(screen.getByTestId('thread').textContent).toBe('__default__');
    expect(screen.getByTestId('live-thread').textContent).toBe('__default__');
    expect(screen.getByTestId('loc').textContent).toBe(tabNvdaFresh);

    // Next send starts a new thread rather than resuming th-nvda-old.
    fireEvent.click(screen.getByTestId('send-msg'));
    await waitFor(() => {
      expect(screen.getByTestId('live-thread').textContent).toBe('th-created-1');
      expect(screen.getByTestId('loc').textContent).toBe('/market?symbol=NVDA&ws=ws-1&thread=th-created-1');
    });
    expect(sentRecords).toEqual([
      { targetThreadId: '__default__', workspaceId: 'ws-1', symbol: 'NVDA' },
    ]);

    // Mounting directly on a URL without ?thread= also stays fresh despite localStorage.
    unmount();
    setMarketThreadId('ws-1', 'NVDA', 'th-nvda-old');
    renderHarness(tabNvdaFresh, {});
    expect(screen.getByTestId('thread').textContent).toBe('__default__');
    expect(screen.getByTestId('live-thread').textContent).toBe('__default__');
    expect(screen.getByTestId('loc').textContent).toBe(tabNvdaFresh);
  });
});
