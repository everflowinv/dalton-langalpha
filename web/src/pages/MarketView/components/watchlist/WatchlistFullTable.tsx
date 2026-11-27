import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Check, ChevronDown, FolderOpen, Star, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocale } from '@/hooks/useLocale';
import { DASH } from '@/lib/format';
import { formatMoney, quoteCurrency } from '@/lib/bars';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  WATCHLIST_TIERS,
  type PersonalWatchlistCompany,
  type WatchlistColumnKey,
  type WatchlistMetricRow,
  type WatchlistTier,
} from '@/lib/dalton/watchlist';

export type WatchlistSortField = 'ticker' | WatchlistColumnKey;

export interface WatchlistFullTableProps {
  companies: readonly PersonalWatchlistCompany[];
  metrics: Record<string, WatchlistMetricRow>;
  metricsLoading: boolean;
  visibleColumns: readonly WatchlistColumnKey[];
  groupedByTier: boolean;
  holdingSymbols: ReadonlySet<string>;
  sortField?: WatchlistSortField | null;
  sortDir?: 'asc' | 'desc';
  onSortChange?: (field: WatchlistSortField | null, dir: 'asc' | 'desc') => void;
  initialScrollTop?: number;
  onScrollTopChange?: (scrollTop: number) => void;
  onSelectSymbol: (symbol: string) => void;
  onRequestTierChange: (company: PersonalWatchlistCompany, nextTier: WatchlistTier) => void;
  onRequestRemove: (company: PersonalWatchlistCompany) => void;
  onOpenWorkspace?: (workspaceId: string) => void;
}

const TIER_LABEL_KEY: Record<WatchlistTier, string> = {
  core: 'marketView:watchlist.tiers.core',
  watch: 'marketView:watchlist.tiers.watch',
  radar: 'marketView:watchlist.tiers.radar',
};

const COL_LABEL_KEY: Record<WatchlistColumnKey, string> = {
  tier: 'marketView:watchlist.columns.tier',
  price: 'marketView:watchlist.columns.price',
  chg_1d: 'marketView:watchlist.columns.chg_1d',
  chg_1w: 'marketView:watchlist.columns.chg_1w',
  chg_1m: 'marketView:watchlist.columns.chg_1m',
  chg_3m: 'marketView:watchlist.columns.chg_3m',
  chg_ytd: 'marketView:watchlist.columns.chg_ytd',
  chg_1y: 'marketView:watchlist.columns.chg_1y',
  range_52w: 'marketView:watchlist.columns.range_52w',
  rsi_14d: 'marketView:watchlist.columns.rsi_14d',
  rsi_30d: 'marketView:watchlist.columns.rsi_30d',
  market_cap: 'marketView:watchlist.columns.market_cap',
  next_earnings: 'marketView:watchlist.columns.next_earnings',
};

const TIER_ORDER: Record<WatchlistTier, number> = { core: 0, watch: 1, radar: 2 };

const MARKET_CAP_CURRENCY_GROUP_ORDER: Record<string, number> = {
  USD: 0,
  HKD: 1,
  CNY: 2,
  EUR: 3,
  GBP: 4,
  JPY: 5,
};

function resolveMarketCapCurrency(
  company: PersonalWatchlistCompany,
  metric: WatchlistMetricRow | undefined,
): string | null {
  return quoteCurrency(metric?.market_cap_currency ?? metric?.currency ?? null, company.ticker, 'equity');
}

function compareCurrencyGroup(curA: string | null, curB: string | null): number {
  if (curA === curB) return 0;
  if (curA == null) return 1;
  if (curB == null) return -1;
  const rankA = MARKET_CAP_CURRENCY_GROUP_ORDER[curA] ?? 999;
  const rankB = MARKET_CAP_CURRENCY_GROUP_ORDER[curB] ?? 999;
  if (rankA !== rankB) return rankA - rankB;
  return curA.localeCompare(curB);
}

function signedPctColor(val: number | null | undefined): string {
  if (val == null || val === 0) return 'var(--color-text-secondary)';
  return val > 0 ? 'var(--color-profit)' : 'var(--color-loss)';
}

function formatSignedPct(val: number | null | undefined): string {
  if (val == null) return DASH;
  return `${val > 0 ? '+' : ''}${val.toFixed(2)}%`;
}

