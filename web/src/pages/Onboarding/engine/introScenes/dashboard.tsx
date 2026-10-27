import {
  CalendarDays,
  CandlestickChart,
  Eye,
  Grid2x2,
  Layers,
  Newspaper,
  Plus,
  RotateCcw,
  Sparkles,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocale } from '@/hooks/useLocale';
import { grouped2, signedFixed2 } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { IntroVisualId } from '../../registry';
import { CLOSE, Camera, REST, VIEW, shot, type Shot } from './camera';
import { ease, useSceneTime } from './clock';
import { Appear, pointAt, pointerScript, signColor } from './kit';
import type { IntroScene } from './types';
import { attachScene } from './dashboardAttach';
import {
  WATCH,
  Spark,
  DashTopbar,
  DashPage,
  PAGE_W,
  PAD,
  GAP,
  Floating,
  Composer,
  Widget,
} from './dashboardParts';
import {
  WatchRow,
  NEWS,
  NewsRow,
  ClassicBody,
  INSIGHT,
  BriefEyebrow,
  BriefHeadline,
  BriefSummary,
  TopicPill,
  EARNINGS,
  EarningsRow,
  GRID,
} from './dashboardClassic';

/* Scenes for the dashboard intro: the live page, and customizing it, and
   from its own file (dashboardAttach.tsx) clipping a row into the chat. Each
   part is drawn after its real counterpart under pages/Dashboard, in
   miniature, with the app's own strings for its labels. A camera shows the
   page a part at a time, close enough to read, with an edge of the next
   part so the viewer can tell where on the page it is. */
/* eslint-disable react-refresh/only-export-components -- scene module:
   private scene components, one record export */

/* ── dashboard · step 1: the page, live ────────────────────────────────── */

/** The Classic page's size, and its brief card's center, in page px
    (measured off it). */
const GRID_PAGE = { width: PAGE_W, height: 403 };
const BRIEF_MID = { x: 122, y: 170 };
/** Between the watchlist's symbols and its prices: a view from the page's
    left edge that ends here sits a touch closer than REST and cuts none of
    the watchlist's figures. */
const WATCH_NAMES_END = 300;
/** As wide as the camera goes before the page's text is too small to read,
    which shows the Classic page whole but for the news card's foot. */
const WIDE = 0.85;

/** A slow pan across the page: the title and index lines with the brief
    arriving under them, a short push in on the brief, out to the watchlist
    while its quotes tick, the index cards over it, then out to the page
    whole, the news under the brief. */
const GRID_SHOTS: [Shot, ...Shot[]] = [
  // the title, the index cards and the brief, out to the watchlist's symbols
  shot(0, 0, 0, VIEW.width / WATCH_NAMES_END),
  // the brief, a little higher than its center so the index cards' changes
  // stay in at the top
  shot(2600, BRIEF_MID.x, BRIEF_MID.y - 8, CLOSE, 1000),
  // the watchlist and the index cards over it, from under the page's title
  // (the left edge falls in the gutter after the first index card)
  shot(3800, PAGE_W, 155, REST, 1000),
  shot(6400, PAGE_W / 2, 0, WIDE, 1000),
];

function DashboardGrid() {
  return (
    <Camera shots={GRID_SHOTS} page={GRID_PAGE}>
      <DashPage>
        <ClassicBody live />
      </DashPage>
    </Camera>
  );
}

/** The price chart's daily candles: open, close, high, low (y grows down). */
const CANDLES: [number, number, number, number][] = [
  [30, 27, 25, 32],
  [27, 28, 25, 30],
  [28, 24, 22, 29],
  [24, 22, 20, 26],
  [22, 25, 21, 27],
  [25, 21, 19, 26],
  [21, 18, 16, 23],
  [18, 19, 16, 21],
  [19, 15, 13, 20],
  [15, 12, 10, 17],
  [12, 13, 10, 15],
  [13, 9, 7, 14],
];

