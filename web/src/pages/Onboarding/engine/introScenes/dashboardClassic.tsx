import type { ReactNode } from 'react';
import {
  ArrowRight,
  Briefcase,
  ChevronRight,
  Clock,
  Eye,
  Newspaper,
  Plus,
  Sparkles,
  TrendingUp,
  type LucideIcon,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocale } from '@/hooks/useLocale';
import { grouped2, relativeTime, shortMonthDay, signedFixed2 } from '@/lib/format';
import { cn } from '@/lib/utils';
import { PAST, ease, useSceneTime } from './clock';
import { Appear, Region, rv, signColor } from './kit';
import { changeAt, INDICES, WATCH, Spark } from './dashboardParts';

/* The classic dashboard page, drawn in miniature: index cards, the watchlist
   card and the news column. Shared by the scenes that show the page. Quotes,
   headlines and symbols are invented. */
/* eslint-disable react-refresh/only-export-components -- shared parts:
   scene components plus data and style helpers */

/** The live page's script (ms), which the dashboard intro's first scene
    plays: the index lines trace in and keep taking new points, the brief
    comes in part by part, quotes tick (their times are in INDICES and WATCH,
    set to land while the camera is on them). */
export const GRID = {
  draw: 300,
  drawDur: 1100,
  brief: 700,
  briefPer: 240,
  shifts: [1900, 3000, 4100],
  shiftDur: 520,
  length: 7600,
};

/* ── classic page ──────────────────────────────────────────────────────── */

/** One index card: name, price and change in mono, the day's line in the
    change's color. `reach`/`slid` drive the line on the live page. */
function IndexCard({
  index,
  live,
  reach,
  slid,
}: {
  index: (typeof INDICES)[number];
  live: boolean;
  reach?: number;
  slid?: number;
}) {
  const locale = useLocale();
  const t = useSceneTime();
  const v = changeAt(index.ticks, t, live);
  return (
    <Region className="flex min-w-0 flex-col gap-[3px] p-1.5">
      <span
        className="truncate text-[7px] font-bold leading-none"
        style={{ color: 'var(--color-text-primary)' }}
      >
        {index.name}
      </span>
      <span
        className="font-mono text-[8px] font-bold leading-none tabular-nums"
        style={{ color: 'var(--color-text-primary)' }}
      >
        {grouped2(index.prev * (1 + v / 100), locale)}
      </span>
      <span
        className={'font-mono text-[6px] leading-none tabular-nums transition-colors duration-quick'}
        style={{ color: signColor(v) }}
      >
        {signedFixed2(v, locale)}%
      </span>
      <Spark
        series={index.spark}
        color={signColor(v)}
        reach={reach}
        slid={slid}
        stretch
        className="mt-[2px] h-3 w-full"
      />
    </Region>
  );
}

/** One watchlist row: symbol over its type, price over the day's move, and
    the % change in its tinted pill. */
export function WatchRow({ row, live = false }: { row: (typeof WATCH)[number]; live?: boolean }) {
  const { t: tr } = useTranslation();
  const locale = useLocale();
  const t = useSceneTime();
  const v = changeAt(row.ticks, t, live);
  const c = signColor(v);
  return (
    <div className="flex items-center gap-1 px-1 py-[3px]">
      <span className="flex min-w-0 flex-col gap-[2px]">
        <span
          className="text-[7px] font-bold leading-none"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {row.sym}
        </span>
        <span className="text-[5px] leading-none" style={{ color: 'var(--color-text-secondary)' }}>
          {tr('dashboard.portfolioWatchlistCard.stock')}
        </span>
      </span>
      <span className="ml-auto flex flex-col items-end gap-[2px]">
        <span
          className="font-mono text-[7px] font-medium leading-none tabular-nums"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {grouped2(row.prev * (1 + v / 100), locale)}
        </span>
        <span
          className={
            'font-mono text-[5px] font-medium leading-none tabular-nums transition-colors ' +
            'duration-quick'
          }
          style={{ color: c }}
        >
          {signedFixed2((row.prev * v) / 100, locale)}
        </span>
      </span>
      <span
        className={
          'min-w-[27px] rounded-[3px] px-[3px] py-[2px] text-center text-[6px] font-bold ' +
          'leading-none tabular-nums transition-colors duration-quick'
        }
        style={{
          background: signColor(v, true),
          color: c,
        }}
      >
        {signedFixed2(v, locale)}%
      </span>
    </div>
  );
}

