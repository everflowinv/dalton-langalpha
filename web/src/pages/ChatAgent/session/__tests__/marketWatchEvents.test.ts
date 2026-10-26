// @vitest-environment node
import { describe, it, expect, vi } from 'vitest';
import {
  armedTargets, coerceNotices, handleMarketWatchUpdate, marketAlertId, nextLapse, resolveNotice, targetsAt,
  wakeNotices,
} from '../marketWatchEvents';
import type { MarketWatchState } from '../marketWatchEvents';
import type { MarketTarget } from '../../utils/api/threads';

// Neutral placeholder tickers and made-up levels only.
function target(over: Partial<MarketTarget> = {}): MarketTarget {
  return {
    target_id: 'tg-1',
    symbol: 'NVDA',
    kind: 'above',
    value: 205,
    pct_from: null,
    armed_price: null,
    status: 'armed',
    expires_at: '2030-01-02T00:00:00Z',
    fired_at: null,
    fired_price: null,
    created_at: '2030-01-01T00:00:00Z',
    ...over,
  };
}

describe('handleMarketWatchUpdate (streaming)', () => {
  it('parses symbols + content + timestamp onto the setter', () => {
    const setMarketWatch = vi.fn<(next: MarketWatchState) => void>();
    const handled = handleMarketWatchUpdate({
      event: {
        symbols: ['NVDA', 'TSLA'],
        content: 'NVDA 900.12 (+1.2%)',
        timestamp: 1719878400,
      },
      setMarketWatch,
    });

    expect(handled).toBe(true);
    expect(setMarketWatch).toHaveBeenCalledTimes(1);
    expect(setMarketWatch).toHaveBeenCalledWith({
      symbols: ['NVDA', 'TSLA'],
      content: 'NVDA 900.12 (+1.2%)',
      timestamp: 1719878400,
    });
  });

  it('coerces a missing/non-array symbols field to an empty list (watch off)', () => {
    const setMarketWatch = vi.fn<(next: MarketWatchState) => void>();
    handleMarketWatchUpdate({
      event: { content: '', timestamp: undefined },
      setMarketWatch,
    });

    expect(setMarketWatch).toHaveBeenCalledWith({
      symbols: [],
      content: '',
      timestamp: undefined,
    });
  });

  it('drops non-string entries from the symbols list', () => {
    const setMarketWatch = vi.fn<(next: MarketWatchState) => void>();
    handleMarketWatchUpdate({
      event: { symbols: ['NVDA', 42, null, 'AAPL'] },
      setMarketWatch,
    });

    const arg = setMarketWatch.mock.calls[0][0];
    expect(arg.symbols).toEqual(['NVDA', 'AAPL']);
  });

  it('leaves content/timestamp undefined when the event omits them', () => {
    const setMarketWatch = vi.fn<(next: MarketWatchState) => void>();
    handleMarketWatchUpdate({
      event: { symbols: ['NVDA'] },
      setMarketWatch,
    });

    const arg = setMarketWatch.mock.calls[0][0];
    expect(arg.symbols).toEqual(['NVDA']);
    expect(arg.content).toBeUndefined();
    expect(arg.timestamp).toBeUndefined();
  });
});

describe('armedTargets', () => {
  it('keeps only the targets still waiting on their condition', () => {
    const list = [
      target({ target_id: 'a' }),
      target({ target_id: 'b', status: 'fired' }),
      target({ target_id: 'c', status: 'cancelled' }),
    ];
    expect(armedTargets(list).map((t) => t.target_id)).toEqual(['a']);
    expect(armedTargets(null)).toEqual([]);
  });
});

// The server settles a lapsed target only on its next sweep, and the view
// re-reads its targets only on a turn end, an alert or a cancel.
describe('targetsAt', () => {
  const EXPIRY = Date.parse('2030-01-02T00:00:00Z');

  it('reads an armed target past its expiry as expired, and nothing else', () => {
    const list = [
      target({ target_id: 'a' }),
      target({ target_id: 'b', expires_at: '2030-01-03T00:00:00Z' }),
      target({ target_id: 'c', status: 'fired' }),
    ];
    const at = targetsAt(list, EXPIRY);
    expect(at?.map((t) => t.status)).toEqual(['expired', 'armed', 'fired']);
    expect(at?.[2]).toBe(list[2]);
    expect(armedTargets(at).map((t) => t.target_id)).toEqual(['b']);
  });

  it('hands back the same list while none has lapsed', () => {
    const list = [target()];
    expect(targetsAt(list, EXPIRY - 1)).toBe(list);
    expect(targetsAt(undefined, EXPIRY)).toBeUndefined();
  });
});