function PriceChartBody() {
  const locale = useLocale();
  const nvda = WATCH[0];
  const v = nvda.ticks[nvda.ticks.length - 1].v;
  return (
    <>
      <span className="flex items-baseline gap-1">
        <span
          className="text-[7px] font-bold leading-none"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {nvda.sym}
        </span>
        <span
          className="font-mono text-[7px] leading-none tabular-nums"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {grouped2(nvda.prev * (1 + v / 100), locale)}
        </span>
        <span
          className="font-mono text-[6px] leading-none tabular-nums"
          style={{ color: signColor(v) }}
        >
          {signedFixed2(v, locale)}%
        </span>
      </span>
      <svg
        viewBox="0 0 100 36"
        preserveAspectRatio="none"
        className="min-h-0 w-full flex-1"
        aria-hidden
      >
        {CANDLES.map(([o, c, h, l], i) => {
          const x = 3 + i * 8;
          // y grows down: a close drawn level with or above its open is a rise
          const col = c <= o ? 'var(--color-profit)' : 'var(--color-loss)';
          return (
            <g key={i}>
              <line
                x1={x + 2}
                x2={x + 2}
                y1={h}
                y2={l}
                stroke={col}
                strokeWidth="1"
                vectorEffect="non-scaling-stroke"
              />
              <rect
                x={x}
                y={Math.min(o, c)}
                width="4"
                height={Math.max(Math.abs(o - c), 0.8)}
                fill={col}
              />
            </g>
          );
        })}
      </svg>
    </>
  );
}

/** The Mini Chart Grid the toolbar adds: a tile per symbol, its 30-day
    line. It starts from the watchlist, topped up to twelve. */
const MINI: { sym: string; v: number; series: number[] }[] = [
  { sym: 'NVDA', v: 1.61, series: [21, 19, 20, 15, 13, 11, 7] },
  { sym: 'AAPL', v: -0.08, series: [14, 15, 13, 15, 14, 16, 15] },
  { sym: 'TSLA', v: 2.05, series: [23, 21, 17, 19, 14, 10, 8] },
  { sym: 'MSFT', v: 0.34, series: [17, 16, 17, 15, 14, 15, 13] },
  { sym: 'AMZN', v: 0.84, series: [22, 20, 21, 16, 17, 12, 9] },
  { sym: 'GOOGL', v: -0.37, series: [12, 14, 11, 15, 17, 16, 20] },
  { sym: 'META', v: 1.12, series: [24, 19, 21, 17, 13, 14, 8] },
  { sym: 'AMD', v: -0.58, series: [10, 13, 12, 17, 15, 19, 21] },
  { sym: 'AVGO', v: 1.94, series: [22, 18, 19, 14, 12, 10, 6] },
  { sym: 'TSM', v: 1.27, series: [19, 20, 16, 15, 16, 12, 10] },
  { sym: 'NFLX', v: -0.42, series: [11, 12, 15, 13, 16, 18, 19] },
  { sym: 'JPM', v: 0.21, series: [16, 15, 16, 14, 15, 13, 14] },
];

function MiniChartBody() {
  const locale = useLocale();
  return (
    <div className="grid min-h-0 flex-1 grid-cols-6 grid-rows-2 gap-1">
      {MINI.map(({ sym, v, series }, i) => (
        <Appear
          key={sym}
          at={ADDED + 120 + i * 40}
          reserve
          className="flex min-w-0 flex-col justify-between gap-[3px] rounded-[4px] border p-1"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          <span className="flex items-center justify-between gap-0.5">
            <span
              className="text-[6px] font-bold leading-none"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {sym}
            </span>
            <span
              className="font-mono text-[5px] leading-none tabular-nums"
              style={{ color: signColor(v) }}
            >
              {signedFixed2(v, locale)}%
            </span>
          </span>
          <Spark series={series} color={signColor(v)} stretch className="min-h-0 w-full flex-1" />
        </Appear>
      ))}
    </div>
  );
}

/* ── dashboard · step 2: customize (Custom mode + add/drag widgets) ────── */

/** Customize, played out: flip to Custom, turn on edit mode with the pencil,
    carry the price chart one slot right (the watchlist slides over to make
    room), then add a widget from the edit toolbar. */