/** The dashed button closing the watchlist. */
export function AddSymbol() {
  const { t } = useTranslation();
  return (
    <span
      className={
        'mt-1 flex items-center justify-center gap-[3px] rounded-[5px] border border-dashed ' +
        'py-[5px] text-[6px] font-medium leading-none'
      }
      style={{ borderColor: 'var(--color-border-default)', color: 'var(--color-text-secondary)' }}
    >
      <Plus className="h-[7px] w-[7px]" />
      {t('dashboard.portfolioWatchlistCard.addSymbol')}
    </span>
  );
}

/* ── the AI brief ──────────────────────────────────────────────────────── */

/** The insight the brief card leads with, as the insight service writes one:
    a headline, a summary and its topics, each moving up or down. It agrees
    with the quotes (INDICES, WATCH) and the lead headline in NEWS. */
export const INSIGHT = {
  headline: 'Chipmakers lead a broad rally as NVDA guides above estimates',
  summary:
    'The Nasdaq 100 rose 0.7% and the S&P 500 0.4%. NVDA gained 1.6% after guiding ' +
    'data-center revenue above estimates, lifting AVGO and TSM, while Treasury yields ' +
    "slipped ahead of Friday's jobs report.",
  topics: [
    { text: 'Semiconductors', up: true },
    { text: 'AI Capex', up: true },
    { text: 'Bond Yields', up: false },
  ],
  /** How long ago it was written, in minutes. */
  age: 23,
};

/** The brief's eyebrow chip (`AIDailyBriefCard`): sparkles and the label on
    the accent's soft fill. */
export function BriefEyebrow() {
  const { t } = useTranslation();
  return (
    <span
      className={
        'flex w-fit shrink-0 items-center gap-[3px] rounded-full border px-1 py-[2px] ' +
        'text-[5px] font-semibold uppercase leading-none tracking-wider'
      }
      style={{
        background: 'var(--color-accent-soft)',
        borderColor: 'var(--color-accent-overlay)',
        color: 'var(--color-accent-light)',
      }}
    >
      <Sparkles className="h-[6px] w-[6px]" />
      {t('dashboard.brief.eyebrow')}
    </span>
  );
}

/** A topic under the brief (`TopicBadge`): its name on the soft fill of the
    way it moved. */
export function TopicPill({ text, up }: { text: string; up: boolean }) {
  const soft = signColor(up, true);
  return (
    <span
      className="rounded-[2px] border px-[3px] py-[2px] text-[5px] font-medium leading-none"
      style={{
        background: soft,
        borderColor: soft,
        color: signColor(up),
      }}
    >
      #{text}
    </span>
  );
}

/** The brief's headline, at the card's display weight. Its leading goes
    after the caller's classes: `cn` reads a text size there as setting the
    line height too, and would drop a leading that came before it. */
export function BriefHeadline({ className }: { className?: string }) {
  return (
    <span
      className={cn('font-bold', className, 'leading-tight')}
      style={{ color: 'var(--color-text-primary)' }}
    >
      {INSIGHT.headline}
    </span>
  );
}

/** The brief's summary, in the card's secondary text. Its leading goes
    last, as the headline's does. */
export function BriefSummary({ className }: { className?: string }) {
  return (
    <span
      className={cn(className, 'leading-relaxed')}
      style={{ color: 'var(--color-text-secondary)' }}
    >
      {INSIGHT.summary}
    </span>
  );
}

/**
 * The brief card on the Classic page (`AIDailyBriefCard`): the eyebrow and
 * when it was written, the headline, the summary, its topics, then the two
 * buttons at the foot, with the card's newspaper mark in the corner. On the
 * live page each part enters in turn from `at`, as the card's parts do.
 */
