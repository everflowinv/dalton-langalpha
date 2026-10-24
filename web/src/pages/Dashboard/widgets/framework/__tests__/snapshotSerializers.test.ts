// @vitest-environment node
import { describe, expect, it } from 'vitest';
import {
  serializeInsightDayToMarkdown,
  serializeInsightToMarkdown,
  serializeNewsItemToMarkdown,
  serializeNewsItemsToMarkdown,
  serializeOhlcvToMarkdown,
  serializeQuoteRowToMarkdown,
  serializeQuoteRowsToMarkdown,
  serializeRowsToMarkdown,
  summarizeOhlcv,
  wrapWidgetContext,
} from '../snapshotSerializers';

describe('wrapWidgetContext', () => {
  it('emits a single attribute and a body', () => {
    const out = wrapWidgetContext('markets.chart', { symbol: 'NVDA' }, 'body');
    expect(out).toBe("<widget-context type='markets.chart' symbol='NVDA'>\nbody\n</widget-context>");
  });

  it('skips empty / undefined attrs', () => {
    const out = wrapWidgetContext('x', { a: 'a', b: undefined, c: '', d: null as unknown as string }, 'body');
    expect(out).toBe("<widget-context type='x' a='a'>\nbody\n</widget-context>");
  });

  it('escapes single quotes in attribute values', () => {
    const out = wrapWidgetContext('x', { label: "it's" }, 'body');
    expect(out).toContain("label='it&#39;s'");
  });

  it('omits attribute clause when no attrs', () => {
    const out = wrapWidgetContext('x', {}, 'body');
    expect(out).toBe("<widget-context type='x'>\nbody\n</widget-context>");
  });
});

describe('serializeOhlcvToMarkdown', () => {
  it('emits a markdown table for short series', () => {
    const out = serializeOhlcvToMarkdown([
      { time: '2026-04-25', open: 100, high: 105, low: 99, close: 104, volume: 1000 },
      { time: '2026-04-26', open: 104, high: 110, low: 103, close: 109, volume: 2000 },
    ]);
    expect(out).toContain('| time | open | high | low | close | volume |');
    expect(out).toContain('| 2026-04-25 | 100.00 | 105.00 | 99.00 | 104.00 | 1000 |');
  });

  it('truncates long series with head/tail markers', () => {
    const bars = Array.from({ length: 100 }, (_, i) => ({
      time: `2026-${String(i).padStart(2, '0')}`,
      open: 100,
      high: 101,
      low: 99,
      close: 100,
    }));
    const out = serializeOhlcvToMarkdown(bars, { headRows: 3, tailRows: 3 });
    expect(out).toContain('_... 94 bars omitted ..._');
    expect(out.split('\n').filter((l) => l.startsWith('| 2026')).length).toBe(6);
  });

  it('handles empty series', () => {
    expect(serializeOhlcvToMarkdown([])).toBe('_no bars_');
  });

  it('omits the volume column when no bars carry it', () => {
    const out = serializeOhlcvToMarkdown([
      { time: 't1', open: 1, high: 2, low: 1, close: 2 },
    ]);
    expect(out).not.toContain('volume');
  });
});

describe('summarizeOhlcv', () => {
  it('reports last close, %, and range', () => {
    const out = summarizeOhlcv([
      { time: 't1', open: 100, high: 105, low: 99, close: 100 },
      { time: 't2', open: 100, high: 110, low: 99, close: 110 },
    ]);
    expect(out).toContain('last close 110.00');
    expect(out).toContain('+10.00%');
    expect(out).toContain('99.00–110.00');
  });

  it('handles empty', () => {
    expect(summarizeOhlcv([])).toBe('');
  });
});

