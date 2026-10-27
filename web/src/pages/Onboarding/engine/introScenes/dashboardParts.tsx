import type { CSSProperties, ReactNode } from 'react';
import {
  GripVertical,
  HelpCircle,
  LayoutGrid,
  MoreVertical,
  Pencil,
  Search,
  type LucideIcon,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils';
import { InputDock, Region } from './kit';

/* The dashboard intro's shared parts: invented quotes, sparklines, the page
   and its header, the floating chat, and the widget frame. Each part is drawn
   after its real counterpart under pages/Dashboard, in miniature, with the
   app's own strings for its labels. The scenes show the page through a
   camera, so it is drawn at its own size and the camera picks the crop. */
/* eslint-disable react-refresh/only-export-components -- shared parts:
   scene components plus data and style helpers */

/* ── live quotes ───────────────────────────────────────────────────────── */

/** A quote's % change over the script: each value holds from its `at`. */
type Ticks = { at: number; v: number }[];

/** The change `ticks` holds at `t`; pages that sit still show the last one. */
export function changeAt(ticks: Ticks, t: number, live: boolean): number {
  if (!live) return ticks[ticks.length - 1].v;
  let i = 0;
  while (i + 1 < ticks.length && ticks[i + 1].at <= t) i++;
  return ticks[i].v;
}

/** The index cards. Their ticks run on DashboardGrid's script; the final
    signs are the static design's (up, up, down, up, down). Each `spark`
    runs three points past the seven on screen (see `Spark`). */
export const INDICES: { name: string; prev: number; ticks: Ticks; spark: number[] }[] = [
  {
    name: 'S&P 500',
    prev: 5820.1,
    ticks: [
      { at: 0, v: 0.31 },
      { at: 1400, v: 0.38 },
      { at: 4800, v: 0.42 },
    ],
    spark: [20, 22, 19, 18, 16, 17, 13, 12, 9, 8],
  },
  {
    name: 'Nasdaq 100',
    prev: 20480.55,
    ticks: [
      { at: 0, v: 0.62 },
      { at: 4400, v: 0.71 },
    ],
    spark: [24, 21, 22, 18, 17, 14, 15, 11, 9, 6],
  },
  {
    name: 'Dow Jones',
    prev: 42810.2,
    ticks: [
      { at: 0, v: 0.05 },
      { at: 2300, v: -0.02 },
    ],
    spark: [14, 12, 15, 13, 16, 14, 15, 17, 15, 17],
  },
  {
    name: 'Russell 2000',
    prev: 2268.4,
    ticks: [
      { at: 0, v: -0.14 },
      { at: 3200, v: 0.09 },
    ],
    spark: [16, 19, 18, 21, 20, 18, 17, 18, 15, 13],
  },
  {
    name: 'VIX',
    prev: 16.12,
    ticks: [
      { at: 0, v: -1.2 },
      { at: 3600, v: -1.64 },
    ],
    spark: [8, 10, 9, 12, 14, 13, 17, 18, 21, 22],
  },
];

export const WATCH: { sym: string; prev: number; ticks: Ticks }[] = [
  {
    sym: 'NVDA',
    prev: 179.53,
    ticks: [
      { at: 0, v: 1.42 },
      { at: 4000, v: 1.61 },
    ],
  },
  {
    sym: 'AAPL',
    prev: 226.8,
    ticks: [
      { at: 0, v: 0.12 },
      { at: 4500, v: -0.08 },
    ],
  },
  {
    sym: 'TSLA',
    prev: 248.1,
    ticks: [
      { at: 0, v: 2.31 },
      { at: 5000, v: 2.05 },
    ],
  },
  { sym: 'MSFT', prev: 431.2, ticks: [{ at: 0, v: 0.34 }] },
  { sym: 'AMZN', prev: 186.4, ticks: [{ at: 0, v: 0.84 }] },
];

/* ── sparklines ────────────────────────────────────────────────────────── */

/** Steps across a sparkline: seven points on screen. */
const SPARK_STEPS = 6;

/** The line slid `slid` steps left and traced out to x = `reach` (of 100),
    clipped to the chart, plus where it ends. */
function sparkLine(series: number[], slid: number, reach: number) {
  const step = 100 / SPARK_STEPS;
  const pts = series.map((y, i) => ({ x: (i - slid) * step, y }));
  const out: string[] = [];
  let end = pts[0];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1];
    const b = pts[i];
    if (b.x <= 0 || a.x >= reach) continue;
    const yAt = (x: number) => a.y + ((b.y - a.y) * (x - a.x)) / (b.x - a.x);
    const x0 = Math.max(a.x, 0);
    const x1 = Math.min(b.x, reach);
    if (out.length === 0) out.push(`${x0},${yAt(x0)}`);
    out.push(`${x1},${yAt(x1)}`);
    end = { x: x1, y: yAt(x1) };
  }
  return { points: out.join(' '), end };
}

