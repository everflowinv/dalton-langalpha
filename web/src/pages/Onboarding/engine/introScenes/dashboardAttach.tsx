import type { CSSProperties } from 'react';
import { Eye, Newspaper, Paperclip, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocale } from '@/hooks/useLocale';
import { relativeTime } from '@/lib/format';
import { CLOSE, Camera, VIEW, corner, shot, type Shot } from './camera';
import { easeInOut, useSceneTime } from './clock';
import { Typed, pointerScript } from './kit';
import type { IntroScene } from './types';
import { WATCH, DashPage, PAD, PAGE_W, Floating, Composer, Widget } from './dashboardParts';
import { WatchRow, NEWS, NewsRow, AddSymbol } from './dashboardClassic';

/* The dashboard intro's last scene: clipping a row into the chat's context.
   Drawn after its real counterpart under pages/Dashboard (row paperclip,
   context deck), in miniature. It plays from the dashboard intro's record,
   so it downloads with that intro's other scenes. */
/* eslint-disable react-refresh/only-export-components -- scene module: a
   private scene component and its record entry */

/* ── dashboard · step 3: clip a row into chat context ──────────────────── */

/** Clipping a row, played out: the pointer finds the news row's paperclip
    (it shows on hover), clicks, the row flies into the composer and lands as
    a context card, then a question about it is typed. */
const ATTACH = {
  hover: 900,
  click: 1350,
  leave: 1900,
  fly: 1450,
  flyDur: 1000,
  focus: 2750,
  type: 2900,
  ask: 'Is this already priced in?',
  length: 5000,
};
/** The page's size: a window's height, so the chat floats at its foot. */
const ATTACH_PAGE = { width: PAGE_W, height: 300 };
/** Page px, read off the layout: the first row and its paperclip, the
    card in the composer's open deck, and the line its text goes on. */
const ROW = { x: 17, y: 34, w: 208 };
const CLIP = { x: 216, y: 52 };
const CARD = { x: 89, y: 216, w: 222 };
const TEXT = { x: 198, y: 257 };
/** Where the crops' edges fall, in page px: between the watchlist's
    symbols and its prices; across the rows' pictures, before their titles;
    the line under the feed's header; above the headers' text; the foot of
    the composer, which floats the page's padding above the page's foot;
    and the widgets' left padding, which puts the right edge of a view at
    the last shot's zoom between the watchlist's prices and their changes. */
const WATCH_NAMES_END = 300;
const PICTURES = 33;
const UNDER_HEADER = 31;
const OVER_HEADER = 15;
const COMPOSER_FOOT = ATTACH_PAGE.height - PAD;
const WIDGET_PAD = 12;
/** The flying card's box: the clipped row, then its slot in the deck. It
    eases in and out over the camera's move, so it rides along in view. */
const CARD_FROM = { x: ROW.x, y: ROW.y + 5, w: ROW.w };
/** Onto the row's paperclip and a click on it, then down to the
    composer's line, clicked to type. */
const ATTACH_POINTER = pointerScript(
  { at: 300, x: 150, y: 140 },
  {
    clip: { ...CLIP, arrive: 1150, click: ATTACH.click, leave: ATTACH.leave },
    text: { ...TEXT, arrive: 2650, click: ATTACH.focus },
  }
);
/** The feed under its header, out to the watchlist's symbols, a touch
    closer than REST; a short push in on the rows as the paperclip is
    clicked; then out with the card as it flies down to the composer, a
    touch wider than REST so both widgets' headers and the composer's foot
    are in: the card it lands as is whole, and the question under it
    reads. */
const ATTACH_SHOTS: [Shot, ...Shot[]] = [
  shot(0, 0, 0, VIEW.width / WATCH_NAMES_END),
  corner(1150, PICTURES, UNDER_HEADER, CLOSE, 900),
  corner(
    ATTACH.fly + ATTACH.flyDur,
    WIDGET_PAD,
    OVER_HEADER,
    VIEW.height / (COMPOSER_FOOT - OVER_HEADER),
    ATTACH.flyDur
  ),
];

/** The context card a clipped row becomes in the composer: the story's
    title, then its source and when it ran, as the row's snapshot labels
    it. */
