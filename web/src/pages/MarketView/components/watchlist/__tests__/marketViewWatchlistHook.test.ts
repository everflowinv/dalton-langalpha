import { describe, expect, it, vi } from 'vitest';

vi.mock('@/config/brand', () => ({ isDaltonBuild: true }));

import {
  navigateDaltonChartJump,
  shouldConsumeMarketRouteParam,
} from '../marketViewWatchlistHook';

describe('marketViewWatchlistHook route param preservation', () => {
  it('keeps ws and returnTo in the tab URL so tab switches restore workspace and chat return path', () => {
    expect(shouldConsumeMarketRouteParam('ws')).toBe(false);
    expect(shouldConsumeMarketRouteParam('returnTo')).toBe(false);
    expect(shouldConsumeMarketRouteParam('symbol')).toBe(false);
    expect(shouldConsumeMarketRouteParam('thread')).toBe(false);
    expect(shouldConsumeMarketRouteParam('tf')).toBe(false);
    // One-shot params are still consumed.
    expect(shouldConsumeMarketRouteParam('comment')).toBe(true);
    expect(shouldConsumeMarketRouteParam('mode')).toBe(true);
  });

  it('preserves ws and returnTo when jumping charts within a MarketView tab', () => {
    const navigate = vi.fn();
    const current = new URLSearchParams('symbol=AAPL&ws=ws-1&thread=th-1&returnTo=%2Fchat%2Ft%2Ft1');
    navigateDaltonChartJump(navigate, 'NVDA', '1hour', false, undefined, current);
    expect(navigate).toHaveBeenCalledTimes(1);
    const [url, opts] = navigate.mock.calls[0];
    const params = new URLSearchParams(String(url).split('?')[1]);
    expect(params.get('symbol')).toBe('NVDA');
    expect(params.get('tf')).toBe('1hour');
    expect(params.get('ws')).toBe('ws-1');
    expect(params.get('thread')).toBeNull();
    expect(params.get('returnTo')).toBe('/chat/t/t1');
    expect(opts).toEqual({ replace: true });
  });

  it('preserves thread when switching timeframe on the same symbol (including alias spelling)', () => {
    const navigate = vi.fn();
    const current = new URLSearchParams('symbol=NVDA&tf=1day&ws=ws-1&thread=th-1&returnTo=%2Fchat%2Ft%2Ft1');
    navigateDaltonChartJump(navigate, 'NVDA', '4hour', false, undefined, current);
    expect(navigate).toHaveBeenCalledTimes(1);
    const [url, opts] = navigate.mock.calls[0];
    const params = new URLSearchParams(String(url).split('?')[1]);
    expect(params.get('symbol')).toBe('NVDA');
    expect(params.get('tf')).toBe('4hour');
    expect(params.get('ws')).toBe('ws-1');
    expect(params.get('thread')).toBe('th-1');
    expect(params.get('returnTo')).toBe('/chat/t/t1');
    expect(opts).toEqual({ replace: true });

    const aliasNav = vi.fn();
    const aliasCurrent = new URLSearchParams('symbol=600519.SH&tf=1day&thread=th-cn');
    navigateDaltonChartJump(aliasNav, '600519.SS', '1hour', false, undefined, aliasCurrent);
    const aliasParams = new URLSearchParams(String(aliasNav.mock.calls[0][0]).split('?')[1]);
    expect(aliasParams.get('thread')).toBe('th-cn');
    expect(aliasParams.get('tf')).toBe('1hour');
  });
});
