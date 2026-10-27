import type { CSSProperties, ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { FileText, GripVertical, Pin, Plus, Search, Server, Upload, X } from 'lucide-react';
import { HomeRowIcon } from '@/hooks/useHomeWorkspace';
import { useLocale } from '@/hooks/useLocale';
import { shortMonthDay } from '@/lib/format';
import { cn } from '@/lib/utils';
import { COMPUTER_STATUS_UI } from '@/pages/ChatAgent/utils/computerStatus';
import type { IntroVisualId } from '../../registry';
import { during, progress, useSceneTime } from './clock';
import { CLOSE, Camera, REST, type Shot, VIEW, shot, wide } from './camera';
import { Appear, Caret, Loader, Region, Typed, pointerScript, pressAt, rv } from './kit';
import type { IntroScene } from './types';
import type { Icon } from './chatParts';

/* Scenes for the workspace gallery: the gallery page, and creating a
   workspace over it. Each part is a small copy of the component the app
   draws it with (named on each), in the same tokens and labels. */
/* eslint-disable react-refresh/only-export-components -- scene module:
   private scene components, one record export */

/* ── chat · step 4: the workspace gallery page ─────────────────────────── */

/** An outlined header button, as the gallery draws its secondary actions. */
function OutlineButton({
  icon: Glyph,
  color = 'var(--color-text-tertiary)',
  children,
}: {
  icon?: Icon;
  color?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        'flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1',
        'text-[7px] leading-none'
      )}
      style={{ borderColor: 'var(--color-border-muted)', color }}
    >
      {Glyph && <Glyph className="h-2 w-2" />}
      {children}
    </span>
  );
}

/** The gallery's header (`WorkspaceGallery`, `GalleryActions`): the title,
    then Computers and New workspace. `pressNew` presses New. */
function GalleryHeader({
  pressNew,
  className,
  style,
}: {
  pressNew?: string;
  className?: string;
  style?: CSSProperties;
}) {
  const { t } = useTranslation();
  return (
    <div className={cn('flex items-center gap-1.5', className)} style={style}>
      <span
        className="mr-auto text-[12px] font-semibold leading-none tracking-[-0.01em]"
        style={{ color: 'var(--color-text-primary)' }}
      >
        {t('workspace.workspaces')}
      </span>
      <OutlineButton icon={Server} color="var(--color-text-secondary)">
        <span className="font-medium">{t('computer.computers', 'Computers')}</span>
      </OutlineButton>
      <span
        className={cn(
          'flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1',
          'transition-transform duration-150'
        )}
        style={{
          background: 'var(--color-btn-primary-bg)',
          color: 'var(--color-btn-primary-text)',
          transform: pressNew,
        }}
      >
        <Plus className="h-2 w-2" />
        <span className="text-[7px] font-medium leading-none">{t('workspace.newWorkspace')}</span>
      </span>
    </div>
  );
}

/** The machine every workspace in the gallery runs on, and the moments it
    goes from stopped to starting to running. */
type MachineState = 'stopped' | 'starting' | 'running';

/** A machine's state as `ComputerStatusIndicator` draws it, from the app's
    own table (`COMPUTER_STATUS_UI`): a hollow ring, the loader, or a filled
    dot in the state's colour, then the state's name. */
function MachineStatus({ state }: { state: MachineState }) {
  const { t } = useTranslation();
  const { labelKey, shape, color } = COMPUTER_STATUS_UI[state];
  return (
    <span className="inline-flex shrink-0 items-center gap-1">
      {shape === 'live' ? (
        <Loader color={color} className="text-[6px]" />
      ) : (
        <span
          className="h-[4px] w-[4px] shrink-0 rounded-full"
          style={shape === 'dot' ? { background: color } : { border: `1px solid ${color}` }}
        />
      )}
      {t(labelKey)}
    </span>
  );
}

/** One workspace in the gallery: its name, what it is for, and when it last
    changed. `ran` is when an agent finishes work in it, from which it reads
    as updated today. */
interface Desk {
  name: string;
  description: string;
  updated: Date;
  pinned?: boolean;
  ran?: number;
}