const CUSTOM = {
  toggle: 1100,
  edit: 1600,
  grab: 3000,
  carry: 3100,
  drop: 4000,
  add: 5000,
  length: 6900,
};
/** The page's height (a window's, so the toolbar floats at its foot), its
    header's, and the grid's room above the floating toolbar. */
const CUSTOM_H = 330;
const HEADER_H = 28;
const GRID_TOP = HEADER_H + PAD;
const FLOAT_ROOM = 50;
/** One grid column plus its gap, and one row (two rows share the room). */
const COL = (PAGE_W - 2 * PAD - 2 * GAP) / 3 + GAP;
const ROW = (CUSTOM_H - GRID_TOP - PAD - FLOAT_ROOM - GAP) / 2;
/** Pointer targets in page px, read off the layout: the header's controls,
    the price chart's edit header, and the toolbar's add button. */
const TOGGLE = { x: 330, y: 13 };
const PENCIL = { x: 367, y: 13 };
const GRAB = { x: PAD + COL + 30, y: GRID_TOP + 8 };
const DROP = { x: GRAB.x + COL, y: GRAB.y + 5 };
const ADD = { x: 132, y: CUSTOM_H - PAD - 11 };
/** The added widget's row. The real grid puts a new widget at the foot of
    the layout, full width, so the page grows by a row and the toolbar, which
    floats at the window's foot, rides down with it as the window follows.
    The row is as tall as lets the closing crop stop just above the toolbar
    with its right edge in the gap between the tiles' halves. */
const MINI_ROW = 132;
const GROW = GAP + MINI_ROW;
const ADDED = CUSTOM.add + 120;
const GROW_DUR = 500;
const grown = (t: number) => (t >= ADDED ? GROW * ease(t, ADDED, GROW_DUR) : 0);
/** The page once it has grown by the new row, and the first row's foot,
    where the closing crop starts. */
const GROWN = { width: PAGE_W, height: CUSTOM_H + GROW };
const ROW1_FOOT = GRID_TOP + ROW;
/** Past halfway, the watchlist gives up its slot. */
const SWAP = (CUSTOM.carry + CUSTOM.drop) / 2;
/** The switch, the pencil, the price chart's header grabbed and carried
    a column right, the toolbar's add button; then down with the toolbar as
    the page grows under it. */
const CUSTOM_POINTER = pointerScript(
  { at: 300, x: 300, y: 110 },
  {
    toggle: { ...TOGGLE, arrive: 1000, click: CUSTOM.toggle, leave: 1250 },
    pencil: { ...PENCIL, arrive: 1500, click: CUSTOM.edit, leave: 1800 },
    grab: { ...GRAB, arrive: 2850, leave: CUSTOM.carry },
    drop: { ...DROP, arrive: CUSTOM.drop, leave: 4300 },
    add: { ...ADD, arrive: 4850, click: CUSTOM.add, leave: ADDED },
    ride: { x: ADD.x, y: ADD.y + GROW, arrive: ADDED + GROW_DUR },
  }
);
/** The page's top right under the header's switch, the left column's edge
    beside it; a short push in on the switch and the pencil, and back out as
    the chart is carried a column right; down to the toolbar's add button,
    and out to the page's full width as the page grows by the new widget's
    row, so the widget is whole with the toolbar under it. */
const CUSTOM_SHOTS: [Shot, ...Shot[]] = [
  shot(0, PAGE_W, 0, REST),
  shot(1200, PAGE_W, 0, CLOSE, 800),
  shot(2800, PAGE_W, 0, REST, 1100),
  // the view's top over the first row's quotes, its foot just under the
  // toolbar
  shot(4900, PAGE_W, 190, REST, 600),
  // from the first row's foot to the page's
  shot(
    ADDED + GROW_DUR + 130,
    PAGE_W / 2,
    GROWN.height,
    VIEW.height / (GROWN.height - ROW1_FOOT),
    700
  ),
];