function BriefCard({ at }: { at: number }) {
  const { t } = useTranslation();
  const locale = useLocale();
  const btn =
    'flex items-center gap-[3px] rounded-[4px] px-1.5 py-[3px] text-[5px] ' +
    'font-semibold leading-none';
  return (
    <Region delay={0.22} className="relative flex min-w-0 flex-col gap-[5px] overflow-hidden p-2">
      <Newspaper
        className="pointer-events-none absolute right-1.5 top-1.5 h-7 w-7"
        strokeWidth={1.5}
        style={{ color: 'var(--color-text-tertiary)', opacity: 0.2 }}
        aria-hidden
      />
      <span className="flex items-center gap-1">
        <BriefEyebrow />
        <span
          className="truncate text-[5px] leading-none"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {t('dashboard.brief.updatedWhen', {
            when: relativeTime(-INSIGHT.age * 60_000, locale, 0),
          })}
        </span>
      </span>
      <Appear at={at}>
        <BriefHeadline className="block pr-7 text-[9px]" />
      </Appear>
      <Appear at={at + GRID.briefPer}>
        <BriefSummary className="block text-[6px]" />
      </Appear>
      <Appear at={at + 2 * GRID.briefPer} className="flex flex-wrap gap-[3px]">
        {INSIGHT.topics.map((topic) => (
          <TopicPill key={topic.text} {...topic} />
        ))}
      </Appear>
      <Appear at={at + 3 * GRID.briefPer} className="mt-auto flex justify-end gap-1">
        <span
          className={cn(btn, 'border')}
          style={{
            borderColor: 'var(--color-border-default)',
            color: 'var(--color-text-secondary)',
          }}
        >
          <Sparkles className="h-[6px] w-[6px]" />
          {t('dashboard.brief.generatePersonalized')}
        </span>
        <span
          className={btn}
          style={{
            background: 'var(--color-btn-primary-bg)',
            color: 'var(--color-btn-primary-text)',
          }}
        >
          {t('dashboard.brief.readFullBrief')}
          <ArrowRight className="h-[6px] w-[6px]" />
        </span>
      </Appear>
    </Region>
  );
}

/* ── news ──────────────────────────────────────────────────────────────── */

/** The market feed. `thumb` is the story's picture, drawn as the chart
    image market news usually runs with: its line and that line's color. */
export const NEWS = [
  {
    title: 'Chipmakers rally after earnings beat',
    source: 'Benzinga',
    hours: 2,
    hot: true,
    tickers: ['NVDA', 'AVGO', 'TSM'],
    thumb: { line: [16, 15, 13, 14, 10, 8, 5], color: '#3FB950' },
  },
  {
    title: 'Treasury yields slip ahead of jobs data',
    source: 'Investing.com',
    hours: 4,
    hot: false,
    tickers: [],
    thumb: { line: [6, 7, 6, 9, 10, 12, 13], color: '#F85149' },
  },
  {
    title: 'Oil steadies as supply worries ease',
    source: 'MarketWatch',
    hours: 5,
    hot: false,
    tickers: [],
    thumb: { line: [12, 8, 11, 9, 10, 9, 10], color: '#E3B341' },
  },
  {
    title: 'Small caps lag as rate bets shift',
    source: 'Zacks',
    hours: 6,
    hot: false,
    tickers: [],
    thumb: { line: [8, 9, 8, 11, 10, 13, 14], color: '#F85149' },
  },
  {
    title: 'Dollar firms ahead of central bank minutes',
    source: "Barron's",
    hours: 8,
    hot: false,
    tickers: [],
    thumb: { line: [13, 12, 12, 10, 11, 9, 8], color: '#58A6FF' },
  },
];

/** A story's picture: a market chart, as the feed's images usually are. A
    picture keeps its own colors in either theme. */
