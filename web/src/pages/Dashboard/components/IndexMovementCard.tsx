import React, { useState, useCallback } from 'react';
import { useNavigate } from 'react-router';
import { LineChart, Line, YAxis, Tooltip } from 'recharts';
import { motion, AnimatePresence, type PanInfo } from '@/lib/framer';
import { SPRING_DRAG } from '@/lib/motion';
import { SwapCell } from '@/components/ui/crossfade';
import { SkeletonBar } from '@/components/ui/skeleton';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useTranslation } from 'react-i18next';
import { useLocale } from '@/hooks/useLocale';
import { useNow } from '@/hooks/useNow';
import { grouped2 } from '@/lib/format';
import { cn, utcMsToETDate } from '@/lib/utils';
import type { IndexData } from '@/types/market';


interface SparklineDataPoint {
  time?: string;
  val: number;
  i: number;
}

interface IndexCardProps {
  index: IndexData;
}

interface IndexMovementCardProps {
  indices?: IndexData[];
  loading?: boolean;
  /**
   * When true, forces the mobile iOS-style swipeable stack rendering even on
   * desktop viewports. Used by the widget grid when the cell is too narrow to
   * fit the 5-column tile layout comfortably.
   */
  forceMobile?: boolean;
  /** One row of equal cards whatever the viewport: the widget's cell is one
   *  row tall and as wide as its own grid span, not the page. */
  singleRow?: boolean;
}

/* ── Shared card content (no animation wrapper) ── */

// Side by side from 14rem of header width (a card of about 258px), where the
// widest quote block still leaves room for the longest single word of a name.
const INDEX_HEADER_ROW =
  'flex flex-col gap-1 @min-[14rem]:flex-row @min-[14rem]:items-start @min-[14rem]:justify-between @min-[14rem]:gap-3';
const INDEX_HEADER_QUOTE = 'shrink-0 @min-[14rem]:text-right';

function IndexCardContent({ index }: { index: IndexData }) {
  const { t } = useTranslation();
  const locale = useLocale();
  const hasQuote = index.quoteAvailable !== false;
  const pos = index.isPositive;
  const ch = Number(index.change);
  const pct = Number(index.changePercent);
  const changeStr = grouped2(ch, locale);
  const pctStr = '(' + (pos ? '+' : '') + grouped2(pct, locale) + '%)';
  const chartData: SparklineDataPoint[] = (index.sparklineData || []).map((pt, i) =>
    typeof pt === 'object' ? { ...pt, i } : { val: pt as unknown as number, i },
  );

  // Show the date of the session the data belongs to (e.g. the last trading day
  // when the market is closed/pre-open), falling back to today (ET) if unknown
  // or malformed. ET — not browser-local — so the fallback matches the
  // ET-derived asOfDate on sibling cards for users outside US timezones. Only
  // a card on the fallback reads the clock, so only it ticks.
  const hasAsOfDate = /^\d{4}-\d{2}-\d{2}$/.test(index.asOfDate ?? '');
  const now = useNow(60_000, !hasAsOfDate);
  const dateStr = (() => {
    const iso = hasAsOfDate
      ? (index.asOfDate as string)
      : utcMsToETDate(now);
    const [, mo, d] = iso.split('-');
    return `${Number(mo)}/${Number(d)}`;
  })();

  return (
    <>
      {/* Name, date and symbol beside the price and change, or above them on
          a card too narrow to hold both sides of the row. */}
      <div className="@container p-4 pb-0">
        <div className={INDEX_HEADER_ROW}>
          <div className="min-w-0">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <h3
                className="text-base font-bold tracking-tight"
                style={{ color: 'var(--color-text-primary)' }}
              >
                {index.name}
              </h3>
              <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {dateStr}
              </span>
            </div>
            <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>
              ^{index.symbol}
            </div>
          </div>
          <div className={INDEX_HEADER_QUOTE}>
            <div
              className="text-lg font-bold tracking-tight dashboard-mono"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {hasQuote ? grouped2(Number(index.price), locale) : 'N/A'}
            </div>
            <div
              className="text-xs dashboard-mono"
              style={{
                color: hasQuote
                  ? pos ? 'var(--color-profit)' : 'var(--color-loss)'
                  : 'var(--color-text-secondary)',
              }}
            >
              {hasQuote ? `${changeStr} ${pctStr}` : 'N/A'}
            </div>
          </div>
        </div>
      </div>

      {/* Sparkline chart */}
      <div className="mt-2 px-1 pb-2" style={{ height: 100 }}>
        {chartData.length > 1 ? (
          <LineChart responsive width="100%" height={100} data={chartData}>
            <Line
              type="monotone"
              dataKey="val"
              stroke={
                hasQuote
                  ? pos ? 'var(--color-profit)' : 'var(--color-loss)'
                  : 'var(--color-text-secondary)'
              }
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
            <Tooltip
              content={({ active, payload }) => {
                if (!active || !payload?.[0]) return null;
                const d = payload[0].payload;
                return (
                  <div
                    className="rounded-lg px-2.5 py-1.5 text-xs shadow-lg border"
                    style={{
                      backgroundColor: 'var(--color-bg-card)',
                      borderColor: 'var(--color-border-muted)',
                      color: 'var(--color-text-primary)',
                    }}
                  >
                    {d.time && (
                      <div style={{ color: 'var(--color-text-secondary)' }}>{d.time}</div>
                    )}
                    <div className="font-semibold dashboard-mono">
                      {grouped2(Number(d.val), locale)}
                    </div>
                  </div>
                );
              }}
              cursor={{ stroke: 'var(--color-border-default)', strokeWidth: 1 }}
            />
            <YAxis domain={['dataMin', 'dataMax']} hide />
          </LineChart>
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              {t('dashboard.widgets.marketsOverview.noChartData')}
            </span>
          </div>
        )}
      </div>
    </>
  );
}