/** The toolbar edit mode floats at the foot of the page. */
function EditToolbar() {
  const { t } = useTranslation();
  const btn = 'flex items-center gap-[3px] rounded-full px-1.5 py-[3px] text-[7px] leading-none';
  return (
    <Appear
      at={CUSTOM.edit}
      className="flex items-center gap-0.5 rounded-full border px-1 py-[3px]"
      style={{
        background: 'var(--color-bg-elevated)',
        borderColor: 'var(--color-border-elevated)',
        boxShadow: '0 4px 12px -4px rgba(0, 0, 0, 0.25)',
      }}
    >
      <span
        className={cn(btn, 'font-medium')}
        style={{
          background: 'var(--color-btn-primary-bg)',
          color: 'var(--color-btn-primary-text)',
        }}
      >
        <Plus className="h-2 w-2" />
        {t('dashboard.widgets.shell.addWidget')}
      </span>
      <span className={cn(btn, 'font-medium')} style={{ color: 'var(--color-text-primary)' }}>
        <Layers className="h-2 w-2" />
        {t('dashboard.widgets.shell.presets')}
      </span>
      <span className={btn} style={{ color: 'var(--color-text-secondary)' }}>
        <RotateCcw className="h-2 w-2" />
        {t('dashboard.widgets.shell.reset')}
      </span>
      <span className="mx-0.5 h-2.5 w-px" style={{ background: 'var(--color-border-muted)' }} />
      <span className={cn(btn, 'font-medium')} style={{ color: 'var(--color-text-secondary)' }}>
        <X className="h-2 w-2" />
        {t('dashboard.widgets.shell.done')}
      </span>
    </Appear>
  );
}