/**
 * A card in the gallery (`WorkspaceCard`): name, one line of description,
 * the machine it runs on with that machine's state, and when it was updated.
 * All workspaces is the flat elevated card with the home glyph and no
 * machine line; every other card sits on the card fill.
 */
function GalleryCard({
  desk,
  home = false,
  machine,
  settled = false,
}: {
  desk: Desk;
  home?: boolean;
  machine: MachineState;
  /** The agents' work has landed already: `ran` has passed from the start. */
  settled?: boolean;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const now = useSceneTime();
  const fresh = desk.ran != null && (settled || now >= desk.ran);
  return (
    <div
      className="flex min-w-0 flex-1 flex-col gap-1.5 rounded-lg border py-2 pl-2.5 pr-2"
      style={
        home
          ? { background: 'var(--color-bg-elevated)', borderColor: 'var(--color-border-default)' }
          : {
              background: 'var(--color-bg-card-gradient)',
              borderColor: 'var(--color-bg-card-border)',
            }
      }
    >
      <div className="flex min-w-0 items-center gap-1">
        {home && (
          <HomeRowIcon
            className="h-2.5 w-2.5 shrink-0"
            style={{ color: 'var(--color-accent-primary)' }}
          />
        )}
        {desk.pinned && (
          <Pin
            className="h-2 w-2 shrink-0 rotate-45"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
        )}
        <span
          className="truncate text-[9px] font-medium leading-none"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {desk.name}
        </span>
      </div>
      <span
        className="line-clamp-1 text-[7px] leading-[1.3]"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        {desk.description}
      </span>
      {!home && (
        <span
          className="flex min-w-0 items-center gap-1 text-[6px] leading-none"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          <Server className="h-[7px] w-[7px] shrink-0" />
          <span className="truncate">Cloud computer</span>
          <MachineStatus state={machine} />
        </span>
      )}
      <span
        key={fresh ? 'fresh' : 'old'}
        className={cn('mt-auto block text-[6px] leading-none', fresh && 'intro-rv')}
        style={{ ...rv(0), color: 'var(--color-text-tertiary)' }}
      >
        {t('workspace.updated', {
          time: shortMonthDay(fresh ? GRID.today : desk.updated, locale),
        })}
      </span>
    </div>
  );
}

/** The gallery, kept calm: the cards come in, the one machine they share
    starts (each card's machine line turns at once), then two of them read
    as updated today as agents finish work in them. Sorted by activity:
    All workspaces, the pinned card, then the most recently updated. */
const GRID = {
  cards: [150, 230, 310, 390, 470, 550],
  starting: 2000,
  running: 3000,
  today: new Date(2026, 9, 21),
  desks: [
    {
      name: 'NVDA deep dive',
      description: 'Q3 print vs guidance, data-center demand and a reverse DCF.',
      updated: new Date(2026, 9, 20),
      pinned: true,
      ran: 3700,
    },
    {
      name: 'Portfolio monitoring',
      description: 'Daily risk check on current holdings and earnings dates.',
      updated: new Date(2026, 9, 19),
      ran: 5300,
    },
    {
      name: 'AI semis comps',
      description: 'Nine-name comps table on NTM multiples, refreshed weekly.',
      updated: new Date(2026, 9, 17),
    },
    {
      name: 'Fed path and rates',
      description: 'Rate-path pricing against the dot plot, after each print.',
      updated: new Date(2026, 9, 14),
    },
    {
      name: 'TSMC supply chain',
      description: 'Supplier map and capex read-through for 2027.',
      updated: new Date(2026, 9, 9),
    },
  ] satisfies Desk[],
  length: 6300,
};

/**
 * The gallery page (`WorkspaceGallery`) at the view's size: the header,
 * search, sort, and the cards two to a row. Each card comes in at its time
 * in `cards`; without them the gallery is there from the start, and the
 * work that lands in it has landed. `pressNew` presses New workspace.
 */
function GalleryPage({
  machine,
  cards,
  pressNew,
}: {
  machine: MachineState;
  cards?: number[];
  pressNew?: string;
}) {
  const { t } = useTranslation();
  const home: Desk = {
    name: t('agents.allWorkspaces'),
    description: t('agents.widgets.allWorkspacesDesc'),
    updated: GRID.today,
  };
  return (
    <div className="flex flex-col gap-2 p-2.5" style={VIEW}>
      <GalleryHeader pressNew={pressNew} className="intro-rv" style={rv(0.08)} />
      {/* search */}
      <Region delay={0.16} className="flex items-center gap-1.5 px-2 py-1.5">
        <Search className="h-2.5 w-2.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
        <span className="text-[8px] leading-none" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('workspace.searchWorkspaces')}
        </span>
      </Region>
      {/* sort */}
      <div className="intro-rv flex items-center justify-end gap-1.5" style={rv(0.2)}>
        <span className="text-[7px] leading-none" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('workspace.sortBy')}
        </span>
        <OutlineButton>{t('workspace.activity')}</OutlineButton>
        <OutlineButton icon={GripVertical}>{t('workspace.reorder')}</OutlineButton>
      </div>
      <div className="grid min-h-0 flex-1 auto-rows-fr grid-cols-2 gap-2">
        {[home, ...GRID.desks].map((desk, i) => (
          <Appear key={i} at={cards?.[i] ?? 0} reserve className="flex min-w-0">
            <GalleryCard desk={desk} home={i === 0} machine={machine} settled={!cards} />
          </Appear>
        ))}
      </div>
    </div>
  );
}