/* ── Desktop card. It has no entrance of its own: the grid it sits in crossfades
   in from the skeleton, so the five cards settle as one. ── */

function IndexCard({ index }: IndexCardProps) {
  const navigate = useNavigate();

  return (
    <div
      className="overflow-hidden rounded-2xl border transition-colors group flex flex-col cursor-pointer"
      style={{
        borderColor: 'var(--color-border-muted)',
        backgroundColor: 'var(--color-bg-card)',
      }}
      onClick={() => navigate(`/market?symbol=^${index.symbol}`)}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--color-border-default)')}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = 'var(--color-border-muted)')}
    >
      <IndexCardContent index={index} />
    </div>
  );
}

/* ── Mobile: iOS Smart Stack-style swipeable widget ── */

const swipeVariants = {
  enter: (dir: number) => ({
    x: dir > 0 ? '80%' : '-80%',
    opacity: 0,
    scale: 0.92,
  }),
  center: {
    x: 0,
    opacity: 1,
    scale: 1,
  },
  exit: (dir: number) => ({
    x: dir > 0 ? '-80%' : '80%',
    opacity: 0,
    scale: 0.92,
  }),
};

function IndexStackWidget({ indices }: { indices: IndexData[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [current, setCurrent] = useState(0);
  const [direction, setDirection] = useState(0);

  const paginate = useCallback(
    (dir: number) => {
      setCurrent((prev) => {
        const next = prev + dir;
        if (next < 0 || next >= indices.length) return prev;
        setDirection(dir);
        return next;
      });
    },
    [indices.length],
  );

  const goTo = useCallback(
    (i: number) => {
      setCurrent((prev) => {
        if (i === prev) return prev;
        setDirection(i > prev ? 1 : -1);
        return i;
      });
    },
    [],
  );

  const handleDragEnd = useCallback(
    (_: unknown, { offset, velocity }: PanInfo) => {
      if (offset.x < -40 || velocity.x < -300) paginate(1);
      else if (offset.x > 40 || velocity.x > 300) paginate(-1);
    },
    [paginate],
  );

  const index = indices[current];

  return (
    <div>
      {/* Card stack container */}
      <div className="relative overflow-hidden rounded-2xl" style={{ touchAction: 'pan-y' }}>
        {/* Stacked depth cards behind active card */}
        {indices.length > 1 && (
          <>
            {current < indices.length - 2 && (
              <div
                className="absolute inset-x-3 bottom-0 rounded-2xl border"
                style={{
                  borderColor: 'var(--color-border-muted)',
                  backgroundColor: 'var(--color-bg-card)',
                  opacity: 0.25,
                  height: 'calc(100% - 10px)',
                  top: 10,
                }}
              />
            )}
            {current < indices.length - 1 && (
              <div
                className="absolute inset-x-1.5 bottom-0 rounded-2xl border"
                style={{
                  borderColor: 'var(--color-border-muted)',
                  backgroundColor: 'var(--color-bg-card)',
                  opacity: 0.45,
                  height: 'calc(100% - 5px)',
                  top: 5,
                }}
              />
            )}
          </>
        )}

        {/* Active card */}
        <AnimatePresence initial={false} custom={direction} mode="popLayout">
          <motion.div
            key={current}
            custom={direction}
            variants={swipeVariants}
            initial="enter"
            animate="center"
            exit="exit"
            transition={SPRING_DRAG}
            drag="x"
            dragConstraints={{ left: 0, right: 0 }}
            dragElastic={0.12}
            onDragEnd={handleDragEnd}
            className="relative overflow-hidden rounded-2xl border cursor-pointer"
            style={{
              borderColor: 'var(--color-border-muted)',
              backgroundColor: 'var(--color-bg-card)',
              touchAction: 'pan-y',
            }}
            onClick={() => navigate(`/market?symbol=^${index.symbol}`)}
          >
            <IndexCardContent index={index} />
          </motion.div>
        </AnimatePresence>
      </div>

      {/* Dot indicators */}
      <div className="flex justify-center items-center gap-1.5 mt-3">
        {indices.map((_, i) => (
          <button
            key={i}
            onClick={() => goTo(i)}
            aria-label={t('dashboard.widgets.marketsOverview.showIndex', { name: indices[i]?.name })}
            className="rounded-full transition-all"
            style={{
              width: i === current ? 18 : 6,
              height: 6,
              backgroundColor:
                i === current
                  ? 'var(--color-accent-primary)'
                  : 'var(--color-border-default)',
            }}
          />
        ))}
      </div>
    </div>
  );
}

/* ── Skeleton loader (single card on mobile, grid on desktop) ── */

function IndexSkeleton({ count }: { count: number }) {
  // The card's own text elements with bars in them, so each line keeps the
  // height its text will have and the cards do not grow when the quotes land.
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="flex flex-col rounded-2xl border"
          style={{
            backgroundColor: 'var(--color-bg-card)',
            borderColor: 'var(--color-border-muted)',
          }}
        >
          <div className="@container p-4 pb-0">
            <div className={INDEX_HEADER_ROW}>
              <div className="min-w-0">
                <h3 className="text-base font-bold tracking-tight">
                  <SkeletonBar style={{ width: 80 }} />
                </h3>
                <div className="text-xs mt-0.5">
                  <SkeletonBar style={{ width: 40 }} />
                </div>
              </div>
              <div className={INDEX_HEADER_QUOTE}>
                <div className="text-lg font-bold tracking-tight dashboard-mono">
                  <SkeletonBar style={{ width: 80 }} />
                </div>
                <div className="text-xs dashboard-mono">
                  <SkeletonBar style={{ width: 60 }} />
                </div>
              </div>
            </div>
          </div>
          <div className="mt-2 px-1 pb-2" style={{ height: 100 }}>
            <div
              className="w-full h-full rounded animate-pulse"
              style={{ backgroundColor: 'var(--color-border-default)', opacity: 0.3 }}
            />
          </div>
        </div>
      ))}
    </>
  );
}

/* ── Main export ── */

function IndexMovementCard({
  indices = [],
  loading = false,
  forceMobile = false,
  singleRow = false,
}: IndexMovementCardProps) {
  const isMobileViewport = useIsMobile();
  const isMobile = forceMobile || isMobileViewport;

  if (isMobile) {
    if (loading) {
      return <IndexSkeleton count={1} />;
    }
    if (indices.length === 0) return null;
    return <IndexStackWidget indices={indices} />;
  }

  return (
    <SwapCell
      as="div"
      className="items-start"
      swapKey={loading ? 'loading' : 'ready'}
      layerClassName={cn(
        'grid gap-4',
        singleRow ? 'grid-flow-col auto-cols-fr' : 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5',
      )}
    >
      {loading ? (
        <IndexSkeleton count={5} />
      ) : (
        indices.map((index) => <IndexCard key={index.symbol} index={index} />)
      )}
    </SwapCell>
  );
}

export default IndexMovementCard;
