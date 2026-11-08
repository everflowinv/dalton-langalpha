/**
 * Pure helpers shared by the holdings/portfolio widgets and their snapshot
 * exporters. Lives in its own module (separate from `_holdingsPrimitives.tsx`)
 * so the component file stays component-only; Vite's fast-refresh requires
 * a clean components-only boundary to do precise HMR updates.
 *
 * Single source of truth for NAV math: the visual NAV card AND the snapshot
 * exporter both call `portfolioSummary()`, so the agent can never see
 * different numbers than the user.
 */

import { grouped2 } from '@/lib/format';
import { fixedUpTo } from '@/lib/bars/currencyDisplay';
import {
  deriveQuoteDisplay,
  formatSignedPct2,
  roundQuote2,
  type MarketStatusInput,
} from '@/lib/dalton/quotes/deriveQuoteDisplay'; // DALTON-HOOK(extended-hours-quotes): the module, not the quotes barrel, which pulls browser-only code into node tests
import type { PortfolioRow } from '../../hooks/usePortfolioData';
import {
  formatPortfolioMoneyCode,
  normalizePortfolioCurrency,
  summarizePortfolioByCurrency,
  type CurrencyPortfolioSummary,
} from '../../utils/portfolioSummary';

export type PortfolioSummary = CurrencyPortfolioSummary;

/** Places an average cost prints up to. */
const COST_PLACES = 10;
const SUB_CENT = new Intl.NumberFormat('en-US', { useGrouping: false, maximumSignificantDigits: 10 });

/** `n` up to `places` decimals, or to its significant digits when under a
 *  cent, so no nonzero cost or price reads as 0.00. */
function upToOrSignificant(n: number, places: number): string {
  return n === 0 || Math.abs(n) >= 0.01 ? fixedUpTo(n, places, 'en-US') : SUB_CENT.format(n);
}

export function portfolioSummary(rows: PortfolioRow[]): PortfolioSummary[] {
  return summarizePortfolioByCurrency(rows);
}

/** Render the portfolio summary as the leading markdown line for snapshot
 *  exporters. Multi-currency portfolios get one NAV line per currency. */
export function formatPortfolioNavMarkdownLine(summaries: PortfolioSummary[], locale: string): string {
  return summaries
    .filter((summary) => summary.totalValue !== 0)
    .map((summary) => {
      if (summary.totalCost <= 0) {
        return `**NAV (${summary.currency})** ${formatPortfolioMoneyCode(summary.totalValue, summary.currency)}`;
      }
      const sign = summary.totalPl >= 0 ? '+' : '-';
      const excluded = summary.uncosted.length
        ? `, excluding ${summary.uncosted.map((h) => `${h.symbol} (${formatPortfolioMoneyCode(h.value, summary.currency)})`).join(', ')}: no cost in ${summary.currency}`
        : '';
      return `**NAV (${summary.currency})** ${formatPortfolioMoneyCode(summary.totalValue, summary.currency)} (cost ${formatPortfolioMoneyCode(summary.totalCost, summary.currency)}, P/L ${sign}${formatPortfolioMoneyCode(Math.abs(summary.totalPl), summary.currency)} / ${sign}${grouped2(Math.abs(summary.totalPlPct), locale)}%${excluded})`;
    })
    .join('\n');
}

/** A holding as its snapshot carries it: what is held, at what cost, and its
 *  unrealized P/L since purchase. */