/** Opens on the whole gallery, pushes in on two cards' machine lines as the
    computer they share starts, with the left column's edge still in view,
    then pulls back as work lands in them. */
const GRID_SHOTS: [Shot, ...Shot[]] = [wide(0), shot(1700, 297, 168, CLOSE, 800), wide(5000, 900)];

function WorkspaceGrid() {
  const now = useSceneTime();
  const machine: MachineState =
    now >= GRID.running ? 'running' : now >= GRID.starting ? 'starting' : 'stopped';
  return (
    <Camera shots={GRID_SHOTS}>
      <GalleryPage machine={machine} cards={GRID.cards} />
    </Camera>
  );
}

/* ── chat · step 5: the new-workspace modal over the gallery ───────────── */

/** Creating a workspace, played out: the pointer clicks New workspace, the
    modal opens, the name and description type in, a click on the drop zone
    adds two files, and the pointer clicks Create. */
const NEW_WS = {
  name: 'Hyperscaler capex tracker',
  desc: 'Capex guidance for MSFT, AMZN, GOOGL, META…',
  clickNew: 1100,
  modal: 1250,
  typeName: 2000,
  clickDesc: 3000,
  typeDesc: 3150,
  clickDrop: 4900,
  files: [5050, 5250],
  upload: 700,
  clickCreate: 6700,
  length: 7600,
};

/** The modal's width (px), centered on the page. */
const MODAL_W = 260;
const MODAL_X = (VIEW.width - MODAL_W) / 2;

/** The whole gallery, as the step before ends on it, while the pointer goes
    to New workspace; then on the form as the modal opens: name and
    description, down to the files, and out to the whole page for Create. */
const NEW_WS_SHOTS: [Shot, ...Shot[]] = [
  wide(0),
  shot(1950, 200, 80, REST, 700),
  shot(4800, 200, 250, REST, 700),
  wide(6400, 700),
];

/** New workspace, then the description's free space past its text, the
    drop zone's browse link, and Create, in page px. Each point sits 2px up
    and left of its target, where the pointer's tip lands; on Create it
    lands in the padding past the label, so the finished frame still reads
    it. */
const NEW_WS_POINTER = pointerScript(
  { at: 200, x: 300, y: 130 },
  {
    new: { x: 348, y: 17, arrive: 1050, click: NEW_WS.clickNew, leave: 2100 },
    desc: { x: 298, y: 126, arrive: 2950, click: NEW_WS.clickDesc, leave: 4300 },
    drop: { x: 230, y: 222, arrive: 4850, click: NEW_WS.clickDrop, leave: 5900 },
    create: { x: 310, y: 297, arrive: 6650, click: NEW_WS.clickCreate },
  }
);