describe('nextLapse', () => {
  it('names the soonest expiry still ahead among the armed targets', () => {
    const list = [
      target({ target_id: 'a', expires_at: '2030-01-03T00:00:00Z' }),
      target({ target_id: 'b' }),
      target({ target_id: 'c', status: 'fired', expires_at: '2030-01-01T12:00:00Z' }),
    ];
    expect(nextLapse(list, Date.parse('2030-01-01T00:00:00Z'))).toBe(Date.parse('2030-01-02T00:00:00Z'));
    expect(nextLapse(list, Date.parse('2030-01-02T00:00:00Z'))).toBe(Date.parse('2030-01-03T00:00:00Z'));
    expect(nextLapse(list, Date.parse('2030-01-03T00:00:00Z'))).toBeNull();
    expect(nextLapse([target({ expires_at: 'not a date' })], 0)).toBeNull();
  });
});

describe('coerceNotices', () => {
  it('keeps typed fields and drops entries without a target id', () => {
    const notices = coerceNotices([
      {
        target_id: 'tg-1', notice: 'hit', symbol: 'NVDA', kind: 'above', value: 205,
        pct_from: null, fired_price: 205.12, fired_at: '2030-01-01T15:05:00Z', expires_at: null,
      },
      { notice: 'hit', symbol: 'TSLA' },
      'not a notice',
      { target_id: '' },
    ]);
    expect(notices).toEqual([{
      target_id: 'tg-1', notice: 'hit', symbol: 'NVDA', kind: 'above', value: 205,
      pct_from: null, fired_price: 205.12, fired_at: '2030-01-01T15:05:00Z', expires_at: null,
    }]);
  });

  it('drops fields of the wrong type rather than the notice', () => {
    const [notice] = coerceNotices([{ target_id: 'tg-1', notice: 'later', kind: 'sideways', value: '205', fired_price: NaN }]);
    expect(notice).toEqual({
      target_id: 'tg-1', notice: undefined, symbol: undefined, kind: undefined, value: undefined,
      pct_from: null, fired_price: null, fired_at: null, expires_at: null,
    });
  });

  it('returns nothing for a payload that is not a list', () => {
    expect(coerceNotices(undefined)).toEqual([]);
    expect(coerceNotices({ target_id: 'tg-1' })).toEqual([]);
  });
});

describe('wakeNotices', () => {
  it('reads a notice list from the query metadata', () => {
    const notices = wakeNotices({ market_target: { notices: [{ target_id: 'a' }, { target_id: 'b' }] } });
    expect(notices.map((n) => n.target_id)).toEqual(['a', 'b']);
  });

  it('reads a single notice from the query metadata', () => {
    expect(wakeNotices({ market_target: { target_id: 'a', notice: 'expiring' } })).toMatchObject([
      { target_id: 'a', notice: 'expiring' },
    ]);
  });

  it('names nothing when the metadata does not', () => {
    expect(wakeNotices(undefined)).toEqual([]);
    expect(wakeNotices({ llm_model: 'm' })).toEqual([]);
  });
});

describe('marketAlertId', () => {
  it('is the same for the same notices, and differs by kind of notice', () => {
    const hit = [{ target_id: 'a', notice: 'hit' as const }];
    expect(marketAlertId(hit)).toBe(marketAlertId([{ target_id: 'a' }]));
    expect(marketAlertId(hit)).not.toBe(marketAlertId([{ target_id: 'a', notice: 'expiring' }]));
    expect(marketAlertId([{ target_id: 'a' }, { target_id: 'b' }])).toBe('market-alert-a:hit,b:hit');
  });
});

describe('resolveNotice', () => {
  it('fills what a notice left out from its target', () => {
    const resolved = resolveNotice(
      { target_id: 'tg-1' },
      [target({ status: 'fired', fired_price: 205.12, fired_at: '2030-01-01T15:05:00Z' })],
    );
    expect(resolved).toEqual({
      notice: 'hit', symbol: 'NVDA', kind: 'above', value: 205, pct_from: null,
      fired_price: 205.12, fired_at: '2030-01-01T15:05:00Z', expires_at: '2030-01-02T00:00:00Z',
    });
  });

  it("prefers the notice's own fields over the target's", () => {
    const resolved = resolveNotice(
      { target_id: 'tg-1', notice: 'hit', fired_price: 206 },
      [target({ status: 'fired', fired_price: 205.12 })],
    );
    expect(resolved?.fired_price).toBe(206);
  });

  it('reads a still-armed target named without a notice as about to expire', () => {
    expect(resolveNotice({ target_id: 'tg-1' }, [target()])?.notice).toBe('expiring');
  });

  it('is null when neither the notice nor a target says what was watched', () => {
    expect(resolveNotice({ target_id: 'gone' }, [target()])).toBeNull();
    expect(resolveNotice({ target_id: 'tg-1', symbol: 'NVDA' }, null)).toBeNull();
  });

  it('stands alone when the notice carries its own fields', () => {
    expect(resolveNotice({ target_id: 'x', symbol: 'TSLA', kind: 'pct_down', value: 3, pct_from: 'previous_close' }, null))
      .toMatchObject({ notice: 'hit', symbol: 'TSLA', kind: 'pct_down', value: 3, pct_from: 'previous_close' });
  });
});