// DALTON-HOOK(extended-hours-quotes): Derive session and extended-hours quote fields via deriveQuoteDisplay while keeping holding cost, precision, and unrealized P/L.
export function portfolioRowData(row: PortfolioRow, marketStatus?: MarketStatusInput) {
  const hasQuote = row.quoteAvailable !== false;
  const d = hasQuote ? deriveQuoteDisplay(row, marketStatus ?? null) : null;
  const isExtended = d != null && d.session !== 'regular';
  const change = d?.mainChange != null ? roundQuote2(d.mainChange) : undefined;
  const changePercent = d?.mainChangePct != null ? roundQuote2(d.mainChangePct) : undefined;
  const extPrice = isExtended && d.extPrice != null ? roundQuote2(d.extPrice) : undefined;
  const extChange = isExtended && d.extChange != null ? roundQuote2(d.extChange) : undefined;
  const extPct = isExtended && d.extChangePct != null ? roundQuote2(d.extChangePct) : undefined;
  return {
    symbol: row.symbol,
    currency: normalizePortfolioCurrency(row.currency),
    shares: row.quantity ?? null,
    averageCost: row.average_cost ?? null,
    costCurrency: normalizePortfolioCurrency(row.costCurrency),
    price: hasQuote ? (d ? d.mainPrice : row.price) : null,
    places: row.places,
    marketValue: hasQuote ? row.marketValue ?? null : null,
    unrealizedPlPercent: hasQuote ? row.unrealizedPlPercent ?? null : null,
    ...(isExtended ? { session: d.session } : {}),
    ...(change !== undefined ? { change } : {}),
    ...(changePercent !== undefined ? { changePercent } : {}),
    ...(d?.session === 'pre' && extPrice !== undefined
      ? {
          preMarket: extPrice,
          ...(extChange !== undefined ? { preMarketChange: extChange } : {}),
          ...(extPct !== undefined ? { preMarketChangePercent: extPct } : {}),
        }
      : {}),
    ...(d?.session === 'post' && extPrice !== undefined
      ? {
          postMarket: extPrice,
          ...(extChange !== undefined ? { postMarketChange: extChange } : {}),
          ...(extPct !== undefined ? { postMarketChangePercent: extPct } : {}),
        }
      : {}),
  };
}

export type PortfolioRowData = ReturnType<typeof portfolioRowData>;

function costCell(row: PortfolioRowData): string {
  if (row.averageCost == null) return '';
  const cost = upToOrSignificant(row.averageCost, COST_PLACES);
  return row.costCurrency === row.currency ? cost : `${cost} ${row.costCurrency}`;
}

