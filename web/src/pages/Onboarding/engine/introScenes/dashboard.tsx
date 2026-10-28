import {
  CalendarDays,
  Eye,
  Grid2x2,
  Layers,
  LineChart,
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
import { Appear, pointAt, pointerScript, pressAt, signColor } from './kit';
import type { IntroScene } from './types';
import { attachScene } from './dashboardAttach';
import {
  INDICES,
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
  PageBody,
  INSIGHT,
  BriefEyebrow,
  BriefHeadline,
  BriefSummary,
  TopicPill,
  EARNINGS,
  EarningsRow,
  GRID,
} from './dashboardPage';

/* Scenes for the dashboard intro: the live page, and customizing it, and
   from its own file (dashboardAttach.tsx) clipping a row into the chat. Each
   part is drawn after its real counterpart under pages/Dashboard, in
   miniature, with the app's own strings for its labels. A camera shows the
   page a part at a time, close enough to read, with an edge of the next
   part so the viewer can tell where on the page it is. */
/* eslint-disable react-refresh/only-export-components -- scene module:
   private scene components, one record export */

/* ── dashboard · step 1: the page, live ────────────────────────────────── */

/** The page's size, and its brief card's center, in page px (measured off
    it). */
const GRID_PAGE = { width: PAGE_W, height: 343 };
const BRIEF_MID = { x: 135, y: 137 };
/** Between the watchlist's symbols and its prices: a view from the page's
    left edge that ends here sits a touch closer than REST and cuts none of
    the watchlist's figures. */
const WATCH_NAMES_END = 308;

/** A slow pan across the page: the index lines with the brief arriving
    under them, a short push in on the brief, out to the watchlist while its
    quotes tick, the index cards over it, then out to the page whole. */
const GRID_SHOTS: [Shot, ...Shot[]] = [
  // the index cards and the brief, out to the watchlist's symbols
  shot(0, 0, 0, VIEW.width / WATCH_NAMES_END),
  // the brief, a little higher than its center so the index cards' changes
  // stay in at the top
  shot(2600, BRIEF_MID.x, BRIEF_MID.y - 8, CLOSE, 1000),
  // the watchlist whole, the index cards over it
  shot(3800, PAGE_W, 0, REST, 1000),
  shot(6400, PAGE_W / 2, GRID_PAGE.height / 2, VIEW.height / GRID_PAGE.height, 1000),
];

function DashboardGrid() {
  return (
    <Camera shots={GRID_SHOTS} page={GRID_PAGE}>
      <DashPage>
        <PageBody live />
      </DashPage>
    </Camera>
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

/* ── dashboard · step 2: customize (edit mode, drag and add widgets) ──── */

/** Customize, played out: press the edit-layout pencil, carry the earnings widget up the
    right column (the watchlist slides down to make room), then add a widget
    from the edit toolbar. */
const CUSTOM = {
  edit: 1250,
  grab: 2700,
  carry: 2800,
  drop: 3700,
  add: 4700,
  length: 6600,
};
/** The page's height (a window's, so the toolbar floats at its foot), its
    header's, and the grid's room above the floating toolbar. */
const CUSTOM_H = 360;
const HEADER_H = 28;
const GRID_TOP = HEADER_H + PAD;
const FLOAT_ROOM = 50;
/** The markets overview's row, then two rows of the same height, as the
    default layout's columns fill the rest of the window. */
const MARKETS_ROW = 50;
const ROW = (CUSTOM_H - GRID_TOP - PAD - FLOAT_ROOM - MARKETS_ROW - 2 * GAP) / 2;
const ROW_STEP = ROW + GAP;
/** The right column's left edge: the left one takes eight of twelve. */
const RIGHT_X = PAD + ((PAGE_W - 2 * PAD - GAP) * 2) / 3 + GAP;
const ROW2_TOP = GRID_TOP + MARKETS_ROW + GAP;
const ROW3_TOP = ROW2_TOP + ROW_STEP;
/** Pointer targets in page px, read off the layout: the header's
    edit-layout pencil, the earnings widget's edit header, and the toolbar's add button. */
const EDIT = { x: 367, y: 13 };
const GRAB = { x: RIGHT_X + 34, y: ROW3_TOP + 8 };
const DROP = { x: GRAB.x - 4, y: GRAB.y - ROW_STEP + 5 };
const ADD = { x: 132, y: CUSTOM_H - PAD - 11 };
/** The added widget's row. The real grid puts a new widget at the foot of
    the layout, full width, so the page grows by a row and the toolbar, which
    floats at the window's foot, rides down with it as the window follows.
    The row is as tall as lets the closing crop, from the second row's foot
    to the page's, keep the page's full width in. */
const MINI_ROW = 146;
const GROW = GAP + MINI_ROW;
const ADDED = CUSTOM.add + 120;
const GROW_DUR = 500;
const grown = (t: number) => (t >= ADDED ? GROW * ease(t, ADDED, GROW_DUR) : 0);
/** The page once it has grown by the new row, and the second row's foot,
    where the closing crop starts. */
const GROWN = { width: PAGE_W, height: CUSTOM_H + GROW };
const ROW2_FOOT = ROW2_TOP + ROW;
/** Past halfway, the watchlist gives up its slot. */
const SWAP = (CUSTOM.carry + CUSTOM.drop) / 2;
/** The pencil, the earnings widget's header grabbed and carried a row
    up, the toolbar's add button; then down with the toolbar as the page
    grows under it. */
const CUSTOM_POINTER = pointerScript(
  { at: 300, x: 300, y: 110 },
  {
    edit: { ...EDIT, arrive: 1100, click: CUSTOM.edit, leave: 1450 },
    grab: { ...GRAB, arrive: 2550, leave: CUSTOM.carry },
    drop: { ...DROP, arrive: CUSTOM.drop, leave: 4000 },
    add: { ...ADD, arrive: 4550, click: CUSTOM.add, leave: ADDED },
    ride: { x: ADD.x, y: ADD.y + GROW, arrive: ADDED + GROW_DUR },
  }
);
/** The page's top right under the header's pencil; a short push in on it,
    and back out down the right column as the earnings widget is
    carried up it; down to the toolbar's add button, and out to the page's
    full width as the page grows by the new widget's row, so the widget is
    whole with the toolbar under it. */
const CUSTOM_SHOTS: [Shot, ...Shot[]] = [
  shot(0, PAGE_W, 0, REST),
  shot(1200, PAGE_W, 0, CLOSE, 800),
  // the view's top in the gap over the brief and the watchlist
  shot(2500, PAGE_W, ROW2_TOP - GAP / 2 + VIEW.height / 2 / REST, REST, 1100),
  // the view's foot on the window's, the toolbar in
  shot(4600, PAGE_W, CUSTOM_H - VIEW.height / 2 / REST, REST, 600),
  // from the second row's foot to the page's
  shot(
    ADDED + GROW_DUR + 130,
    PAGE_W / 2,
    GROWN.height,
    VIEW.height / (GROWN.height - ROW2_FOOT),
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

/** The markets overview in its edit frame: an index per tile, name over
    price and change, as the widget's compact cards draw them. */
function MarketsBody() {
  const locale = useLocale();
  return (
    <div className="grid min-h-0 flex-1 grid-cols-5 gap-1">
      {INDICES.map(({ name, prev, ticks }) => {
        const v = ticks[ticks.length - 1].v;
        return (
          <span
            key={name}
            className="flex min-w-0 flex-col justify-center gap-[3px] rounded-[4px] border px-1"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            <span
              className="truncate text-[6px] font-bold leading-none"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {name}
            </span>
            <span className="flex items-baseline justify-between gap-0.5">
              <span
                className="font-mono text-[6px] leading-none tabular-nums"
                style={{ color: 'var(--color-text-primary)' }}
              >
                {grouped2(prev * (1 + v / 100), locale)}
              </span>
              <span
                className="font-mono text-[5px] leading-none tabular-nums"
                style={{ color: signColor(v) }}
              >
                {signedFixed2(v, locale)}%
              </span>
            </span>
          </span>
        );
      })}
    </div>
  );
}

/** The default layout in edit mode, each widget in its frame: the markets
    overview across the top, the brief over news on the left, the watchlist
    over earnings on the right. */
function EditGrid() {
  const { t: tr } = useTranslation();
  const newsHeader = tr('dashboard.widgets.newsFeed.header', {
    label: tr('dashboard.widgets.newsFeed.tab_market'),
  });
  const t = useSceneTime();
  const held = t >= CUSTOM.grab;
  const dropped = t >= CUSTOM.drop;
  const swapped = t >= SWAP;
  // the earnings widget rides with the pointer while held, then settles into
  // its new slot from wherever the pointer let go
  const lift = ease(t, CUSTOM.grab, 160) * (1 - ease(t, CUSTOM.drop, 260));
  const settle = 1 - ease(t, CUSTOM.drop, 260);
  const p = pointAt(CUSTOM_POINTER.path, t);
  const dx = dropped ? (DROP.x - GRAB.x) * settle : held ? p.x - GRAB.x : 0;
  const dy = dropped ? (DROP.y - GRAB.y + ROW_STEP) * settle : held ? p.y - GRAB.y : 0;
  return (
    <Appear at={CUSTOM.edit + 80} className="flex flex-col">
      <div
        className="grid grid-cols-[2fr_1fr] gap-1.5"
        style={{ gridTemplateRows: `${MARKETS_ROW}px ${ROW}px ${ROW}px` }}
      >
        <div style={{ gridColumn: '1 / 3', gridRow: 1 }}>
          <Widget icon={LineChart} title={tr('dashboard.widgets.marketsOverview.title')} edit>
            <MarketsBody />
          </Widget>
        </div>
        <div style={{ gridColumn: 1, gridRow: 2 }}>
          <Widget icon={Sparkles} title={tr('dashboard.widgets.insightBrief.title')} edit>
            <BriefEyebrow />
            <BriefHeadline className="line-clamp-2 shrink-0 text-[7px]" />
            <BriefSummary className="line-clamp-2 shrink-0 text-[5px]" />
            <span className="flex shrink-0 flex-wrap gap-[3px]">
              {INSIGHT.topics.map((topic) => (
                <TopicPill key={topic.text} {...topic} />
              ))}
            </span>
          </Widget>
        </div>
        <div style={{ gridColumn: 1, gridRow: 3 }}>
          <Widget
            icon={Newspaper}
            title={tr('dashboard.widgets.newsFeed.title')}
            header={newsHeader}
            edit
          >
            {NEWS.slice(0, 2).map((item) => (
              <NewsRow key={item.title} item={item} />
            ))}
          </Widget>
        </div>
        {/* where the held widget will land, as the grid marks it */}
        {held && t < CUSTOM.drop + 260 && (
          <div
            className="rounded-lg"
            style={{
              gridColumn: 2,
              gridRow: swapped ? 2 : 3,
              background: 'var(--color-accent-soft)',
            }}
          />
        )}
        {/* watchlist: slides one row down once the earnings pass halfway */}
        <div
          style={{
            gridColumn: 2,
            gridRow: swapped ? 3 : 2,
            transform: swapped
              ? `translateY(${-ROW_STEP * (1 - ease(t, SWAP, 300))}px)`
              : undefined,
          }}
        >
          <Widget
            icon={Eye}
            title={tr('dashboard.widgets.portfolioWatchlist.title')}
            edit
          >
            {WATCH.slice(0, 3).map((row) => (
              <WatchRow key={row.sym} row={row} />
            ))}
          </Widget>
        </div>
        {/* the earnings, carried */}
        <div
          className="relative"
          style={{
            gridColumn: 2,
            gridRow: dropped ? 2 : 3,
            zIndex: held ? 2 : undefined,
            transform: `translate(${dx}px, ${dy}px) scale(${1 + 0.015 * lift})`,
          }}
        >
          <Widget
            icon={CalendarDays}
            title={tr('dashboard.widgets.earningsCalendar.title')}
            edit
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
            {EARNINGS.map((row) => (
              <EarningsRow key={row.sym} row={row} />
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
              edit
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

/** The page as step 1 left it, until the pencil puts every widget in its
    frame. */
function DashboardCustomize() {
  const t = useSceneTime();
  const edit = t >= CUSTOM.edit;
  return (
    <Camera shots={CUSTOM_SHOTS} page={GROWN} pointer={CUSTOM_POINTER}>
      <DashPage
        height={CUSTOM_H + grown(t)}
        header={<DashTopbar edit={edit} pressed={pressAt(t, CUSTOM.edit)} />}
      >
        {t < CUSTOM.edit + 80 ? (
          <PageBody />
        ) : (
          /* the grid stops short of the floating toolbar, as its rows do */
          <div className="flex min-h-0 flex-1 flex-col" style={{ paddingBottom: FLOAT_ROOM }}>
            <EditGrid />
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