function ContextCard({ style }: { style?: CSSProperties }) {
  const locale = useLocale();
  return (
    <div
      className="flex items-center gap-1.5 rounded-md border p-1"
      style={{
        background: 'var(--color-bg-card)',
        borderColor: 'var(--color-border-muted)',
        ...style,
      }}
    >
      <span
        className="flex h-4 w-4 shrink-0 items-center justify-center rounded-[4px]"
        style={{ background: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}
      >
        <Newspaper className="h-2 w-2" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-[3px]">
        <span
          className="truncate text-[7px] font-medium leading-none"
          style={{ color: 'var(--color-text-primary)' }}
        >
          News: {NEWS[0].title}
        </span>
        <span
          className="truncate text-[5px] leading-none"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {NEWS[0].source} · {relativeTime(-NEWS[0].hours * 3_600_000, locale, 0)}
        </span>
      </span>
      <X className="h-2 w-2 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
    </div>
  );
}

/** The deck over the composer's text: the count, then the card on top. */
function ContextDeck({ shown }: { shown: boolean }) {
  const { t } = useTranslation();
  return (
    <div
      className="flex flex-col gap-1 border-b border-dashed pb-1"
      style={{ borderColor: 'var(--color-border-muted)', visibility: shown ? 'visible' : 'hidden' }}
    >
      <span
        className={
          'flex items-center gap-1 px-0.5 font-mono text-[5px] uppercase leading-none ' +
          'tracking-[0.18em]'
        }
        style={{ color: 'var(--color-accent-primary)' }}
      >
        <span
          className="h-[3px] w-[3px] rounded-full"
          style={{ background: 'var(--color-accent-primary)' }}
        />
        {t('chat.widgetContext.inContext', { count: 1 })}
      </span>
      <ContextCard />
    </div>
  );
}

function DashboardAttach() {
  const { t: tr } = useTranslation();
  const t = useSceneTime();
  const hovered = t >= ATTACH.hover && t < ATTACH.leave + 150;
  const { clip } = ATTACH_POINTER.beats;
  const onClip = t >= clip.arrive && t < clip.leave;
  const landed = t >= ATTACH.fly + ATTACH.flyDur;
  // eased as the camera's move is, so the card keeps pace with it
  const fly = easeInOut(t, ATTACH.fly, ATTACH.flyDur);
  return (
    <Camera shots={ATTACH_SHOTS} page={ATTACH_PAGE} pointer={ATTACH_POINTER}>
      <DashPage height={ATTACH_PAGE.height}>
        <div className="flex min-h-0 flex-1 gap-1.5">
          {/* news widget: the paperclip shows on the hovered row */}
          <Widget
            icon={Newspaper}
            title={tr('dashboard.widgets.newsFeed.header', {
              label: tr('dashboard.widgets.newsFeed.tab_market'),
            })}
            count="24"
            delay={0.12}
            className="flex-[1.5]"
          >
            {NEWS.map((item, i) => (
              <NewsRow key={item.title} item={item} hovered={i === 0 && hovered}>
                {i === 0 && (
                  <span
                    className={
                      'absolute right-0.5 top-1/2 flex h-3.5 w-3.5 -translate-y-1/2 ' +
                      'items-center justify-center rounded-full ' +
                      'transition-[opacity,background-color,color] duration-150'
                    }
                    style={{
                      opacity: hovered ? 1 : 0,
                      background: onClip ? 'var(--color-accent-soft)' : 'transparent',
                      color: onClip ? 'var(--color-accent-primary)' : 'var(--color-text-tertiary)',
                    }}
                  >
                    <Paperclip className="h-2 w-2" />
                  </span>
                )}
              </NewsRow>
            ))}
          </Widget>
          <Widget
            icon={Eye}
            title={tr('dashboard.widgets.watchlist.header')}
            count="5"
            delay={0.24}
            className="flex-1"
          >
            {WATCH.map((row) => (
              <WatchRow key={row.sym} row={row} />
            ))}
            <AddSymbol />
          </Widget>
        </div>
        {/* the floating chat: takes the clipped row, then the question */}
        <Floating>
          <Composer
            focused={t >= ATTACH.focus}
            delay={0.36}
            chip={
              /* opens while the card is on its way, so it lands in place */
              <div
                className="grid"
                style={{ gridTemplateRows: `${fly}fr`, marginBottom: (fly - 1) * 6 }}
              >
                <div className="min-h-0 overflow-hidden">
                  <ContextDeck shown={landed} />
                </div>
              </div>
            }
            text={t >= ATTACH.type ? <Typed text={ATTACH.ask} at={ATTACH.type} /> : undefined}
          />
        </Floating>
        {/* the row on its way into the composer */}
        {t >= ATTACH.fly && !landed && (
          <ContextCard
            style={{
              position: 'absolute',
              pointerEvents: 'none',
              left: CARD_FROM.x + (CARD.x - CARD_FROM.x) * fly,
              top: CARD_FROM.y + (CARD.y - CARD_FROM.y) * fly,
              width: CARD_FROM.w + (CARD.w - CARD_FROM.w) * fly,
              // lifted off the row: solid at once, with the shadow a
              // carried card takes
              opacity: Math.min(fly * 12, 1),
              boxShadow: '0 6px 14px -6px rgba(0, 0, 0, 0.3)',
            }}
          />
        )}
      </DashPage>
    </Camera>
  );
}

/** The scene's entry in the dashboard record (dashboard.tsx). */
export const attachScene: IntroScene = {
  Scene: DashboardAttach,
  length: ATTACH.length,
  anchor: 'right',
};