function CustomGrid({ edit }: { edit: boolean }) {
  const { t: tr } = useTranslation();
  const newsHeader = tr('dashboard.widgets.newsFeed.header', {
    label: tr('dashboard.widgets.newsFeed.tab_market'),
  });
  const t = useSceneTime();
  const held = t >= CUSTOM.grab;
  const dropped = t >= CUSTOM.drop;
  const swapped = t >= SWAP;
  // the price chart rides with the pointer while held, then settles into its
  // new slot from wherever the pointer let go
  const lift = ease(t, CUSTOM.grab, 160) * (1 - ease(t, CUSTOM.drop, 260));
  const settle = 1 - ease(t, CUSTOM.drop, 260);
  const p = pointAt(CUSTOM_POINTER.path, t);
  const dx = dropped ? (DROP.x - GRAB.x - COL) * settle : held ? p.x - GRAB.x : 0;
  const dy = dropped ? (DROP.y - GRAB.y) * settle : held ? p.y - GRAB.y : 0;
  return (
    <Appear at={CUSTOM.toggle + 80} className="flex flex-col">
      <div className="grid grid-cols-3 gap-1.5" style={{ gridTemplateRows: `${ROW}px ${ROW}px` }}>
        <div style={{ gridColumn: 1, gridRow: 1 }}>
          <Widget icon={Sparkles} title={tr('dashboard.widgets.insightBrief.title')} edit={edit}>
            {/* the brief card itself, in a third of the page */}
            <BriefEyebrow />
            <BriefHeadline className="line-clamp-3 shrink-0 text-[7px]" />
            <BriefSummary className="line-clamp-2 shrink-0 text-[5px]" />
            <span className="flex shrink-0 flex-wrap gap-[3px]">
              {INSIGHT.topics.slice(0, 2).map((topic) => (
                <TopicPill key={topic.text} {...topic} />
              ))}
            </span>
          </Widget>
        </div>
        {/* where the held widget will land, as the grid marks it */}
        {held && t < CUSTOM.drop + 260 && (
          <div
            className="rounded-lg"
            style={{
              gridColumn: swapped ? 3 : 2,
              gridRow: 1,
              background: 'var(--color-accent-soft)',
            }}
          />
        )}
        {/* watchlist: slides one slot left once the chart passes halfway */}
        <div
          style={{
            gridColumn: swapped ? 2 : 3,
            gridRow: 1,
            transform: swapped ? `translateX(${COL * (1 - ease(t, SWAP, 300))}px)` : undefined,
          }}
        >
          <Widget
            icon={Eye}
            title={tr('dashboard.widgets.watchlist.title')}
            header={tr('dashboard.widgets.watchlist.header')}
            count="3"
            edit={edit}
          >
            {WATCH.slice(0, 3).map((row) => (
              <WatchRow key={row.sym} row={row} />
            ))}
          </Widget>
        </div>
        {/* the price chart, carried */}
        <div
          className="relative"
          style={{
            gridColumn: dropped ? 3 : 2,
            gridRow: 1,
            zIndex: held ? 2 : undefined,
            transform: `translate(${dx}px, ${dy}px) scale(${1 + 0.015 * lift})`,
          }}
        >
          <Widget
            icon={CandlestickChart}
            title={tr('dashboard.widgets.chart.title')}
            edit={edit}
            style={
              lift > 0
                ? {
                    boxShadow: `0 10px 20px -8px rgba(0, 0, 0, ${(0.28 * lift).toFixed(3)}), 0 0 0 ${
                      lift > 0.5 ? 1 : 0
                    }px var(--color-accent-primary)`,
                  }
                : undefined
            }
          >
            <PriceChartBody />
          </Widget>
        </div>
        {/* the second row as the real grid packs it: no slot left open */}
        <div style={{ gridColumn: 1, gridRow: 2 }}>
          <Widget
            icon={CalendarDays}
            title={tr('dashboard.widgets.earningsCalendar.title')}
            edit={edit}
          >
            {EARNINGS.map((row) => (
              <EarningsRow key={row.sym} row={row} />
            ))}
          </Widget>
        </div>
        <div style={{ gridColumn: '2 / 4', gridRow: 2 }}>
          <Widget
            icon={Newspaper}
            title={tr('dashboard.widgets.newsFeed.title')}
            header={newsHeader}
            count="24"
            edit={edit}
          >
            {NEWS.slice(0, 2).map((item) => (
              <NewsRow key={item.title} item={item} />
            ))}
          </Widget>
        </div>
      </div>
      {/* the widget the toolbar adds, at the layout's foot, full width */}
      {t >= ADDED && (
        <div className="shrink-0 overflow-hidden" style={{ height: grown(t) }}>
          <Appear at={ADDED} pop className="flex" style={{ paddingTop: GAP, height: GROW }}>
            <Widget
              icon={Grid2x2}
              title={tr('dashboard.widgets.miniChartGrid.title')}
              header={tr('dashboard.widgets.miniChartGrid.header')}
              edit={edit}
              className="flex-1"
            >
              <MiniChartBody />
            </Widget>
          </Appear>
        </div>
      )}
    </Appear>
  );
}

function DashboardCustomize() {
  const t = useSceneTime();
  const custom = t >= CUSTOM.toggle;
  const edit = t >= CUSTOM.edit;
  return (
    <Camera shots={CUSTOM_SHOTS} page={GROWN} pointer={CUSTOM_POINTER}>
      <DashPage
        height={CUSTOM_H + grown(t)}
        header={<DashTopbar custom={custom} pencil edit={edit} />}
      >
        {t < CUSTOM.toggle + 80 ? (
          <ClassicBody />
        ) : (
          /* the grid stops short of the floating chat, as its rows do */
          <div className="flex min-h-0 flex-1 flex-col" style={{ paddingBottom: FLOAT_ROOM }}>
            <CustomGrid edit={edit} />
          </div>
        )}
        {/* the floating chat, which edit mode swaps for its toolbar */}
        <Floating>{edit ? <EditToolbar /> : <Composer delay={0.46} />}</Floating>
      </DashPage>
    </Camera>
  );
}

export const dashboardScenes = {
  dashboardGrid: { Scene: DashboardGrid, length: GRID.length, anchor: 'left' },
  dashboardCustomize: { Scene: DashboardCustomize, length: CUSTOM.length, anchor: 'right' },
  dashboardAttach: attachScene,
} satisfies Partial<Record<IntroVisualId, IntroScene>>;