describe('news serializers', () => {
  it('renders one item with meta and url', () => {
    const out = serializeNewsItemToMarkdown({
      title: 'NVDA earnings beat',
      source: 'Reuters',
      publishedAt: '2026-04-26',
      url: 'https://x.test',
      tickers: ['NVDA', 'AMD'],
      summary: 'Earnings up.',
    });
    expect(out).toContain('**NVDA earnings beat**');
    expect(out).toContain('Reuters · 2026-04-26 · NVDA, AMD');
    expect(out).toContain('URL: https://x.test');
    expect(out).toContain('Earnings up.');
  });

  it('renders a list as markdown table', () => {
    const out = serializeNewsItemsToMarkdown([
      { title: 'A', source: 'r', publishedAt: 't1' },
      { title: 'B | with pipe', source: 'r', publishedAt: 't2' },
    ]);
    expect(out).toContain('| 1 | A |');
    expect(out).toContain('B \\| with pipe');
  });

  it('handles empty news', () => {
    expect(serializeNewsItemsToMarkdown([])).toBe('_no headlines_');
  });
});

describe('quote row serializers', () => {
  it('renders one row with bits', () => {
    const out = serializeQuoteRowToMarkdown({
      symbol: 'AAPL',
      price: 182.31,
      change: 0.76,
      changePercent: 0.42,
      preMarket: 182.1,
      marketValue: 1234.5,
      shares: 10,
      volume: 38000000,
    });
    expect(out).toContain('**AAPL**');
    expect(out).toContain('$182.31');
    expect(out).toContain('+0.76 (+0.42%)');
    expect(out).toContain('pre-market $182.10');
    expect(out).toContain('shares 10');
    expect(out).toContain('mkt val $1234.50');
    expect(out).toContain('vol 38000000');
  });

  it('renders rows as a markdown table', () => {
    const out = serializeQuoteRowsToMarkdown([
      { symbol: 'AAPL', price: 1, change: 0, changePercent: 0 },
      { symbol: 'MSFT', price: 2, change: 0, changePercent: 0, volume: 100 },
    ]);
    expect(out).toContain('| symbol | price | change | change% | volume |');
  });

  it('skips volume column when no row has volume', () => {
    const out = serializeQuoteRowsToMarkdown([{ symbol: 'A', price: 1, change: 0, changePercent: 0 }]);
    expect(out).not.toContain('volume');
  });

  it('handles empty rows', () => {
    expect(serializeQuoteRowsToMarkdown([])).toBe('_no symbols_');
  });

  // DALTON-HOOK(extended-hours-quotes): Verify explicit price identity labels in pre-market and after-hours markdown.
  it('labels regular close vs prev close and post-market vs regular close in after-hours', () => {
    const row = {
      symbol: 'GOOG',
      session: 'post' as const,
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      postMarket: 318.1,
      postMarketChange: 0.82,
      postMarketChangePercent: 0.26,
      currency: 'USD',
    };
    const out = serializeQuoteRowToMarkdown(row);
    expect(out).toBe(
      [
        '**GOOG**',
        'regular close USD 317.28 -3.48 (-1.08%) vs prev close',
        'post-market USD 318.10 +0.82 (+0.26% vs regular close)',
      ].join('\n'),
    );
    const table = serializeQuoteRowsToMarkdown([row]);
    expect(table).toContain(
      '| symbol | currency | session | price | change | change% | post-market | post-market% |',
    );
    expect(table).toContain('| GOOG | USD | regular close | 317.28 | -3.48 | -1.08% | 318.10 | +0.26% |');
    expect(table).toContain(
      '_Basis: change% vs prev close (for prev close rows, prior session move); post-market% vs regular close._',
    );
  });

  // DALTON-HOOK(extended-hours-quotes): Verify prev close (with prior session basis) and pre-market vs prev close labels in pre-market.
  it('labels prev close and pre-market vs prev close in pre-market', () => {
    const row = {
      symbol: 'GOOG',
      session: 'pre' as const,
      price: 320.76,
      change: -3.46,
      changePercent: -1.07,
      preMarket: 322.26,
      preMarketChange: 1.5,
      preMarketChangePercent: 0.47,
      currency: 'USD',
    };
    const out = serializeQuoteRowToMarkdown(row);
    expect(out).toBe(
      [
        '**GOOG**',
        'prev close USD 320.76 -3.46 (-1.07% prior session)',
        'pre-market USD 322.26 +1.50 (+0.47% vs prev close)',
      ].join('\n'),
    );
    const table = serializeQuoteRowsToMarkdown([row]);
    expect(table).toContain(
      '| symbol | currency | session | price | change | change% | pre-market | pre-market% |',
    );
    expect(table).toContain('| GOOG | USD | prev close | 320.76 | -3.46 | -1.07% | 322.26 | +0.47% |');
    expect(table).toContain(
      '_Basis: change% vs prev close (for prev close rows, prior session move); pre-market% vs prev close._',
    );
  });

  // DALTON-HOOK(extended-hours-quotes): Verify mixed-session table renders session labels and basis note clearly.
  it('renders mixed-session rows with session labels and basis note', () => {
    const table = serializeQuoteRowsToMarkdown([
      {
        symbol: 'GOOG',
        session: 'pre',
        price: 320.76,
        change: -3.46,
        changePercent: -1.07,
        preMarket: 322.26,
        preMarketChangePercent: 0.47,
        currency: 'USD',
      },
      {
        symbol: '0700.HK',
        session: 'regular',
        price: 400.0,
        change: 5.0,
        changePercent: 1.27,
        currency: 'HKD',
      },
      {
        symbol: 'AAPL',
        session: 'post',
        price: 180.0,
        change: -1.0,
        changePercent: -0.55,
        postMarket: 181.8,
        postMarketChangePercent: 1.0,
        currency: 'USD',
      },
    ]);
    expect(table).toContain('| GOOG | USD | prev close | 320.76 | -3.46 | -1.07% | 322.26 | +0.47% |  |  |');
    expect(table).toContain('| 0700.HK | HKD | regular | 400.00 | 5.00 | +1.27% |  |  |  |  |');
    expect(table).toContain('| AAPL | USD | regular close | 180.00 | -1.00 | -0.55% |  |  | 181.80 | +1.00% |');
    expect(table).toContain(
      '_Basis: change% vs prev close (for prev close rows, prior session move); pre-market% vs prev close; post-market% vs regular close._',
    );
  });

  // DALTON-HOOK(extended-hours-quotes): Verify shared formatSignedPct2 / roundQuote2 (thousand separators, non-finite values) and separate holding unrealized P/L.
  it('formats thousand-grouped and non-finite percentages via shared deriveQuoteDisplay helpers and separates holding unrealized P/L', () => {
    const out = serializeQuoteRowToMarkdown({
      symbol: 'GOOG',
      session: 'post',
      price: 317.28,
      change: -3.48,
      changePercent: -1.0849,
      postMarket: 318.1,
      postMarketChange: 0.82,
      postMarketChangePercent: 1234.565,
      shares: 10,
      marketValue: 3172.8,
      unrealizedPlPercent: 5.76,
      currency: 'USD',
    });
    expect(out).toBe(
      [
        '**GOOG**',
        'regular close USD 317.28 -3.48 (-1.08%) vs prev close',
        'post-market USD 318.10 +0.82 (+1,234.57% vs regular close)',
        'shares 10',
        'mkt val USD 3172.80',
        '持仓浮盈 +5.76%',
      ].join('\n'),
    );

    const tableOut = serializeQuoteRowsToMarkdown([
      {
        symbol: 'GOOG',
        session: 'post',
        price: 317.28,
        change: -3.48,
        changePercent: 1234.565,
        postMarket: 318.1,
        postMarketChangePercent: 1234.565,
        unrealizedPlPercent: 5.76,
        currency: 'USD',
      },
    ]);
    expect(tableOut).toContain(
      '| GOOG | USD | regular close | 317.28 | -3.48 | +1,234.57% | 318.10 | +1,234.57% | +5.76% |',
    );

    const pctOnlyHoldingOut = serializeQuoteRowToMarkdown({
      symbol: 'GOOG',
      session: 'post',
      price: 317.28,
      changePercent: -1.0849,
      shares: 10,
      marketValue: 3172.8,
      unrealizedPlPercent: 5.76,
      currency: 'USD',
    });
    expect(pctOnlyHoldingOut).toBe(
      [
        '**GOOG**',
        'regular close USD 317.28 (-1.08%) vs prev close',
        'shares 10',
        'mkt val USD 3172.80',
        '持仓浮盈 +5.76%',
      ].join('\n'),
    );

    const nonFiniteOut = serializeQuoteRowToMarkdown({
      symbol: 'NAN',
      price: Number.NaN,
      change: Number.NaN,
      changePercent: Number.NaN,
      currency: 'USD',
    });
    expect(nonFiniteOut).toBe(['**NAN**', 'USD 0.00 +0.00 (+0.00%)'].join('\n'));
  });
});