function NewsThumb({ thumb }: { thumb: (typeof NEWS)[number]['thumb'] }) {
  const points = thumb.line.map((y, i) => `${2 + i * 3.33},${y}`).join(' ');
  return (
    <svg viewBox="0 0 24 18" className="h-[18px] w-6 shrink-0 rounded-[3px]" aria-hidden>
      <rect width="24" height="18" fill="#161B22" />
      {[6, 12].map((y) => (
        <line key={y} x1="0" x2="24" y1={y} y2={y} stroke="#2A313C" strokeWidth="0.5" />
      ))}
      <polyline
        points={points}
        fill="none"
        stroke={thumb.color}
        strokeWidth="1"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** A news row (`NewsFeedWidget`'s row): the story's picture, then the
    sentiment dot, the source in the accent and how long ago, the headline,
    and the tickers it names. */
export function NewsRow({
  item,
  hovered = false,
  children,
}: {
  item: (typeof NEWS)[number];
  hovered?: boolean;
  children?: ReactNode;
}) {
  const locale = useLocale();
  return (
    <div
      className="relative flex items-start gap-1.5 rounded-[4px] px-1 py-1 transition-colors duration-quick"
      style={{ background: hovered ? 'var(--color-bg-card-hover)' : 'transparent' }}
    >
      <NewsThumb thumb={item.thumb} />
      <span className="flex min-w-0 flex-1 flex-col gap-[3px]">
        <span className="flex items-center gap-[3px]">
          <span
            className="h-[3px] w-[3px] shrink-0 rounded-full"
            style={{ background: item.hot ? 'var(--color-profit)' : 'var(--color-text-secondary)' }}
          />
          <span
            className="truncate text-[4.5px] font-semibold uppercase leading-none tracking-wide"
            style={{ color: 'var(--color-accent-light)' }}
          >
            {item.source}
          </span>
          <span
            className="flex shrink-0 items-center gap-[2px] text-[5px] leading-none"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            <Clock className="h-[5px] w-[5px]" />
            {relativeTime(-item.hours * 3_600_000, locale, 0)}
          </span>
        </span>
        <span
          className="line-clamp-2 text-[7px] font-medium leading-[1.3]"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {item.title}
        </span>
        {item.tickers.length > 0 && (
          <span className="flex gap-[2px]">
            {item.tickers.map((sym) => (
              <span
                key={sym}
                className="rounded-[2px] px-[3px] py-[1.5px] text-[4.5px] font-bold leading-none"
                style={{
                  background: 'var(--color-accent-soft)',
                  color: 'var(--color-accent-light)',
                }}
              >
                {sym}
              </span>
            ))}
          </span>
        )}
      </span>
      {children}
    </div>
  );
}

/* ── earnings ──────────────────────────────────────────────────────────── */

/** Who reports next, from the watchlist. */
export const EARNINGS = [
  { sym: 'MSFT', name: 'Microsoft Corp.', date: new Date(2026, 9, 28) },
  { sym: 'AMZN', name: 'Amazon.com Inc.', date: new Date(2026, 9, 30) },
];

/** An upcoming report (`EarningsCalendarCard`'s row): the logo tile with the
    symbol's first letters, symbol over company, and the date. */
export function EarningsRow({ row }: { row: (typeof EARNINGS)[number] }) {
  const locale = useLocale();
  return (
    <span className="flex items-center gap-1 px-0.5">
      <span
        className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[3px] border"
        style={{ background: 'var(--color-bg-tag)', borderColor: 'var(--color-border-muted)' }}
      >
        <span
          className="text-[4.5px] font-bold leading-none"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {row.sym.slice(0, 2)}
        </span>
      </span>
      <span className="flex min-w-0 flex-col gap-[2px]">
        <span
          className="text-[6px] font-bold leading-none"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {row.sym}
        </span>
        <span
          className="truncate text-[5px] leading-none"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {row.name}
        </span>
      </span>
      <span
        className="ml-auto shrink-0 text-[6px] font-medium leading-none"
        style={{ color: 'var(--color-text-primary)' }}
      >
        {shortMonthDay(row.date, locale)}
      </span>
    </span>
  );
}

const NEWS_TABS: { icon: LucideIcon; label: string }[] = [
  { icon: TrendingUp, label: 'dashboard.newsFeedCard.tabMarket' },
  { icon: Briefcase, label: 'dashboard.newsFeedCard.tabPortfolio' },
  { icon: Eye, label: 'dashboard.newsFeedCard.tabWatchlist' },
];

/**
 * The Classic page under the header: title, index cards, then the brief and
 * news on the left and the watchlist and earnings on the right. `live` plays
 * the script (the brief comes in part by part, the index lines trace in and
 * keep taking points, quotes tick); otherwise it sits on the finished frame.
 */
export function ClassicBody({ live = false }: { live?: boolean }) {
  const { t: tr } = useTranslation();
  const now = useSceneTime();
  const t = live ? now : Number.POSITIVE_INFINITY;
  const slid = GRID.shifts.reduce((s, at) => s + ease(t, at, GRID.shiftDur), 0);
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <span
        className="intro-rv mb-1.5 text-[11px] font-bold leading-none"
        style={{ ...rv(0.05), color: 'var(--color-text-primary)' }}
      >
        {tr('dashboard.classic.marketOverview')}
      </span>
      <div className="intro-rv grid grid-cols-5 gap-1.5" style={rv(0.1)}>
        {INDICES.map((index, i) => (
          <IndexCard
            key={index.name}
            index={index}
            live={live}
            reach={100 * ease(t, GRID.draw + i * 90, GRID.drawDur)}
            slid={slid}
          />
        ))}
      </div>
      {/* brief and watchlist side by side, then news and earnings under them */}
      <div className="mt-1.5 grid min-h-0 grid-cols-[3fr_2fr] content-start gap-1.5">
        {/* the AI brief; on the live page its parts enter in turn */}
        <BriefCard at={live ? GRID.brief : PAST} />
        <Region delay={0.32} className="flex min-w-0 flex-col p-2">
          <div className="mb-1 flex items-center justify-between gap-1">
            <span
              className="text-[8px] font-bold leading-none"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {tr('dashboard.portfolioWatchlistCard.headerWatchlist')}
            </span>
            <span
              className="flex rounded-[4px] p-[1.5px]"
              style={{ background: 'var(--color-bg-tag)' }}
            >
              {['tabWatch', 'tabHoldings'].map((label, i) => (
                <span
                  key={label}
                  className="rounded-[3px] px-1 py-[2px] text-[5px] font-medium leading-none"
                  style={{
                    background: i === 0 ? 'var(--color-bg-elevated)' : 'transparent',
                    color: i === 0 ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
                  }}
                >
                  {tr(`dashboard.portfolioWatchlistCard.${label}`)}
                </span>
              ))}
            </span>
          </div>
          {WATCH.map((row) => (
            <WatchRow key={row.sym} row={row} live={live} />
          ))}
          <AddSymbol />
        </Region>
        {/* news and earnings, the next row down */}
        <Region delay={0.3} className="flex min-w-0 flex-col gap-1 p-2">
          <div
            className="flex border-b pb-1.5"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            <span
              className="flex gap-[2px] rounded-[4px] p-[2px]"
              style={{ background: 'var(--color-bg-tag)' }}
            >
              {NEWS_TABS.map(({ icon: Icon, label }, i) => (
                <span
                  key={label}
                  className={
                    'flex items-center gap-[2px] rounded-[3px] px-1 py-[2px] text-[5px] ' +
                    'font-medium leading-none'
                  }
                  style={{
                    background: i === 0 ? 'var(--color-bg-elevated)' : 'transparent',
                    color: i === 0 ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
                  }}
                >
                  <Icon className="h-[6px] w-[6px]" />
                  {tr(label)}
                </span>
              ))}
            </span>
          </div>
          {NEWS.slice(0, 3).map((item) => (
            <NewsRow key={item.title} item={item} />
          ))}
        </Region>
        <Region delay={0.4} className="flex min-w-0 flex-col gap-1.5 p-2">
          <span className="flex items-center justify-between gap-1">
            <span
              className="text-[8px] font-bold leading-none"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {tr('dashboard.widgets.earningsCalendar.title')}
            </span>
            <span
              className="flex items-center text-[5px] leading-none"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {tr('dashboard.earningsCalendarCard.viewAll')}
              <ChevronRight className="h-[6px] w-[6px]" />
            </span>
          </span>
          <span
            className="px-0.5 text-[4.5px] font-bold uppercase leading-none tracking-wider"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {tr('dashboard.earningsCalendarCard.sectionUpcoming')}
          </span>
          {EARNINGS.map((row) => (
            <EarningsRow key={row.sym} row={row} />
          ))}
        </Region>
      </div>
    </div>
  );
}
