/**
 * The tasks chip stands for a running task no row names (a workflow's own
 * steps, or a row not loaded yet) even while it keeps settled rows, so the
 * tray still reads as working.
 */
import { describe, it, expect } from 'vitest';
import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import type { MarketTarget } from '../../../utils/api';
import { trayWorking, useTrayContents } from '../useTrayContents';

function readTray(hasActiveSubagents: boolean) {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => useTrayContents({
    marketWatch: null,
    cards: {},
    taskHistory: { 'task:Qm3xLp': { description: 'Pull filings', status: 'completed', messages: [], lastActivityMs: 1_700_000_000_000 } },
    messages: [],
    isLoading: false,
    hasActiveSubagents,
    reportBackOwed: false,
    owedThreads: null,
    isHome: false,
  }), { wrapper });
  return result.current;
}

describe('useTrayContents: a running task no row names', () => {
  it('keeps the settled rows and still reads as working', () => {
    const contents = readTray(true);
    expect(contents?.tasks).toEqual({ rows: [expect.objectContaining({ id: 'task:Qm3xLp', running: false })], unnamedRunning: true });
    expect(trayWorking(contents!)).toBe(true);
  });

  it('is quiet once nothing runs', () => {
    const contents = readTray(false);
    expect(contents?.tasks?.unnamedRunning).toBe(false);
    expect(trayWorking(contents!)).toBe(false);
  });
});

// Neutral placeholder ticker and a made-up level only.
function target(over: Partial<MarketTarget>): MarketTarget {
  return {
    target_id: 'tg-1', symbol: 'NVDA', kind: 'above', value: 205, pct_from: null, armed_price: null,
    status: 'armed', expires_at: '2030-01-02T00:00:00Z', fired_at: null, fired_price: null,
    created_at: '2030-01-01T00:00:00Z', ...over,
  };
}

function readWatch(targets: MarketTarget[]) {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => useTrayContents({
    marketWatch: { symbols: [], targets },
    cards: {},
    taskHistory: {},
    messages: [],
    isLoading: false,
    hasActiveSubagents: false,
    reportBackOwed: false,
    owedThreads: null,
    isHome: false,
  }), { wrapper });
  return result.current;
}

describe('useTrayContents: a target with no live-stamped symbol', () => {
  it('keeps the watch chip while the target is armed', () => {
    expect(readWatch([target({})])?.watch).not.toBeNull();
  });

  it('drops it once every target has settled', () => {
    expect(readWatch([target({ status: 'fired' }), target({ target_id: 'b', status: 'expired' })])).toBeNull();
  });
});