/** A field's text typed in place: an invisible copy of the finished text
    holds the line's size, so nothing around it moves while it types. A
    focused field that has not started typing shows the caret. */
function TypedInPlace({ text, at, focus }: { text: string; at: number; focus: boolean }) {
  const t = useSceneTime();
  return (
    <span className="grid">
      <span className="invisible [grid-area:1/1]">{text}</span>
      <span className="[grid-area:1/1]">
        <Typed text={text} at={at} />
        {focus && t < at && <Caret />}
      </span>
    </span>
  );
}

/** A field's label, as the modal sets it: the name, then a required mark or
    an optional note. */
function FieldLabel({ children, required = false }: { children: ReactNode; required?: boolean }) {
  const { t } = useTranslation();
  return (
    <span
      className="text-[8px] font-medium leading-none"
      style={{ color: 'var(--color-text-primary)' }}
    >
      {children}{' '}
      {required ? (
        <span style={{ color: 'var(--color-loss)' }}>*</span>
      ) : (
        <span className="text-[7px] font-normal" style={{ color: 'var(--color-text-secondary)' }}>
          {t('common.optional')}
        </span>
      )}
    </span>
  );
}

/** A text field in the modal: the card fill, and the accent edge while focused. */
function field(focus: boolean): CSSProperties {
  return {
    background: 'var(--color-bg-card)',
    borderColor: focus ? 'var(--color-accent-primary)' : 'var(--color-border-muted)',
  };
}

/** A file added to the new workspace: its upload bar fills from `at`, then
    the row reads as the modal lists a file, size and remove. Its space is
    held so the modal does not grow. */
function UploadRow({ name, size, at }: { name: string; size: string; at: number }) {
  const t = useSceneTime();
  const p = progress(t, at, NEW_WS.upload);
  return (
    <Appear
      at={at}
      reserve
      className="flex items-center gap-1.5 rounded-[4px] border px-1.5 py-1"
      style={{ background: 'var(--color-bg-tag)', borderColor: 'var(--color-border-muted)' }}
    >
      <FileText className="h-2 w-2 shrink-0" style={{ color: 'var(--color-text-secondary)' }} />
      <span
        className="truncate text-[7px] leading-none"
        style={{ color: 'var(--color-text-primary)' }}
      >
        {name}
      </span>
      <span className="ml-auto flex h-2 items-center gap-1">
        {p < 1 ? (
          <span
            className="block h-0.5 w-8 overflow-hidden rounded-full"
            style={{ background: 'var(--color-border-muted)' }}
          >
            <span
              className="block h-full rounded-full"
              style={{
                width: `${p * 100}%`,
                background: 'var(--color-accent-primary)',
                transition: 'width 90ms linear',
              }}
            />
          </span>
        ) : (
          <>
            <span
              className="text-[6px] leading-none"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {size}
            </span>
            <X className="h-1.5 w-1.5" style={{ color: 'var(--color-text-secondary)' }} />
          </>
        )}
      </span>
    </Appear>
  );
}

