import React, { useState, useMemo } from 'react';
import { TrendingUp, Clock, Briefcase, Eye, Search, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { SwapCell } from '@/components/ui/crossfade';
import { useLastHeight } from '@/lib/lastShape';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useNow } from '@/hooks/useNow';
import { useLocale } from '@/hooks/useLocale';
import { relativeTime } from '@/lib/format';
import { isWithinDateRange, type NewsDateRange } from '../utils/newsItem';

interface NewsItem {
  id?: string | number;
  title: string;
  source?: string;
  publishedAt?: string | null;
  image?: string | null;
  favicon?: string | null;
  tickers?: string[];
  isHot?: boolean;
  articleUrl?: string | null;
}

type TabKey = 'market' | 'portfolio' | 'watchlist';

const TAB_KEYS: { key: TabKey; icon: React.ComponentType<{ size?: number }> }[] = [
  { key: 'market', icon: TrendingUp },
  { key: 'portfolio', icon: Briefcase },
  { key: 'watchlist', icon: Eye },
];

const DATE_RANGE_KEYS: NewsDateRange[] = ['all', '1h', '6h', '24h', '7d'];

interface NewsRowProps {
  item: NewsItem;
  onNewsClick?: (id: string | number, articleUrl?: string | null) => void;
}

function NewsRow({ item, onNewsClick }: NewsRowProps) {
  const locale = useLocale();
  const now = useNow();
  const time = relativeTime(item.publishedAt, locale, now);
  const sentiment = item.isHot ? 'positive' : 'neutral';
  const sentimentColor =
    sentiment === 'positive'
      ? 'var(--color-profit)'
      : 'var(--color-text-secondary)';
  const tickers = (item.tickers?.length ?? 0) > 0 ? item.tickers! : null;

  return (
    <div
      onClick={() => item.id != null && onNewsClick?.(item.id, item.articleUrl)}
      className="group flex items-center gap-3 sm:gap-4 px-2 py-2.5 sm:p-3 rounded-lg sm:rounded-xl border border-transparent transition-all cursor-pointer"
      style={{ backgroundColor: 'transparent' }}
      onMouseEnter={(e: React.MouseEvent<HTMLDivElement>) => {
        e.currentTarget.style.backgroundColor = 'var(--color-bg-hover)';
        e.currentTarget.style.borderColor = 'var(--color-border-muted)';
      }}
      onMouseLeave={(e: React.MouseEvent<HTMLDivElement>) => {
        e.currentTarget.style.backgroundColor = 'transparent';
        e.currentTarget.style.borderColor = 'transparent';
      }}
    >
      {/* Thumbnail — hidden on mobile */}
      {item.image && (
        <div className="relative h-16 w-24 shrink-0 overflow-hidden rounded-lg hidden sm:block">
          <img
            src={item.image}
            alt=""
            className="h-full w-full object-cover"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
          />
        </div>
      )}

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <span
            className="w-1.5 h-1.5 rounded-full shrink-0"
            style={{ backgroundColor: sentimentColor }}
          />
          {item.favicon && (
            <img
              src={item.favicon}
              alt=""
              className="w-4 h-4 rounded shrink-0"
              onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
            />
          )}
          {item.source && (
            <span
              className="text-[0.625rem] font-medium uppercase tracking-wide"
              style={{ color: 'var(--color-accent-light)' }}
            >
              {item.source}
            </span>
          )}
          {time && (
            <span
              className="text-xs flex items-center gap-1"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              <Clock size={10} /> {time}
            </span>
          )}
        </div>
        <h3
          className="text-sm font-medium truncate transition-colors"
          style={{ color: 'var(--color-text-primary)' }}
          title={item.title}
        >
          {item.title}
        </h3>
        {tickers && (
          <div className="flex items-center gap-1.5 mt-1">
            {tickers.slice(0, 4).map((t) => (
              <span
                key={t}
                className="text-[0.625rem] font-bold px-1.5 py-0.5 rounded"
                style={{
                  backgroundColor: 'var(--color-accent-soft)',
                  color: 'var(--color-accent-light)',
                }}
              >
                {t}
              </span>
            ))}
            {tickers.length > 4 && (
              <span className="text-[0.625rem]" style={{ color: 'var(--color-text-secondary)' }}>
                +{tickers.length - 4}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function SkeletonRows({ count = 6 }: { count?: number }) {
  return Array.from({ length: count }).map((_, idx) => (
    <div key={idx} className="flex items-center gap-4 p-3 animate-pulse">
      <div
        className="h-16 w-24 shrink-0 rounded-lg hidden sm:block"
        style={{ backgroundColor: 'var(--color-border-default)' }}
      />
      <div className="flex-1 min-w-0">
        <div className="h-3 rounded mb-2" style={{ backgroundColor: 'var(--color-border-default)', width: '40%' }} />
        <div className="h-4 rounded" style={{ backgroundColor: 'var(--color-border-default)', width: `${60 + (idx % 3) * 15}%` }} />
      </div>
    </div>
  ));
}

interface EmptyStateProps {
  tab: TabKey;
  hasFilters: boolean;
}

function EmptyState({ tab, hasFilters }: EmptyStateProps) {
  const { t } = useTranslation();

  if (hasFilters) {
    return (
      <div className="flex items-center justify-center py-16">
        <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          {t('dashboard.newsFeedCard.emptyFilters')}
        </p>
      </div>
    );
  }

  const messages: Record<TabKey, string> = {
    market: t('dashboard.newsFeedCard.emptyMarket'),
    portfolio: t('dashboard.newsFeedCard.emptyPortfolio'),
    watchlist: t('dashboard.newsFeedCard.emptyWatchlist'),
  };

  return (
    <div className="flex items-center justify-center py-16">
      <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        {messages[tab] || t('dashboard.newsFeedCard.emptyDefault')}
      </p>
    </div>
  );
}

interface NewsFeedCardProps {
  marketItems?: NewsItem[];
  marketLoading?: boolean;
  portfolioItems?: NewsItem[];
  portfolioLoading?: boolean;
  watchlistItems?: NewsItem[];
  watchlistLoading?: boolean;
  onNewsClick?: (id: string | number, articleUrl?: string | null) => void;
}

function NewsFeedCard({
  marketItems = [],
  marketLoading = false,
  portfolioItems = [],
  portfolioLoading = false,
  watchlistItems = [],
  watchlistLoading = false,
  onNewsClick,
}: NewsFeedCardProps) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<TabKey>('market');
  const [tickerFilter, setTickerFilter] = useState('');
  const [dateRange, setDateRange] = useState<NewsDateRange>('all');
  const now = useNow();

  const tabLabels: Record<TabKey, string> = {
    market: t('dashboard.newsFeedCard.tabMarket'),
    portfolio: t('dashboard.newsFeedCard.tabPortfolio'),
    watchlist: t('dashboard.newsFeedCard.tabWatchlist'),
  };

  const dateRangeLabels: Record<NewsDateRange, string> = {
    all: t('dashboard.newsFeedCard.rangeAll'),
    '1h': t('dashboard.newsFeedCard.range1h'),
    '6h': t('dashboard.newsFeedCard.range6h'),
    '24h': t('dashboard.newsFeedCard.range24h'),
    '7d': t('dashboard.newsFeedCard.range7d'),
  };

  const dataMap: Record<TabKey, { items: NewsItem[]; loading: boolean }> = {
    market: { items: marketItems, loading: marketLoading },
    portfolio: { items: portfolioItems, loading: portfolioLoading },
    watchlist: { items: watchlistItems, loading: watchlistLoading },
  };

  const { items, loading } = dataMap[activeTab];

  const hasFilters = tickerFilter.trim() !== '' || dateRange !== 'all';
  // Each tab's list height last time here, so its skeleton holds that room.
  // Not under a filter, which a visit does not open with.
  const { ref: listRef, height: lastListHeight } = useLastHeight(`dashboard:news.height.${activeTab}`, !hasFilters);

  const filteredItems = useMemo(() => {
    let result = items;

    // Ticker filter
    const query = tickerFilter.trim().toUpperCase();
    if (query) {
      result = result.filter((item) =>
        item.tickers?.some((t) => t.toUpperCase().includes(query))
      );
    }

    if (dateRange !== 'all') {
      result = result.filter((item) => isWithinDateRange(item.publishedAt, dateRange, now));
    }

    return result;
  }, [items, tickerFilter, dateRange, now]);

  // Collect unique tickers across current items for quick-pick
  const _availableTickers = useMemo(() => {
    const set = new Set<string>();
    for (const item of items) {
      if (item.tickers) item.tickers.forEach((t) => set.add(t));
    }
    return [...set].sort();
  }, [items]);

  const isMobile = useIsMobile();

  return (
    <div className={`dashboard-glass-card ${isMobile ? 'p-3' : 'p-6'}`}>
      {/* Header: tabs + filters on same row */}
      <div
        className={`flex items-center justify-between gap-3 ${isMobile ? 'pb-3 mb-3' : 'pb-4 mb-4'} border-b flex-wrap`}
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        {/* Tabs */}
        <div className="flex items-center gap-1 p-1 rounded-lg min-w-0 overflow-x-auto" style={{ backgroundColor: 'var(--color-bg-tag)' }}>
          {TAB_KEYS.map((tab) => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                onClick={() => {
                  setActiveTab(tab.key);
                  setTickerFilter('');
                  setDateRange('all');
                }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all"
                style={{
                  backgroundColor: isActive ? 'var(--color-bg-elevated)' : 'transparent',
                  color: isActive ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
                }}
              >
                <Icon size={13} />
                {tabLabels[tab.key]}
              </button>
            );
          })}
        </div>

        {/* Filters */}
        <div className="flex items-center gap-2">
          {/* Ticker search */}
          <div
            className="flex items-center gap-1.5 h-7 px-2 rounded-lg border"
            style={{
              backgroundColor: 'var(--color-bg-input, var(--color-bg-tag))',
              borderColor: 'var(--color-border-muted)',
              width: tickerFilter ? 160 : 130,
              transition: 'width var(--motion-quick) var(--ease-out)',
            }}
          >
            <Search size={12} style={{ color: 'var(--color-text-secondary)', flexShrink: 0 }} />
            <input
              type="text"
              placeholder={t('dashboard.newsFeedCard.tickerPlaceholder')}
              value={tickerFilter}
              onChange={(e) => setTickerFilter(e.target.value)}
              className="flex-1 text-[0.6875rem] bg-transparent border-none min-w-0"
              style={{ color: 'var(--color-text-primary)' }}
            />
            {tickerFilter && (
              <button
                onClick={() => setTickerFilter('')}
                className="shrink-0"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                <X size={11} />
              </button>
            )}
          </div>

          {/* Date range pills */}
          <div className="flex items-center gap-0.5 p-0.5 rounded-lg" style={{ backgroundColor: 'var(--color-bg-tag)' }}>
            {DATE_RANGE_KEYS.map((key) => {
              const isActive = dateRange === key;
              return (
                <button
                  key={key}
                  onClick={() => setDateRange(key)}
                  className="px-2 py-1 rounded-md text-[0.6875rem] font-medium transition-all"
                  style={{
                    backgroundColor: isActive ? 'var(--color-bg-elevated)' : 'transparent',
                    color: isActive ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
                  }}
                >
                  {dateRangeLabels[key]}
                </button>
              );
            })}
          </div>

          {/* Clear */}
          {hasFilters && (
            <button
              onClick={() => { setTickerFilter(''); setDateRange('all'); }}
              style={{ color: 'var(--color-text-secondary)' }}
            >
              <X size={14} />
            </button>
          )}
        </div>
      </div>

      {/* Tab content. A tab change dissolves in one grid cell: the old list
          leaves as the new one arrives, never through an empty card. A tab the
          reader picks rises; the skeleton giving way to its rows crossfades. */}
      <SwapCell
        as="div"
        className="items-start"
        riseKey={activeTab}
        swapKey={`${activeTab}:${loading ? 'loading' : 'ready'}`}
        layerClassName="flex flex-col gap-1"
      >
        {loading ? (
          <div className="flex flex-col gap-1 overflow-hidden" style={{ height: lastListHeight ?? undefined }}>
            <SkeletonRows />
          </div>
        ) : (
          <div ref={listRef} className="flex flex-col gap-1">
            {filteredItems.length === 0 ? (
              <EmptyState tab={activeTab} hasFilters={hasFilters} />
            ) : (
              filteredItems.map((item, idx) => (
                <NewsRow key={item.id || idx} item={item} onNewsClick={onNewsClick} />
              ))
            )}
          </div>
        )}
      </SwapCell>
    </div>
  );
}

export default NewsFeedCard;