function plCell(pct: number | null): string {
  return pct == null ? '' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

function formatSignedDelta(n: number, places: number): string {
  const sign = n >= 0 ? '+' : '-';
  return `${sign}${upToOrSignificant(Math.abs(n), places)}`;
}

/** The holdings table for the agent. Its P/L column is since purchase, never
 *  the day's move, so it is not headed as a change. */
export function serializePortfolioRowsToMarkdown(rows: PortfolioRowData[]): string {
  if (!rows.length) return '_no holdings_';
  // DALTON-HOOK(extended-hours-quotes): Include session and pre/post-market columns when any holding carries extended-hours quote data.
  const includePre = rows.some((r) => r.preMarket !== undefined);
  const includePost = rows.some((r) => r.postMarket !== undefined);
  const includeSession =
    includePre ||
    includePost ||
    rows.some((r) => r.session !== undefined && r.session !== 'regular');
  const headers = [
    'symbol',
    'currency',
    ...(includeSession ? ['session'] : []),
    'shares',
    'avg cost',
    'price',
    ...(includePre ? ['pre-market', 'pre-market%'] : []),
    ...(includePost ? ['post-market', 'post-market%'] : []),
    'value',
    'unrealized P/L%',
  ];
  const lines = [
    `| ${headers.join(' | ')} |`,
    `| ${headers.map(() => '---').join(' | ')} |`,
  ];
  rows.forEach((row) => {
    // DALTON-HOOK(extended-hours-quotes): Format session and pre/post-market cells in the holdings table.
    const rowIsPost = row.session === 'post' || row.postMarket !== undefined;
    const rowIsPre = !rowIsPost && (row.session === 'pre' || row.preMarket !== undefined);
    const cells = [
      row.symbol,
      row.currency,
      ...(includeSession
        ? [rowIsPost ? 'regular close' : rowIsPre ? 'prev close' : 'regular']
        : []),
      row.shares != null ? String(row.shares) : '',
      costCell(row),
      row.price != null ? upToOrSignificant(row.price, row.places) : '',
      ...(includePre
        ? [
            row.preMarket !== undefined ? upToOrSignificant(row.preMarket, row.places) : '',
            row.preMarketChangePercent !== undefined ? formatSignedPct2(row.preMarketChangePercent) : '',
          ]
        : []),
      ...(includePost
        ? [
            row.postMarket !== undefined ? upToOrSignificant(row.postMarket, row.places) : '',
            row.postMarketChangePercent !== undefined ? formatSignedPct2(row.postMarketChangePercent) : '',
          ]
        : []),
      row.marketValue != null ? row.marketValue.toFixed(2) : '',
      plCell(row.unrealizedPlPercent),
    ];
    lines.push(`| ${cells.join(' | ')} |`);
  });
  return lines.join('\n');
}

/** One holding for a row attach, in the table's terms. */
export function serializePortfolioRowToMarkdown(
  holding: PortfolioRow,
  marketStatus?: MarketStatusInput,
): string {
  const row = portfolioRowData(holding, marketStatus);
  const lines = [`**${row.symbol}**`];
  if (row.shares != null) lines.push(`shares ${row.shares}`);
  if (row.averageCost != null) lines.push(`avg cost ${row.costCurrency} ${upToOrSignificant(row.averageCost, COST_PLACES)}`);
  // DALTON-HOOK(extended-hours-quotes): Include session anchor and pre/post-market quote lines when in extended hours.
  const isPost = row.session === 'post' || row.postMarket !== undefined;
  const isPre = !isPost && (row.session === 'pre' || row.preMarket !== undefined);
  if (row.price != null) {
    const amt = `${row.currency} ${upToOrSignificant(row.price, row.places)}`;
    if (isPost) {
      const move =
        row.change !== undefined && row.changePercent !== undefined
          ? ` ${formatSignedDelta(row.change, row.places)} (${formatSignedPct2(row.changePercent)}) vs prev close`
          : row.changePercent !== undefined
            ? ` (${formatSignedPct2(row.changePercent)}) vs prev close`
            : '';
      lines.push(`regular close ${amt}${move}`);
    } else if (isPre) {
      const move =
        row.change !== undefined && row.changePercent !== undefined
          ? ` ${formatSignedDelta(row.change, row.places)} (${formatSignedPct2(row.changePercent)} prior session)`
          : row.changePercent !== undefined
            ? ` (${formatSignedPct2(row.changePercent)} prior session)`
            : '';
      lines.push(`prev close ${amt}${move}`);
    } else {
      lines.push(`price ${amt}`);
    }
  }
  if (row.preMarket !== undefined) {
    const extAmt = `${row.currency} ${upToOrSignificant(row.preMarket, row.places)}`;
    const extMove =
      row.preMarketChange !== undefined && row.preMarketChangePercent !== undefined
        ? ` ${formatSignedDelta(row.preMarketChange, row.places)} (${formatSignedPct2(row.preMarketChangePercent)} vs prev close)`
        : row.preMarketChangePercent !== undefined
          ? ` (${formatSignedPct2(row.preMarketChangePercent)} vs prev close)`
          : '';
    lines.push(`pre-market ${extAmt}${extMove}`);
  }
  if (row.postMarket !== undefined) {
    const extAmt = `${row.currency} ${upToOrSignificant(row.postMarket, row.places)}`;
    const extMove =
      row.postMarketChange !== undefined && row.postMarketChangePercent !== undefined
        ? ` ${formatSignedDelta(row.postMarketChange, row.places)} (${formatSignedPct2(row.postMarketChangePercent)} vs regular close)`
        : row.postMarketChangePercent !== undefined
          ? ` (${formatSignedPct2(row.postMarketChangePercent)} vs regular close)`
          : '';
    lines.push(`post-market ${extAmt}${extMove}`);
  }
  if (row.marketValue != null) lines.push(`value ${row.currency} ${row.marketValue.toFixed(2)}`);
  if (row.unrealizedPlPercent != null) lines.push(`unrealized P/L ${plCell(row.unrealizedPlPercent)}`);
  return lines.join('\n');
}