/**
 * A sparkline over `series`. A series longer than the seven points on screen
 * starts slid to its last seven; each point that arrives slides the window a
 * step left. `stretch` fills its box (the index cards, no end dot); otherwise
 * the box keeps the 10:3 viewBox and the line ends on a dot, as the mini
 * chart tiles draw it.
 */
export function Spark({
  series,
  color,
  slid = series.length - 1 - SPARK_STEPS,
  reach = 100,
  stretch = false,
  className = '',
}: {
  series: number[];
  color: string;
  slid?: number;
  reach?: number;
  stretch?: boolean;
  className?: string;
}) {
  const { points, end } = sparkLine(series, slid, reach);
  return (
    <svg
      viewBox="0 0 100 30"
      preserveAspectRatio={stretch ? 'none' : undefined}
      className={className}
      aria-hidden
    >
      {points && (
        <>
          <polyline
            points={points}
            fill="none"
            stroke={color}
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
            strokeLinejoin="round"
            strokeLinecap="round"
            className="transition-colors duration-quick"
          />
          {!stretch && <circle cx={end.x} cy={end.y} r="3" fill={color} />}
        </>
      )}
    </svg>
  );
}

/* ── page chrome ───────────────────────────────────────────────────────── */

/** The Classic / Custom switch: a neutral segmented control, the active
    side on the elevated fill. */
function ModeToggle({ custom }: { custom: boolean }) {
  const { t } = useTranslation();
  const seg = (on: boolean): CSSProperties => ({
    background: on ? 'var(--color-bg-elevated)' : 'transparent',
    color: on ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
    fontWeight: on ? 600 : 400,
  });
  const cls = 'flex items-center gap-[3px] rounded-[4px] px-1.5 py-[3px] text-[7px] leading-none';
  return (
    <span
      className="flex items-center rounded-md border p-[1.5px]"
      style={{ background: 'var(--color-bg-input)', borderColor: 'var(--color-border-muted)' }}
    >
      <span className={cn(cls, 'transition-colors duration-quick')} style={seg(!custom)}>
        {t('dashboard.layoutToggle.classic')}
      </span>
      <span className={cn(cls, 'transition-colors duration-quick')} style={seg(custom)}>
        <LayoutGrid className="h-[7px] w-[7px]" />
        {t('dashboard.layoutToggle.custom')}
      </span>
    </span>
  );
}

/**
 * The dashboard header: symbol search on the left, then the layout toggle,
 * the edit-layout pencil (Custom mode only; `pencil` keeps its slot so the
 * toggle holds still when it shows) and help. `edit` fills the pencil, as
 * edit mode does.
 */
export function DashTopbar({
  custom = false,
  pencil = false,
  edit = false,
}: {
  custom?: boolean;
  pencil?: boolean;
  edit?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <>
      <span
        className="flex h-[17px] w-[44%] items-center gap-1 rounded-md border px-1.5"
        style={{ background: 'var(--color-bg-input)', borderColor: 'var(--color-border-muted)' }}
      >
        <Search className="h-2 w-2 shrink-0" style={{ color: 'var(--color-icon-muted)' }} />
        <span
          className="truncate text-[6px] leading-none"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {t('dashboard.searchPlaceholder')}
        </span>
        <span
          className="ml-auto rounded-[2px] border px-[2px] text-[5px] leading-[1.3]"
          style={{
            borderColor: 'var(--color-border-default)',
            color: 'var(--color-text-quaternary)',
          }}
        >
          /
        </span>
      </span>
      <span className="ml-auto flex items-center gap-1.5">
        <ModeToggle custom={custom} />
        {pencil && (
          <span
            className={
              'flex h-3.5 w-3.5 items-center justify-center rounded-[4px] transition-colors ' +
              'duration-quick'
            }
            style={{
              opacity: custom ? 1 : 0,
              background: edit ? 'var(--color-btn-primary-bg)' : 'transparent',
              color: edit ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
            }}
          >
            <Pencil className="h-2 w-2" />
          </span>
        )}
        <HelpCircle className="h-2.5 w-2.5" style={{ color: 'var(--color-text-secondary)' }} />
      </span>
    </>
  );
}

