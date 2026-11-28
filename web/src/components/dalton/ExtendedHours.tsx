import type React from 'react';
import { formatMoney, quoteCurrency } from '@/lib/bars';
import {
  deriveQuoteDisplay,
  roundQuote2,
  snapshotToStockPrice,
  type MarketStatusInput,
  type QuoteRow,
} from '@/lib/quotes';
import { TermChange } from '@/pages/ChatAgent/components/charts/TermCard';
import { cn } from '@/lib/utils';

export interface ExtendedHoursProps {
  label: string;
  price: string;
  pct: number;
  className?: string;
}

/**
 * Shared pre-market (`PM`) / after-hours (`AH`) move line: `AH $199.10 ▲ 0.34%`.
 * Reused by Dashboard watchlist/holdings widgets and MarketView watchlist table.
 */
export function ExtendedHours({
  label,
  price,
  pct,
  className,
}: ExtendedHoursProps): React.ReactElement {
  return (
    <span
      className={cn(
        'dash-ext inline-flex items-center gap-1 text-[11px] leading-tight whitespace-nowrap tabular-nums',
        className,
      )}
      style={{ color: 'var(--color-term-text-tertiary, var(--color-text-tertiary))' }}
    >
      <span className="term-label">{label}</span>
      {price}
      <TermChange value={pct} />
    </span>
  );
}

export interface FormattedExtendedHoursBadge {
  label: 'PM' | 'AH';
  price: string;
  pct: number;
}

export interface ResolvedExtendedHoursDisplay {
  session: 'regular' | 'pre' | 'post';
  mainPrice: number | null;
  mainChangePct: number | null;
  places: number | undefined;
  currency: string | null;
  ext: FormattedExtendedHoursBadge | null;
}

/**
 * Format a numeric quote price using the exact precision rules shared with Dashboard
 * (`WatchlistRowItem` / `PortfolioRowItem`).
 */
export function formatExtendedQuotePrice(
  value: number,
  currency: string | null,
  locale: string,
  places?: number,
): string {
  const num = places && places > 2 ? Number(value) : roundQuote2(Number(value));
  return formatMoney(num, currency, locale, { upTo: places });
}

/**
 * Derive headline price, 1D regular change %, and optional pre-/after-hours badge
 * from a shared `useQuotes` `QuoteRow` + `marketStatus`, using the single shared
 * `snapshotToStockPrice` + `deriveQuoteDisplay` rules.
 */
export function resolveExtendedHoursDisplay(
  quote: QuoteRow | undefined | null,
  marketStatus: MarketStatusInput | null | undefined,
  options: {
    symbol: string;
    fallbackCurrency?: string | null;
    locale: string;
  },
): ResolvedExtendedHoursDisplay {
  const { symbol, fallbackCurrency = null, locale } = options;
  if (!quote) {
    return {
      session: 'regular',
      mainPrice: null,
      mainChangePct: null,
      places: undefined,
      currency: quoteCurrency(fallbackCurrency, symbol, 'equity'),
      ext: null,
    };
  }

  const stockPrice = snapshotToStockPrice(symbol, quote);
  const currency = quoteCurrency(
    stockPrice.currency ?? fallbackCurrency,
    symbol,
    quote.asset_class ?? 'equity',
  );
  if (stockPrice.quoteAvailable === false) {
    return {
      session: 'regular',
      mainPrice: null,
      mainChangePct: null,
      places: stockPrice.places,
      currency,
      ext: null,
    };
  }

  const quoteDisplay = deriveQuoteDisplay(stockPrice, marketStatus ?? null);
  const extType = quoteDisplay.session === 'regular' ? null : quoteDisplay.session;
  const extLabel: 'PM' | 'AH' | null =
    extType === 'pre' ? 'PM' : extType === 'post' ? 'AH' : null;
  const ext =
    extLabel && quoteDisplay.extPrice != null && quoteDisplay.extChangePct != null
      ? {
          label: extLabel,
          price: formatExtendedQuotePrice(
            quoteDisplay.extPrice,
            currency,
            locale,
            stockPrice.places,
          ),
          pct: roundQuote2(Number(quoteDisplay.extChangePct)),
        }
      : null;

  return {
    session: quoteDisplay.session,
    mainPrice: quoteDisplay.mainPrice,
    mainChangePct: quoteDisplay.mainChangePct,
    places: stockPrice.places,
    currency,
    ext,
  };
}