function CreateWorkspace() {
  const { t } = useTranslation();
  const now = useSceneTime();
  const open = now >= NEW_WS.modal;
  const nameFocus = during(now, NEW_WS.modal, NEW_WS.clickDesc);
  const descFocus = during(now, NEW_WS.clickDesc, NEW_WS.clickDrop);
  const { drop } = NEW_WS_POINTER.beats;
  const overDrop = during(now, drop.arrive, drop.leave);
  const typed = 'text-[8px] leading-none';
  return (
    <Camera shots={NEW_WS_SHOTS} pointer={NEW_WS_POINTER}>
      <div className="relative" style={VIEW}>
        {/* the gallery, dimmed behind the modal once it opens */}
        <div style={{ opacity: open ? 0.45 : 1, transition: 'opacity 300ms ease' }}>
          <GalleryPage machine="running" pressNew={pressAt(now, NEW_WS.clickNew)} />
        </div>
        {/* the modal the click opens (`CreateWorkspaceModal`): name,
            description, files, Create. Centering transform lives on a static
            wrapper — the pop animates `transform` and would clobber translate
            utilities on itself. */}
        <div
          className="absolute top-1/2 -translate-y-1/2"
          style={{ left: MODAL_X, width: MODAL_W }}
        >
          <Appear
            at={NEW_WS.modal}
            pop
            className="rounded-[10px] border p-3"
            style={{
              background: 'var(--color-bg-elevated)',
              borderColor: 'var(--color-border-muted)',
            }}
          >
            <div className="flex items-center justify-between">
              <span
                className="text-[10px] font-semibold leading-none"
                style={{ color: 'var(--color-text-primary)' }}
              >
                {t('workspace.createNewWorkspace')}
              </span>
              <X className="h-2.5 w-2.5" style={{ color: 'var(--color-text-primary)' }} />
            </div>
            <div className="mt-2.5 flex flex-col gap-1">
              {/* name, focused when the modal opens */}
              <FieldLabel required>{t('workspace.workspaceName')}</FieldLabel>
              <div className="rounded-[4px] border px-1.5 py-1" style={field(nameFocus)}>
                <span className={typed} style={{ color: 'var(--color-text-primary)' }}>
                  <TypedInPlace text={NEW_WS.name} at={NEW_WS.typeName} focus={nameFocus} />
                </span>
              </div>
              {/* description, a three-line box */}
              <div className="mt-1.5">
                <FieldLabel>{t('common.description')}</FieldLabel>
              </div>
              <div className="h-9 rounded-[4px] border px-1.5 py-1" style={field(descFocus)}>
                <span className={typed} style={{ color: 'var(--color-text-primary)' }}>
                  <TypedInPlace text={NEW_WS.desc} at={NEW_WS.typeDesc} focus={descFocus} />
                </span>
              </div>
              {/* files, added after the workspace is created */}
              <div className="mt-1.5">
                <FieldLabel>{t('workspace.files')}</FieldLabel>
              </div>
              <span
                className="text-[6px] leading-none"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                {t('workspace.filesUploadNote')}
              </span>
              <div
                className="mt-0.5 flex flex-col items-center gap-1 rounded-md border border-dashed py-2"
                style={{
                  borderColor: overDrop
                    ? 'var(--color-accent-primary)'
                    : 'var(--color-border-muted)',
                }}
              >
                <Upload className="h-3 w-3" style={{ color: 'var(--color-text-secondary)' }} />
                <span
                  className="text-[7px] leading-none"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {t('workspace.dragFilesHere')}
                  <span className="font-medium" style={{ color: 'var(--color-accent-primary)' }}>
                    {t('workspace.clickToBrowse')}
                  </span>
                </span>
              </div>
              <UploadRow name="capex-model-v3.xlsx" size="412 KB" at={NEW_WS.files[0]} />
              <UploadRow name="dc-buildout-notes.md" size="18 KB" at={NEW_WS.files[1]} />
              <div className="flex items-center justify-end gap-1.5 pt-1.5">
                <span
                  className="px-2 py-1 text-[8px] font-medium leading-none"
                  style={{ color: 'var(--color-text-primary)' }}
                >
                  {t('common.cancel')}
                </span>
                <span
                  className={cn(
                    'rounded-[4px] px-2 py-1 text-[8px] font-medium leading-none',
                    'transition-transform duration-150'
                  )}
                  style={{
                    background: 'var(--color-btn-primary-bg)',
                    color: 'var(--color-btn-primary-text)',
                    transform: pressAt(now, NEW_WS.clickCreate),
                  }}
                >
                  {t('common.create')}
                </span>
              </div>
            </div>
          </Appear>
        </div>
      </div>
    </Camera>
  );
}

export const workspaceScenes = {
  workspaceGrid: { Scene: WorkspaceGrid, length: GRID.length, anchor: 'center' },
  createWorkspace: { Scene: CreateWorkspace, length: NEW_WS.length, anchor: 'center' },
} satisfies Partial<Record<IntroVisualId, IntroScene>>;