/** The page's width, in page px: what the camera's shots are measured in. */
export const PAGE_W = 400;
/** The page's padding and the gap between its cards, in page px. */
export const PAD = 10;
export const GAP = 6;

/**
 * The dashboard page with no app around it: `header` over the content when
 * a step is about the header, the content padded under it. `height` holds
 * the page to a window's height, for a chat that floats at its foot; what
 * runs past it is cut off, as the page scrolls on under the fold.
 */
export function DashPage({
  header,
  height,
  children,
}: {
  header?: ReactNode;
  height?: number;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col overflow-hidden" style={{ width: PAGE_W, height }}>
      {header && (
        <div
          className="flex h-7 shrink-0 items-center gap-2 border-b px-2.5"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          {header}
        </div>
      )}
      <div className="relative flex min-h-0 flex-1 flex-col gap-1.5" style={{ padding: PAD }}>
        {children}
      </div>
    </div>
  );
}

/** The floating chat's own lift off the page (Dashboard.css), scaled down. */
const FLOAT_SHADOW = '0 4px 12px rgba(0, 0, 0, 0.12)';

/** What floats over the foot of a `DashPage`, as the real chat is fixed
    over the content and the page runs on under it. */
export function Floating({ children }: { children: ReactNode }) {
  return (
    <div className="absolute inset-x-0 flex justify-center" style={{ bottom: PAD }}>
      {children}
    </div>
  );
}

/** The floating chat, as wide as the real one sits on the content column. */
export function Composer({
  delay,
  focused,
  chip,
  text,
}: {
  delay?: number;
  focused?: boolean;
  chip?: ReactNode;
  text?: ReactNode;
}) {
  return (
    <div className="w-[236px] rounded-[11px]" style={{ boxShadow: FLOAT_SHADOW }}>
      <InputDock delay={delay} focused={focused} chip={chip} text={text} />
    </div>
  );
}

/* ── custom widgets ────────────────────────────────────────────────────── */

/** Edit mode draws every widget in a dashed accent frame. */
const EDIT_FRAME: CSSProperties = {
  borderStyle: 'dashed',
  borderColor: 'var(--color-accent-primary)',
};

/**
 * A widget on the Custom grid. At rest it is the widget's own card: icon,
 * eyebrow and count over the body. In edit mode the frame takes over: a
 * dashed accent edge and a header with the drag grip, the title and the
 * widget menu.
 */
export function Widget({
  icon: Icon,
  title,
  header = title,
  count,
  edit = false,
  delay,
  className,
  style,
  children,
}: {
  icon: LucideIcon;
  title: string;
  /** What the widget's own card calls itself, when that differs from the
      title the edit frame shows. */
  header?: string;
  count?: string;
  edit?: boolean;
  delay?: number;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
}) {
  const label = (
    <span
      className="truncate text-[6px] font-semibold uppercase leading-none"
      style={{ color: 'var(--color-text-secondary)', letterSpacing: edit ? '0.06em' : '0.14em' }}
    >
      {edit ? title : header}
    </span>
  );
  return (
    <Region
      delay={delay}
      className={cn('flex h-full min-w-0 flex-col overflow-hidden', !edit && 'p-1.5', className)}
      style={edit ? { ...EDIT_FRAME, ...style } : style}
    >
      {edit ? (
        <div
          className="flex h-4 shrink-0 items-center gap-1 border-b px-1"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          <GripVertical
            className="h-2.5 w-2.5 shrink-0"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
          {label}
          <MoreVertical
            className="ml-auto h-2.5 w-2.5 shrink-0"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
        </div>
      ) : (
        <div
          className="mb-1 flex shrink-0 items-center gap-1 border-b pb-1"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          <Icon className="h-2 w-2 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
          {label}
          {count && (
            <span
              className="font-mono text-[8px] leading-none tabular-nums"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {count}
            </span>
          )}
        </div>
      )}
      <div className={cn('flex min-h-0 flex-1 flex-col gap-1', edit && 'p-1.5')}>{children}</div>
    </Region>
  );
}