describe('serializeRowsToMarkdown (generic)', () => {
  it('renders generic rows', () => {
    const out = serializeRowsToMarkdown(
      [
        { id: 1, name: 'a' },
        { id: 2, name: 'b' },
      ],
      [
        { key: 'id', label: 'id' },
        { key: 'name', label: 'name' },
      ],
    );
    expect(out).toContain('| id | name |');
    expect(out).toContain('| 1 | a |');
  });

  it('uses formatter when provided', () => {
    const out = serializeRowsToMarkdown(
      [{ price: 1234.5 }],
      [{ key: 'price', label: 'price', format: (v) => `$${(v as number).toFixed(2)}` }],
    );
    expect(out).toContain('$1234.50');
  });
});

describe('serializeInsightToMarkdown', () => {
  it('renders headline, type, summary, topics, content, and sources', () => {
    const out = serializeInsightToMarkdown({
      market_insight_id: 'i1',
      type: 'pre_market',
      headline: 'NVDA leads pre-market',
      summary: 'Chips rally on AI demand.',
      completed_at: '2026-04-27T12:00:00Z',
      topics: [{ text: 'AI', trend: 'up' }, { text: 'Chips' }],
      content: [
        { title: 'Article 1', body: 'Body 1', url: 'https://e.com/1' },
        { title: 'Article 2', body: 'Body 2' },
      ],
      sources: [{ url: 'https://e.com/x', title: 'Example' }],
    });
    expect(out).toContain('## NVDA leads pre-market');
    expect(out).toContain('type: pre_market');
    expect(out).toContain('completed: 2026-04-27T12:00:00Z');
    expect(out).toContain('Chips rally on AI demand.');
    expect(out).toContain('**Topics:** AI (up), Chips');
    expect(out).toContain('1. **Article 1** — https://e.com/1');
    expect(out).toContain('   Body 1');
    expect(out).toContain('2. **Article 2**');
    expect(out).toContain('- Example — https://e.com/x');
  });

  it('uses custom heading when provided', () => {
    const out = serializeInsightToMarkdown(
      { market_insight_id: 'i1', type: 'market_update', headline: 'X' },
      { heading: '## X (latest)' },
    );
    expect(out.startsWith('## X (latest)')).toBe(true);
  });
});

describe('serializeInsightDayToMarkdown', () => {
  it('marks the first item as latest and joins with separators', () => {
    const out = serializeInsightDayToMarkdown([
      { market_insight_id: 'a', type: 'pre_market', headline: 'First' },
      { market_insight_id: 'b', type: 'personalized', headline: 'Mine', summary: 'For me' },
    ]);
    expect(out).toContain('## First (latest)');
    expect(out).toContain('## Mine');
    expect(out).toContain('type: personalized');
    expect(out).toContain('---');
    expect(out).toContain('2 briefs on this date');
  });

  it('returns _no briefs_ when empty', () => {
    expect(serializeInsightDayToMarkdown([])).toBe('_no briefs_');
  });
});