export function WatchlistFullTable({
  companies,
  metrics,
  metricsLoading,
  visibleColumns,
  groupedByTier,
  holdingSymbols,
  sortField: controlledSortField,
  sortDir: controlledSortDir,
  onSortChange,
  initialScrollTop = 0,
  onScrollTopChange,
  onSelectSymbol,
  onRequestTierChange,
  onRequestRemove,
  onOpenWorkspace,
}: WatchlistFullTableProps): React.ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const [internalSortField, setInternalSortField] = useState<WatchlistSortField | null>(null);
  const [internalSortDir, setInternalSortDir] = useState<'asc' | 'desc'>('desc');
  const sortField = controlledSortField !== undefined ? controlledSortField : internalSortField;
  const sortDir = controlledSortDir !== undefined ? controlledSortDir : internalSortDir;
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const restoredScrollRef = useRef(false);

  useEffect(() => {
    if (restoredScrollRef.current) return;
    if (initialScrollTop > 0 && scrollContainerRef.current) {
      scrollContainerRef.current.scrollTop = initialScrollTop;
      restoredScrollRef.current = true;
    }
  }, [initialScrollTop, companies.length]);

  const toggleSort = (field: WatchlistSortField) => {
    let nextField: WatchlistSortField | null = field;
    let nextDir: 'asc' | 'desc';
    if (sortField === field) {
      nextDir = sortDir === 'desc' ? 'asc' : 'desc';
    } else {
      nextDir = field === 'ticker' || field === 'tier' || field === 'next_earnings' ? 'asc' : 'desc';
    }
    if (onSortChange) {
      onSortChange(nextField, nextDir);
    } else {
      setInternalSortField(nextField);
      setInternalSortDir(nextDir);
    }
  };

  const sortedCompanies = useMemo(() => {
    const list = [...companies];
    if (!sortField) return list;
    const dir = sortDir === 'asc' ? 1 : -1;
    const metricValue = (c: PersonalWatchlistCompany): number | string | null => {
      const m = metrics[c.ticker];
      switch (sortField) {
        case 'ticker':
          return c.ticker;
        case 'tier':
          return TIER_ORDER[c.tier] ?? 9;
        case 'price':
          return c.is_private ? null : (m?.price ?? null);
        case 'chg_1d':
          return m?.chg_1d ?? null;
        case 'chg_1w':
          return m?.chg_1w ?? null;
        case 'chg_1m':
          return m?.chg_1m ?? null;
        case 'chg_3m':
          return m?.chg_3m ?? null;
        case 'chg_ytd':
          return m?.chg_ytd ?? null;
        case 'chg_1y':
          return m?.chg_1y ?? null;
        case 'range_52w':
          return m?.position_52w ?? null;
        case 'rsi_14d':
          return m?.rsi_14d ?? null;
        case 'rsi_30d':
          return m?.rsi_30d ?? null;
        case 'market_cap':
          return m?.market_cap ?? null;
        case 'next_earnings':
          return m?.next_earnings ?? null;
      }
    };
    list.sort((a, b) => {
      if (sortField === 'market_cap') {
        const ma = metrics[a.ticker];
        const mb = metrics[b.ticker];
        const va = ma?.market_cap ?? null;
        const vb = mb?.market_cap ?? null;
        if (va == null && vb == null) return a.ticker.localeCompare(b.ticker);
        if (va == null) return 1;
        if (vb == null) return -1;
        // Group by quote currency (unknown currency last) so HKD/CNY/USD are never mixed as one unit
        const curDiff = compareCurrencyGroup(
          resolveMarketCapCurrency(a, ma),
          resolveMarketCapCurrency(b, mb),
        );
        if (curDiff !== 0) return curDiff;
        const capDiff = (va - vb) * dir;
        return capDiff !== 0 ? capDiff : a.ticker.localeCompare(b.ticker);
      }
      const va = metricValue(a);
      const vb = metricValue(b);
      if (va == null && vb == null) return a.ticker.localeCompare(b.ticker);
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'string' && typeof vb === 'string') {
        return va.localeCompare(vb) * dir;
      }
      return (Number(va) - Number(vb)) * dir;
    });
    return list;
  }, [companies, metrics, sortField, sortDir]);

  const sections = useMemo(() => {
    if (!groupedByTier) {
      return [{ tier: null as WatchlistTier | null, rows: sortedCompanies }];
    }
    return WATCHLIST_TIERS.map((tier) => ({
      tier,
      rows: sortedCompanies.filter((c) => c.tier === tier),
    })).filter((s) => s.rows.length > 0);
  }, [groupedByTier, sortedCompanies]);

  const renderMetricCell = (company: PersonalWatchlistCompany, col: WatchlistColumnKey) => {
    const m = metrics[company.ticker];
    const isUnlisted = Boolean(company.is_private || m?.is_private);

    if (col === 'tier') {
      return (
        <td key={col} className="px-2.5 py-2 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                data-testid={`watchlist-tier-badge-${company.ticker}`}
                aria-label={t('marketView:watchlist.changeTierAria', { symbol: company.ticker })}
                className="inline-flex items-center gap-1 text-xs rounded-full px-2 py-0.5 border font-medium cursor-pointer"
                style={{
                  backgroundColor: 'var(--color-bg-page)',
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-primary)',
                }}
              >
                <span>{t(TIER_LABEL_KEY[company.tier])}</span>
                <ChevronDown className="h-3 w-3 opacity-60 shrink-0" aria-hidden="true" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="start"
              className="min-w-[96px]"
              onClick={(e) => e.stopPropagation()}
            >
              <DropdownMenuRadioGroup
                value={company.tier}
                onValueChange={(value) => {
                  const nextTier = value as WatchlistTier;
                  if (nextTier !== company.tier) onRequestTierChange(company, nextTier);
                }}
              >
                {WATCHLIST_TIERS.map((tierOpt) => (
                  <DropdownMenuRadioItem
                    key={tierOpt}
                    value={tierOpt}
                    className="text-xs flex items-center justify-between gap-2"
                  >
                    <span>{t(TIER_LABEL_KEY[tierOpt])}</span>
                    {company.tier === tierOpt && <Check className="h-3 w-3 shrink-0" aria-hidden="true" />}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </td>
      );
    }

    if (isUnlisted) {
      return (
        <td
          key={col}
          className="px-2.5 py-2 text-right text-xs tabular-nums whitespace-nowrap"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {col === 'price' ? t('marketView:watchlist.unlisted') : DASH}
        </td>
      );
    }

    if (col === 'price') {
      const code = quoteCurrency(m?.currency ?? null, company.ticker, 'equity');
      return (
        <td
          key={col}
          className="px-2.5 py-2 text-right text-xs font-medium tabular-nums whitespace-nowrap"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {m?.price != null ? formatMoney(m.price, code, locale) : metricsLoading ? '…' : DASH}
        </td>
      );
    }

    if (
      col === 'chg_1d' ||
      col === 'chg_1w' ||
      col === 'chg_1m' ||
      col === 'chg_3m' ||
      col === 'chg_ytd' ||
      col === 'chg_1y'
    ) {
      const val = m?.[col];
      return (
        <td
          key={col}
          className="px-2.5 py-2 text-right text-xs tabular-nums whitespace-nowrap"
          style={{ color: signedPctColor(val) }}
        >
          {formatSignedPct(val)}
        </td>
      );
    }

    if (col === 'range_52w') {
      const pos = m?.position_52w;
      return (
        <td key={col} className="px-2.5 py-2 text-right text-xs tabular-nums whitespace-nowrap">
          {pos == null ? (
            <span style={{ color: 'var(--color-text-tertiary)' }}>{DASH}</span>
          ) : (
            <div className="inline-flex items-center gap-1.5 justify-end">
              <div
                className="w-14 h-1.5 rounded-full relative overflow-hidden"
                style={{ backgroundColor: 'var(--color-border-muted)' }}
                title={
                  m?.low_52w != null && m?.high_52w != null
                    ? `${m.low_52w.toFixed(2)} – ${m.high_52w.toFixed(2)}`
                    : undefined
                }
              >
                <span
                  className="absolute top-0 bottom-0 w-1.5 rounded-full"
                  style={{
                    left: `calc(${Math.max(0, Math.min(100, pos))}% - 3px)`,
                    backgroundColor: 'var(--color-accent-primary)',
                  }}
                />
              </div>
              <span style={{ color: 'var(--color-text-secondary)' }}>{pos.toFixed(0)}%</span>
            </div>
          )}
        </td>
      );
    }

    if (col === 'rsi_14d' || col === 'rsi_30d') {
      const val = m?.[col];
      const color =
        val == null
          ? 'var(--color-text-tertiary)'
          : val >= 70
            ? 'var(--color-loss)'
            : val <= 30
              ? 'var(--color-profit)'
              : 'var(--color-text-primary)';
      return (
        <td key={col} className="px-2.5 py-2 text-right text-xs tabular-nums whitespace-nowrap" style={{ color }}>
          {val == null ? DASH : val.toFixed(1)}
        </td>
      );
    }

    if (col === 'market_cap') {
      const val = m?.market_cap;
      const code = resolveMarketCapCurrency(company, m);
      return (
        <td
          key={col}
          className="px-2.5 py-2 text-right text-xs tabular-nums whitespace-nowrap"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {val != null ? formatMoney(val, code, locale, { compact: true }) : DASH}
        </td>
      );
    }

    // next_earnings
    return (
      <td
        key={col}
        className="px-2.5 py-2 text-right text-xs tabular-nums whitespace-nowrap"
        style={{ color: 'var(--color-text-secondary)' }}
      >
        {m?.next_earnings ?? DASH}
      </td>
    );
  };

  const renderSortIcon = (field: WatchlistSortField) => {
    if (sortField !== field) return null;
    return sortDir === 'asc' ? (
      <ArrowUp className="inline h-3 w-3 ml-0.5" />
    ) : (
      <ArrowDown className="inline h-3 w-3 ml-0.5" />
    );
  };

  return (
    <div
      ref={scrollContainerRef}
      onScroll={(e) => onScrollTopChange?.(e.currentTarget.scrollTop)}
      className="flex-1 min-h-0 overflow-auto"
    >
      <table className="w-full border-collapse text-left">
        <thead
          className="sticky top-0 z-10 border-b"
          style={{
            backgroundColor: 'var(--color-bg-card)',
            borderColor: 'var(--color-border-muted)',
          }}
        >
          <tr>
            <th
              className="sticky left-0 z-20 px-3 py-2 text-[11px] font-medium whitespace-nowrap cursor-pointer select-none"
              style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-text-tertiary)' }}
              onClick={() => toggleSort('ticker')}
            >
              {t('marketView:watchlist.columns.symbolName')}
              {renderSortIcon('ticker')}
            </th>
            {visibleColumns.map((col) => (
              <th
                key={col}
                className={`px-2.5 py-2 text-[11px] font-medium whitespace-nowrap cursor-pointer select-none ${
                  col === 'tier' ? 'text-left' : 'text-right'
                }`}
                style={{ color: 'var(--color-text-tertiary)' }}
                onClick={() => toggleSort(col)}
              >
                {t(COL_LABEL_KEY[col])}
                {renderSortIcon(col)}
              </th>
            ))}
            <th className="px-2 py-2 text-[11px] font-medium text-right" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('marketView:watchlist.columns.actions')}
            </th>
          </tr>
        </thead>
        <tbody>
          {sections.map((section) => (
            <React.Fragment key={section.tier ?? 'all'}>
              {section.tier && (
                <tr
                  style={{
                    backgroundColor: 'var(--color-bg-page)',
                    borderBottom: '1px solid var(--color-border-muted)',
                  }}
                >
                  <td
                    colSpan={visibleColumns.length + 2}
                    className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide"
                    style={{ color: 'var(--color-text-secondary)' }}
                  >
                    {t(TIER_LABEL_KEY[section.tier])} · {section.rows.length}
                  </td>
                </tr>
              )}
              {section.rows.map((company) => {
                const isHeld = holdingSymbols.has(company.ticker.toUpperCase());
                return (
                  <tr
                    key={company.ticker}
                    data-testid={`watchlist-row-${company.ticker}`}
                    onClick={() => onSelectSymbol(company.ticker)}
                    className="border-b cursor-pointer transition-colors hover:bg-(--color-bg-hover)"
                    style={{ borderColor: 'var(--color-border-muted)' }}
                  >
                    <td
                      className="sticky left-0 z-10 px-3 py-2 whitespace-nowrap"
                      style={{ backgroundColor: 'var(--color-bg-card)' }}
                    >
                      <div className="flex items-center gap-1.5 min-w-[160px]">
                        {company.is_pinned ? (
                          <Star className="h-3.5 w-3.5 shrink-0 fill-current" style={{ color: 'var(--color-warning)' }} />
                        ) : (
                          <span className="w-3.5 shrink-0" />
                        )}
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="font-mono text-xs font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                              {company.ticker}
                            </span>
                            {isHeld && (
                              <span
                                className="text-[10px] px-1.5 py-0.2 rounded font-medium"
                                style={{
                                  backgroundColor: 'var(--color-bg-elevated)',
                                  color: 'var(--color-accent-light)',
                                }}
                              >
                                {t('marketView:watchlist.holdingBadge')}
                              </span>
                            )}
                            {company.workspace_id ? (
                              <button
                                type="button"
                                title={t('marketView:watchlist.openWorkspace')}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  onOpenWorkspace?.(company.workspace_id!);
                                }}
                                className="p-0.5 rounded hover:bg-(--color-bg-hover)"
                                style={{ color: 'var(--color-text-tertiary)' }}
                              >
                                <FolderOpen className="h-3 w-3" />
                              </button>
                            ) : (
                              <span className="text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>
                                {t('marketView:watchlist.noWorkspaceBadge')}
                              </span>
                            )}
                          </div>
                          {company.name && company.name !== company.ticker && (
                            <div
                              className="text-[11px] truncate max-w-[180px]"
                              style={{ color: 'var(--color-text-tertiary)' }}
                            >
                              {company.name}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    {visibleColumns.map((col) => renderMetricCell(company, col))}
                    <td className="px-2 py-2 text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        aria-label={t('marketView:watchlist.removeAria', { symbol: company.ticker })}
                        onClick={() => onRequestRemove(company)}
                        className="p-1 rounded hover:bg-(--color-bg-hover)"
                        style={{ color: 'var(--color-text-tertiary)' }}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </React.Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
