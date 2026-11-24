import {
  useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type Ref, type RefObject,
} from 'react';
import { Building2, Check, Settings, Star } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  Button, GridList, GridListHeader, GridListItem, GridListSection, ToggleButton, ToggleButtonGroup,
  useFilter, type Key,
} from 'react-aria-components';
import { SearchField } from './aria-search-field';
import { ComposerPopover, MENU_ROW, usePickToDraft } from './chat-input.composerPopover';
import { getModelDisplayName } from './chat-input.helpers';
import { cn } from '@/lib/utils';
import { llmProviderArt } from '@/lib/providerArt';
import { isClaudeCodeModel } from '@/lib/claudeCode';
import { CheckNowFor } from '@/components/modelAccess/PlanCheck';
import { PickerPlanMeter, type PickerMeter } from '@/components/modelAccess/PlanUsage';
import type { CheckTarget } from '@/components/modelAccess/usePlanCheck';
import { isImeComposing } from '@/lib/ime';
import { favoriteSections, type ModelConnection, type ModelGroup } from '@/lib/modelGroups';
import { isSubscription, subscriptionPayer, type Payer } from '@/lib/modelPayer';
import { usePayerLabel } from '@/hooks/usePayerLabel';
import { useAccount } from '@/hooks/useAccount';
import { MODEL_RULE_COPY, modelsRule } from '@/lib/accountRules';
import { useNow } from '@/hooks/useNow';
import type { ModelMetadataEntry, TaskCostBaseline } from '@/hooks/useFilteredModels';
import { ModelMark, MarkedModelName } from '@/components/model/ModelMark';
import { PayerBadge } from '@/components/model/PayerBadge';
// DALTON-HOOK(antigravity-series): collapse Antigravity variants into one row across tabs, search, favorites, recent
import type { CustomModelEntry } from '@/components/model/types';
import {
  favoriteAntigravitySeriesVariants,
  indexSelectableSeries,
  inspectSavedAntigravityModel,
  type AntigravitySeriesCatalog,
  type SelectableAntigravitySeries,
} from '@/lib/antigravitySeries';
import { CostMarksGlyph, TaskCostHeading } from '@/components/model/TaskCost';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './tooltip';
import { TuningPane, TuningStrip, type ModelTuning } from './chat-input.modelTuning';
import { catalogCostRange, costMarks, formatCostMultiple, type CostMarks } from '@/lib/taskCost';
import { effortLabelFor, modelHasTuning } from '@/lib/modelTuning';
import { useLocale } from '@/hooks/useLocale';

export const FAVORITES_TAB = '__favorites__';

type Metadata = Record<string, ModelMetadataEntry>;

/** What a row says about its model: a tag beside the name for one that is a
 *  pick, or, for one that is not, why, under its name where a sentence fits.
 *  A row held back by something the user can fix takes them there instead of
 *  picking, through `fix`. One a check may free offers Check now, through
 *  `check`, and stays enabled so the control is reached as a row's second
 *  one is, though a press on the row itself does nothing. */
export type ModelRowStatus = { tag?: string } | { unavailable: string; fix?: () => void; fixLabel?: string; check?: CheckTarget };

/** What the model list shows, which the pill's wrapper hands through whole. */
export interface ModelListProps {
  selectedModel: string | null;
  metadata?: Metadata;
  payers: Record<string, Payer>;
  groups: ModelGroup[];
  /** The favorites the user can reach now, in their saved order. */
  favorites: string[];
  /** This thread's models and the current one that are not favorites, listed
   *  under the favorites so the current model is always on the first tab. */
  recent: string[];
  onToggleFavorite: (model: string, favorite: boolean) => void;
  // DALTON-HOOK(antigravity-series): optional batch toggle for collapsed series favorites & series catalogs
  onToggleFavoriteMany?: (models: readonly string[], favorite: boolean) => void;
  seriesCatalog?: AntigravitySeriesCatalog;
  selectableSeries?: readonly SelectableAntigravitySeries[];
  rawMetadata?: Metadata;
  customModels?: readonly CustomModelEntry[];
  /** What the row says at `now`, the picker's minute clock, so a hold that
   *  ends while the picker is open ends there too. */
  rowStatus: (model: string, now: number) => ModelRowStatus;
  /** The Claude plan Claude Code's models run on here, which heads their
   *  tab, and Favorites while the current model is one of them. */
  planMeter?: PickerMeter;
  /** What each row's cost marks are measured against. */
  taskCostBaseline?: TaskCostBaseline | null;
}

/** The list's own width beside the pane: the rail and a row with room for
 *  most names, their cost and the star. A payer's tag sits on its section's
 *  header, or the tab's, so few rows carry one, and a longer name truncates
 *  and shows whole on hover. */
const LIST_WIDTH = 352;
/** The tuning pane beside it (`w-60`). */
const PANE_WIDTH = 240;

/**
 * The room the picker has, for whether its tuning fits beside the list: the
 * composer's width, which is what the picker opens over, and never more than
 * the viewport less its gutters. Zero while closed; read before paint when it
 * opens, so the first frame is laid out for the room there is. Where nothing
 * can be measured (jsdom) the composer reads as unbounded.
 */
function usePickerRoom(container: HTMLElement | null | undefined, isOpen: boolean): number {
  const [room, setRoom] = useState(0);
  useLayoutEffect(() => {
    if (!isOpen) return;
    const read = () => {
      const composer = container?.getBoundingClientRect().width || Infinity;
      setRoom(Math.min(composer, window.innerWidth - 32));
    };
    read();
    window.addEventListener('resize', read);
    const box = container && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(read) : null;
    if (container) box?.observe(container);
    return () => {
      window.removeEventListener('resize', read);
      box?.disconnect();
    };
  }, [container, isOpen]);
  return room;
}

/**
 * The composer's model picker: a rail of tabs, favorites first and then one
 * per connection, beside the list of the tab's models, and the current
 * model's tuning (its reasoning level, and speed on a Codex model) in a pane
 * beside the list where the composer has room for one, else in a strip under
 * it. Typing searches every model whatever the tab.
 *
 * The list is a grid rather than a menu because each row holds a second
 * control, the star. A pick that leaves the new model something to tune
 * keeps the picker open, so its tuning is right there; one that does not
 * closes it, as a level or a speed never does.
 */
export function ModelPicker({
  triggerRef, isOpen, onOpenChange, direction, container, draftRef, onPick, onManage, notice, tuning, disabled = false, ...list
}: ModelListProps & {
  /** A warning about the next send on the current model, heading the picker. */
  notice?: ReactNode;
  triggerRef: RefObject<HTMLButtonElement | null>;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  direction: 'up' | 'down';
  /** The composer, which a phone portals the picker into and whose width
   *  says whether the tuning fits beside the list. */
  container?: HTMLElement | null;
  /** The message the pick is for, which takes focus after a pick by pointer. */
  draftRef?: RefObject<HTMLElement | null>;
  onPick: (model: string) => void;
  onManage: () => void;
  /** The current model's tuning. Without it the picker lists models alone. */
  tuning?: ModelTuning;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const pickToDraft = usePickToDraft(isOpen, draftRef);
  const room = usePickerRoom(container, isOpen);
  const wide = !!tuning && room >= LIST_WIDTH + PANE_WIDTH;
  const { selectedModel, metadata, payers } = list;
  const currentMeta = selectedModel ? metadata?.[selectedModel] : undefined;
  const range = useMemo(() => catalogCostRange(metadata), [metadata]);
  const close = () => {
    pickToDraft();
    onOpenChange(false);
  };
  return (
    <ComposerPopover
      triggerRef={triggerRef}
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      direction={direction}
      align="end"
      container={container}
      label={t('chat.modelSelector.select')}
      // A height of its own, not its content's: it opens up from the pill, so
      // a pick whose model has more or fewer levels, or a search that thins
      // the list, would move its top and the rows under the pointer with it.
      // Tall enough for the longest pane; a short window still caps it.
      className={cn('h-[23rem]', wide ? 'w-[37rem]' : 'w-96')}
    >
      {notice}
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          <ModelList
            {...list}
            onPick={(model) => {
              const status = list.rowStatus(model, Date.now());
              if ('unavailable' in status) {
                if (!status.fix) return;
                onOpenChange(false);
                status.fix();
                return;
              }
              onPick(model);
              // Picking the current model again is how a pick is confirmed.
              // DALTON-HOOK(antigravity-series): keep picker open when picking a multi-effort Antigravity series so its effort pane stays reachable
              const pickedSeries = inspectSavedAntigravityModel(model, {
                seriesCatalog: list.seriesCatalog,
                rawMetadata: list.rawMetadata ?? metadata,
                customModels: list.customModels,
              });
              const pickedHasSeriesTuning = Boolean(
                pickedSeries && (list.selectableSeries?.find((s) => s.seriesId === pickedSeries.series.series_id)?.efforts.length ?? 0) > 1,
              );
              if (!tuning || model === selectedModel || (!modelHasTuning(metadata?.[model]) && !pickedHasSeriesTuning)) close();
            }}
            onManage={() => {
              onOpenChange(false);
              onManage();
            }}
          />
          {tuning && !wide && <TuningStrip tuning={tuning} meta={currentMeta} disabled={disabled} />}
        </div>
        {tuning && wide && (
          <TuningPane
            tuning={tuning}
            meta={currentMeta}
            payer={selectedModel ? payers[selectedModel] : undefined}
            range={range}
            disabled={disabled}
          />
        )}
      </div>
    </ComposerPopover>
  );
}

/** What the list shows: a search across every model, or one tab. */
// DALTON-HOOK(antigravity-series): collapsed row descriptor used across brand/search/favorites/recent views
interface PickerRow {
  rowKey: string;
  rawModel: string;
  displayName: string;
  provider?: string;
  selected: boolean;
  favorite: boolean;
  favoriteKeys: string[];
}

type View =
  | { kind: 'search'; rows: PickerRow[] }
  | { kind: 'favorites'; rows: PickerRow[]; recent: PickerRow[] }
  | { kind: 'brand'; group: ModelGroup; connection: ModelConnection; rows: PickerRow[] };

function collapseToRows({
  models,
  selectedModel,
  metadata,
  favoriteSet,
  seriesCatalog,
  selectableById,
  rawMetadata,
  customModels,
  seenSeries,
}: {
  models: readonly string[];
  selectedModel: string | null;
  metadata?: Metadata;
  favoriteSet: ReadonlySet<string>;
  seriesCatalog?: AntigravitySeriesCatalog;
  selectableById: Readonly<Record<string, SelectableAntigravitySeries>>;
  rawMetadata?: Metadata;
  customModels?: readonly CustomModelEntry[];
  seenSeries: Set<string>;
}): PickerRow[] {
  const seriesOpts = { seriesCatalog, rawMetadata: rawMetadata ?? metadata, customModels };
  const selectedSeriesId = inspectSavedAntigravityModel(selectedModel, seriesOpts)?.series.series_id ?? null;
  const rows: PickerRow[] = [];
  for (const m of models) {
    const hit = inspectSavedAntigravityModel(m, seriesOpts);
    if (!hit) {
      rows.push({
        rowKey: m,
        rawModel: m,
        displayName: getModelDisplayName(m, metadata),
        provider: metadata?.[m]?.provider,
        selected: m === selectedModel,
        favorite: favoriteSet.has(m),
        favoriteKeys: [m],
      });
      continue;
    }
    const sid = hit.series.series_id;
    if (seenSeries.has(sid)) continue;
    seenSeries.add(sid);
    const sel = selectableById[sid];
    const isSelected = selectedSeriesId === sid;
    const pickKey = isSelected && selectedModel
      ? selectedModel
      : sel
        ? sel.defaultVariantKey
        : m;
    // DALTON-HOOK(antigravity-series): exclude same-name custom models when aggregating/removing series favorites
    const favVariants = favoriteAntigravitySeriesVariants(hit.series, favoriteSet, seriesOpts);
    const isFav = favVariants.length > 0;
    rows.push({
      rowKey: pickKey,
      rawModel: pickKey,
      displayName: hit.displayName,
      provider: hit.series.provider,
      selected: isSelected,
      favorite: isFav,
      favoriteKeys: isFav ? favVariants : [pickKey],
    });
  }
  return rows;
}

// DALTON-HOOK(antigravity-series): collapse Antigravity series variants in viewOf tabs & search
function viewOf({
  query, tab, connectionTab, groups, favorites, recent, selectedModel, matches,
  metadata, favoriteSet, seriesCatalog, selectableById, rawMetadata, customModels,
}: {
  query: string;
  tab: string | null;
  connectionTab: string | null;
  groups: ModelGroup[];
  favorites: string[];
  recent: string[];
  selectedModel: string | null;
  matches: (displayName: string) => boolean;
  metadata?: Metadata;
  favoriteSet: ReadonlySet<string>;
  seriesCatalog?: AntigravitySeriesCatalog;
  selectableById: Readonly<Record<string, SelectableAntigravitySeries>>;
  rawMetadata?: Metadata;
  customModels?: readonly CustomModelEntry[];
}): View {
  const toRows = (list: readonly string[], seenSeries = new Set<string>()) => collapseToRows({
    models: list,
    selectedModel,
    metadata,
    favoriteSet,
    seriesCatalog,
    selectableById,
    rawMetadata,
    customModels,
    seenSeries,
  });
  if (query.trim()) {
    const all = new Set(groups.flatMap((g) => g.connections.flatMap((c) => c.models)));
    return { kind: 'search', rows: toRows([...all]).filter((r) => matches(r.displayName)) };
  }
  // The first tab holds the current model, so it opens there unless it has
  // nothing at all to show. A brand that has since gone falls back to it too.
  const group = tab === null
    ? (favorites.length + recent.length > 0 ? undefined : groups[0])
    : groups.find((g) => g.id === tab);
  if (!group) {
    const seen = new Set<string>();
    return { kind: 'favorites', rows: toRows(favorites, seen), recent: toRows(recent, seen) };
  }
  // Within a brand it opens on the way the current model is paid for, else
  // on the user's own before the deployment's.
  const connection = group.connections.find((c) => c.id === connectionTab)
    ?? group.connections.find((c) => !!selectedModel && c.models.includes(selectedModel))
    ?? group.connections[0];
  return { kind: 'brand', group, connection, rows: toRows(connection.models) };
}

/** The picker's content, mounted while it is open, so the search, the tab and
 *  the brand's payer start over on each opening. */
// DALTON-HOOK(antigravity-series): wire selectableSeries & customModels into ModelList
function ModelList({
  selectedModel, metadata, payers, groups, favorites, recent, onToggleFavorite, onToggleFavoriteMany,
  seriesCatalog, selectableSeries, rawMetadata, customModels, rowStatus, planMeter, onPick, onManage,
  taskCostBaseline,
}: ModelListProps & { onPick: (model: string) => void; onManage: () => void }) {
  const { t } = useTranslation();
  const costLabel = useCostMarksLabel(taskCostBaseline, metadata);
  const payerLabel = usePayerLabel();
  const now = useNow();
  // A rule that narrows the models is named once under the list, so a model
  // the user remembers and cannot find here is explained.
  const account = useAccount();
  const narrowedBy = modelsRule(account.policy);
  const [query, setQuery] = useState('');
  const [tab, setTab] = useState<string | null>(null);
  const [connectionTab, setConnectionTab] = useState<string | null>(null);
  const { contains } = useFilter({ sensitivity: 'base' });
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const firstRowRef = useRef<HTMLDivElement | null>(null);
  const currentRowRef = useRef<HTMLDivElement | null>(null);
  const { attach: attachRail, style: railFade } = useFadeAtEnd<HTMLDivElement>();
  const { attach: attachList, style: listFade } = useFadeAtEnd<HTMLDivElement>();
  const favoriteSet = new Set(favorites);
  const selectableById = indexSelectableSeries(selectableSeries);

  // DALTON-HOOK(antigravity-series): pass series & customModels context into viewOf
  const view = viewOf({
    query, tab, connectionTab, groups, favorites, recent, selectedModel,
    matches: (displayName) => contains(displayName, query),
    metadata,
    favoriteSet,
    seriesCatalog,
    selectableById,
    rawMetadata,
    customModels,
  });
  const header = view.kind === 'favorites' ? t('chat.modelSelector.favorites')
    : view.kind === 'brand' ? view.group.label
    : null;
  // A connection's tag sits once above its models rather than on each row.
  const headerPayer = view.kind === 'brand' ? view.connection.payer : undefined;
  // The tab's own subscription (Claude Code's) is named by its header already.
  const headerNamesPayer = view.kind === 'brand' && headerPayer === subscriptionPayer(view.group.id);
  const inThread = view.kind === 'favorites' ? view.recent : [];
  // Favorites by who pays, each payer's run kept together by brand; the
  // section's header names the payer, so its rows carry no badge of their own.
  // DALTON-HOOK(antigravity-series): group collapsed favorite PickerRows by payer
  const rowByRaw = new Map(view.rows.map((r) => [r.rawModel, r]));
  const sections = view.kind === 'favorites'
    ? favoriteSections(view.rows.map((r) => r.rawModel), payers, groups).map((section) => ({
      payer: section.payer,
      rows: section.models.map((m) => rowByRaw.get(m)).filter((r): r is PickerRow => r !== undefined),
    }))
    : null;
  const rows = sections ? sections.flatMap((section) => section.rows) : view.rows;
  const first = [...rows, ...inThread].find((r) => !('unavailable' in rowStatus(r.rawModel, now)))?.rowKey;
  // Claude Code's plan heads the tab of its models, and Favorites while the
  // current model is one of them.
  const isClaudeCode = (m: string | null) => !!m && isClaudeCodeModel(metadata?.[m]);
  const meter = (view.kind === 'brand' && view.connection.models.some(isClaudeCode))
    || (view.kind === 'favorites' && isClaudeCode(selectedModel)) ? planMeter : undefined;
  const marksOf = (m: string) => costMarks(metadata?.[m], payers[m]);
  // The column's heading, over the marks, wherever a row on the tab has some.
  const showCosts = [...view.rows, ...inThread].some((r) => marksOf(r.rawModel) !== null);

  // The current model's check is what the user looks for on opening, and a
  // long first tab can hold it below the fold.
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const row = currentRowRef.current;
      const list = row?.closest<HTMLElement>('[role="grid"]');
      if (!row || !list) return;
      const view = list.getBoundingClientRect();
      const at = row.getBoundingClientRect();
      if (at.top >= view.top && at.bottom <= view.bottom) return;
      // The panel scales in as it opens, and what the screen measures is
      // scaled with it; scrolling is in the list's own pixels.
      const scale = view.height / list.offsetHeight || 1;
      // From its section's top, so the header saying who pays comes into view
      // with it, unless that would leave the row below the fold.
      const section = row.closest('[role="rowgroup"]')?.getBoundingClientRect();
      if (section && (at.bottom - section.top) / scale <= list.clientHeight) {
        list.scrollTop += (section.top - view.top) / scale;
      } else {
        row.scrollIntoView({ block: 'nearest' });
      }
    });
    return () => cancelAnimationFrame(frame);
  }, []);

  const chooseTab = (keys: Set<Key>) => {
    const [next] = keys;
    if (next === undefined) return;
    setTab(String(next));
    setConnectionTab(null);
    setQuery('');
    // The search field holds focus so the arrow keys keep moving through the
    // list, whichever tab was clicked.
    inputRef.current?.focus();
  };

  // DALTON-HOOK(antigravity-series): unfavorite all variants in collapsed series row at once
  const toggleFav = (r: PickerRow) => {
    const next = !r.favorite;
    if (!next && r.favoriteKeys.length > 1 && onToggleFavoriteMany) {
      onToggleFavoriteMany(r.favoriteKeys, false);
    } else {
      onToggleFavorite(r.favoriteKeys[0] ?? r.rowKey, next);
    }
  };

  // DALTON-HOOK(antigravity-series): render collapsed PickerRow with batch-unfavorite callback
  const row = (r: PickerRow, badged = !headerPayer) => (
    <ModelRow
      key={r.rowKey}
      model={r.rowKey}
      displayName={r.displayName}
      provider={r.provider}
      // The first row takes Down from the search; the current one is
      // scrolled to on opening, which a first row never needs.
      rowRef={r.rowKey === first ? firstRowRef : r.selected ? currentRowRef : undefined}
      payer={badged ? payers[r.rawModel] : undefined}
      selected={r.selected}
      favorite={r.favorite}
      status={rowStatus(r.rawModel, now)}
      marks={marksOf(r.rawModel)}
      costColumn={showCosts}
      costLabel={costLabel}
      onToggleFavorite={() => toggleFav(r)}
    />
  );

  return (
    <>
      <div className="rule-b flex flex-none items-center pr-2">
        {/* A grid takes no virtual focus from a search field, so the field
            hands over itself: Down moves into the list, and Enter picks the
            top row. Any other key goes on, so Escape still closes. */}
        <SearchField
          aria-label={t('chat.modelSelector.search')}
          aria-controls={listId}
          placeholder={t('chat.modelSelector.search')}
          value={query}
          onChange={setQuery}
          inputRef={inputRef}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown' && firstRowRef.current) {
              e.preventDefault();
              firstRowRef.current.focus();
            } else if (e.key === 'Enter' && !isImeComposing(e) && first) {
              e.preventDefault();
              onPick(first);
            } else {
              e.continuePropagation();
            }
          }}
          autoFocus
          ruled={false}
          className="min-w-0 flex-1"
        />
        <Button
          aria-label={t('chat.modelSelector.manageModels')}
          onPress={onManage}
          className={cn(
            'flex h-7 w-7 flex-none items-center justify-center rounded-md text-(--color-text-tertiary) outline-hidden',
            'data-hovered:bg-(--color-bg-tag) data-hovered:text-(--color-text-secondary) data-focus-visible:outline-current',
          )}
        >
          <Settings aria-hidden className="h-4 w-4" />
        </Button>
      </div>
      {/* It takes the panel's height and is the first part to give way: a
          notice and the levels strip under it both have to fit in the fixed
          panel, and on a phone the strip is the only place to set a level. It
          keeps three rows (2rem each), so a cut row still says there is more. */}
      <div className="flex min-h-24 flex-[1_1_11.5rem]">
        {/* Out of the flow, so a long rail scrolls rather than sets the
            height. */}
        <div className="relative w-11 flex-none border-r border-[color:var(--color-shell-divider,var(--color-border-muted))]">
          {/* The toggle group takes no ref, so a box of its own scrolls it. */}
          <div
            ref={attachRail}
            style={railFade}
            className="absolute inset-0 overflow-y-auto [scrollbar-width:none]"
          >
            <ToggleButtonGroup
              aria-label={t('chat.modelSelector.groups')}
              orientation="vertical"
              selectionMode="single"
              selectedKeys={view.kind === 'search' ? [] : [view.kind === 'brand' ? view.group.id : FAVORITES_TAB]}
              onSelectionChange={chooseTab}
              className="flex flex-col items-center gap-1 py-1.5"
            >
              <RailTab id={FAVORITES_TAB} label={t('chat.modelSelector.favorites')}>
                <Star aria-hidden className="h-4 w-4 text-(--color-text-tertiary)" />
              </RailTab>
              {groups.map((g) => (
                <RailTab
                  key={g.id}
                  id={g.id}
                  label={g.label}
                  dot={g.connections.some((c) => isSubscription(c.payer) && c.models.some((m) => !('unavailable' in rowStatus(m, now))))}
                >
                  <RailIcon provider={g.id} label={g.label} />
                </RailTab>
              ))}
            </ToggleButtonGroup>
          </div>
        </div>
        <div className="flex min-w-0 flex-1 flex-col">
          {header && (
            // The list's gutter too, so the cost heading stands over the marks
            // whether or not a scrollbar takes room beside the rows.
            <div className="flex flex-none items-center gap-2 overflow-hidden px-3 pb-1 pt-2 text-xs text-(--color-text-tertiary) [scrollbar-gutter:stable]">
              {meter
                ? <span className="min-w-0 flex-1"><PickerPlanMeter meter={meter} /></span>
                : <span data-slot="menu-label" className="min-w-0 flex-1 truncate">{header}</span>}
              {view.kind === 'brand' && view.group.connections.length > 1 ? (
                <ToggleButtonGroup
                  aria-label={t('chat.modelSelector.paidBy')}
                  selectionMode="single"
                  disallowEmptySelection
                  selectedKeys={[view.connection.id]}
                  onSelectionChange={(keys) => {
                    const [next] = keys;
                    if (next !== undefined) setConnectionTab(String(next));
                  }}
                  // One of the segmented controls, which the open card
                  // styles draw without a track (styles/cardStyle.css).
                  data-slot="segmented"
                  className="flex flex-none gap-0.5 rounded-md bg-[var(--color-shell-fill,var(--color-bg-tag))] p-0.5"
                >
                  {view.group.connections.map((c) => (
                    <ToggleButton
                      key={c.id}
                      id={c.id}
                      className={cn(
                        'rounded px-1.5 py-px text-[0.625rem] font-medium leading-normal text-(--color-text-tertiary) outline-hidden',
                        'data-hovered:text-(--color-text-secondary) data-selected:bg-[var(--color-shell-fill,var(--color-bg-elevated))] data-selected:text-(--color-text-primary)',
                        'data-focus-visible:outline-current',
                      )}
                    >
                      {c.payer ? payerLabel(c.payer) : t('chat.modelSelector.unbilled')}
                    </ToggleButton>
                  ))}
                </ToggleButtonGroup>
              ) : (
                <PayerBadge payer={headerNamesPayer ? undefined : headerPayer} />
              )}
              {/* Over the marks, which end each row at its edge. */}
              {showCosts && (
                <TaskCostHeading note={t('chat.modelSelector.costMarksNote')} />
              )}
            </div>
          )}
          <TooltipProvider delayDuration={300}>
          <GridList
            id={listId}
            ref={attachList}
            style={listFade}
            aria-label={header ?? t('chat.modelSelector.search')}
            onAction={(key) => onPick(String(key))}
            className="min-h-0 flex-1 overflow-y-auto p-1 outline-hidden [scrollbar-gutter:stable]"
            renderEmptyState={() => (
              <p className="px-3 py-4 text-center text-[0.8125rem] text-(--color-text-tertiary)">
                {view.kind === 'favorites' ? t('chat.modelSelector.noFavorites') : t('chat.modelSelector.noMatch')}
              </p>
            )}
          >
            {/* DALTON-HOOK(antigravity-series): render collapsed favorite, brand, search, and in-thread PickerRows */}
            {sections
              ? sections.map((section) => (
                <GridListSection key={section.payer ?? ''}>
                  <GridListHeader
                    data-slot="menu-label"
                    className="px-2 pb-1 pt-2.5 text-xs font-medium text-(--color-text-secondary)"
                  >
                    {section.payer ? payerLabel(section.payer) : t('chat.modelSelector.unbilled')}
                  </GridListHeader>
                  {section.rows.map((r) => row(r, false))}
                </GridListSection>
              ))
              : view.rows.map((r) => row(r))}
            {inThread.length > 0 && (
              <GridListSection>
                <GridListHeader data-slot="menu-label" className="px-2 pb-1 pt-2.5 text-xs text-(--color-text-tertiary)">
                  {t('chat.modelSelector.inThread')}
                </GridListHeader>
                {inThread.map((r) => row(r))}
              </GridListSection>
            )}
          </GridList>
          </TooltipProvider>
        </div>
      </div>
      {narrowedBy && (
        <p className="rule-t flex flex-none items-start gap-2 px-3 py-2 text-xs text-(--color-text-tertiary)">
          <Building2 aria-hidden className="mt-px h-3.5 w-3.5 flex-none" />
          <span className="min-w-0">
            {t(`organization.picker.foot.${MODEL_RULE_COPY[narrowedBy]}`, { who: account.setterName(narrowedBy).start })}
          </span>
        </p>
      )}
    </>
  );
}

/**
 * Fades a scrolling list's bottom edge while there is more below it, so a
 * cut row reads as a list going on rather than as a clipped one.
 */
function useFadeAtEnd<T extends HTMLElement>() {
  // A callback ref held as state, so the effect reruns for a new element.
  const [el, attach] = useState<T | null>(null);
  const [more, setMore] = useState(false);
  useEffect(() => {
    if (!el) return;
    const update = () => setMore(el.scrollTop + el.clientHeight < el.scrollHeight - 1);
    update();
    el.addEventListener('scroll', update, { passive: true });
    // A tab switch or a search changes the rows, not the box.
    const rows = new MutationObserver(update);
    rows.observe(el, { childList: true, subtree: true });
    const box = new ResizeObserver(update);
    box.observe(el);
    return () => {
      el.removeEventListener('scroll', update);
      rows.disconnect();
      box.disconnect();
    };
  }, [el]);
  const style: CSSProperties | undefined = more ? { maskImage: FADE, WebkitMaskImage: FADE } : undefined;
  return { attach, style };
}

// A hint, not a curtain: the last row dims rather than disappears.
const FADE = 'linear-gradient(to bottom, #000 calc(100% - 1.5rem), rgb(0 0 0 / 0.35))';

function RailTab({ id, label, dot = false, children }: {
  id: string;
  label: string;
  /** Marks a brand the user reaches through a subscription that can run one
   *  of its models now. */
  dot?: boolean;
  children: ReactNode;
}) {
  return (
    <ToggleButton
      id={id}
      aria-label={label}
      className={cn(
        'relative flex h-8 w-8 flex-none items-center justify-center rounded-[var(--cs-radius-control,var(--radius-lg))] outline-hidden',
        // The open card styles pick a provider with the front tab's edge, not a fill.
        // The rail's gap-1 keeps a chosen tab and a hovered neighbour apart.
        'data-hovered:bg-(--color-bg-hover) data-selected:bg-[color:var(--color-shell-fill,var(--color-bg-tag))]',
        'data-selected:shadow-[inset_0_0_0_1px_var(--color-shell-front,transparent)]',
        'data-focus-visible:outline-current',
      )}
    >
      <span title={label} className="flex items-center justify-center">{children}</span>
      {dot && (
        <span
          aria-hidden
          className="absolute bottom-1 right-1 h-1.5 w-1.5 rounded-full bg-(--color-success) ring-[1.5px] ring-(--color-bg-elevated)"
        />
      )}
    </ToggleButton>
  );
}

/** A provider's mark, or its initial for one we ship no art for: unlike a
 *  mark beside a name, a rail tab has nothing else to say what it is. */
function RailIcon({ provider, label }: { provider: string; label: string }) {
  if (llmProviderArt(provider)) return <ModelMark provider={provider} className="h-4 w-4" />;
  return (
    <span aria-hidden className="text-xs font-semibold uppercase text-(--color-text-tertiary)">
      {label.charAt(0)}
    </span>
  );
}

/**
 * What a row's cost marks say, for their tooltip and a screen reader: the
 * model's figure against the baseline model, named, at its level; a bound
 * said as one; one from the list price said to be an estimate.
 */
function useCostMarksLabel(baseline: TaskCostBaseline | null | undefined, metadata?: Metadata) {
  const { t } = useTranslation();
  const locale = useLocale();
  const model = baseline ? getModelDisplayName(baseline.model, metadata) : t('chat.modelSelector.costBaselineFallback');
  const level = baseline ? effortLabelFor(t, baseline.effort) : null;
  return (marks: CostMarks) => {
    if (marks.kind === 'estimate') return t('chat.modelSelector.costMarksEstimate');
    const args = { value: formatCostMultiple(marks.value, locale), model, level };
    const key = marks.kind === 'measured' ? 'costMarksMeasured' : 'costMarksBound';
    return t(`chat.modelSelector.${key}${level ? 'AtLevel' : ''}`, args);
  };
}

// DALTON-HOOK(antigravity-series): render pre-resolved series or model display name on row
function ModelRow({ model, displayName, provider, rowRef, payer, selected, favorite, status, marks, costColumn = false, costLabel, onToggleFavorite }: {
  model: string;
  displayName: string;
  provider?: string;
  rowRef?: Ref<HTMLDivElement>;
  payer?: Payer;
  selected: boolean;
  favorite: boolean;
  status: ModelRowStatus;
  marks: CostMarks | null;
  /** The tab shows costs, so a row without marks keeps their room and its
   *  star stays in the column. */
  costColumn?: boolean;
  costLabel: (marks: CostMarks) => string;
  onToggleFavorite: () => void;
}) {
  const { t } = useTranslation();
  const name = displayName;
  const reason = 'unavailable' in status ? status.unavailable : undefined;
  const check = 'unavailable' in status ? status.check : undefined;
  const fixLabel = 'unavailable' in status && status.fix ? status.fixLabel : undefined;
  const actionable = 'unavailable' in status && (!!status.fix || !!check);
  const tag = 'tag' in status ? status.tag : undefined;
  const cost = marks ? costLabel(marks) : undefined;
  return (
    <GridListItem
      ref={rowRef}
      id={model}
      // A row a reason holds back says it as part of its name, which is all a
      // screen reader reads of a row; so does its cost, after the name, where
      // typing a name to find the row still matches.
      textValue={[name, reason, fixLabel, cost].filter(Boolean).join(', ')}
      isDisabled={reason !== undefined && !actionable}
      data-slot="menu-item"
      className={cn('group', MENU_ROW, 'data-disabled:text-(--color-text-tertiary)', actionable && 'text-(--color-text-tertiary)')}
    >
      {/* One line holds the name and everything after it, so the check and
          the star sit level with the name whether or not a reason goes under. */}
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-center gap-2">
          <span className="flex min-w-0 flex-1 items-center gap-2">
            <MarkedModelName provider={provider} name={name} nameClassName="min-w-0 truncate" wholeOnHover />
          </span>
          <PayerBadge payer={payer} markLegacy={false} />
          {tag && <span className="flex-none text-xs text-(--color-text-tertiary)">{tag}</span>}
          {/* The check and the star share one place: the current model shows
              its check until the row is hovered or reached by keyboard, when the star
              takes it to be toggled. A touch screen hovers nothing, so there
              the current row shows both. */}
          {/* DALTON-HOOK(antigravity-series): unfavorite button triggers collapsed variant cleanup */}
          <span className="flex flex-none items-center gap-2">
            {selected && (
              <>
                <Check
                  aria-hidden
                  className="mx-0.5 h-4 w-4 flex-none text-(--color-accent-primary) group-data-focus-visible:hidden group-data-hovered:hidden [@media(hover:none)]:block!"
                />
                <span className="sr-only">{t('chat.modelSelector.current')}</span>
              </>
            )}
            <Button
              aria-label={t(favorite ? 'chat.modelSelector.unfavorite' : 'chat.modelSelector.favorite', { model: name })}
              onPress={onToggleFavorite}
              className={cn(
                'h-5 w-5 flex-none items-center justify-center rounded outline-hidden data-focus-visible:outline-current',
                selected
                  ? 'hidden group-data-focus-visible:flex group-data-hovered:flex data-focus-visible:flex [@media(hover:none)]:flex'
                  : 'flex',
                favorite
                  ? 'text-(--color-text-secondary)'
                  // Shown on the row's hover or focus where there is a pointer to
                  // hover with. A touch screen has none, so it stays shown rather
                  // than sit there invisible and still take a tap.
                  : 'text-(--color-text-tertiary) opacity-0 group-data-focus-visible:opacity-100 group-data-hovered:opacity-100 data-focus-visible:opacity-100 [@media(hover:none)]:opacity-100',
              )}
            >
              <Star aria-hidden className="h-3.5 w-3.5" fill={favorite ? 'currentColor' : 'none'} />
            </Button>
          </span>
          {/* Last, against the row's edge, under their heading. */}
          {marks && cost ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span data-cost-marks={marks.kind} className="flex-none cursor-default">
                  <CostMarksGlyph marks={marks} />
                </span>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-60">{cost}</TooltipContent>
            </Tooltip>
          ) : costColumn && (
            <CostMarksGlyph marks={null} />
          )}
        </span>
        {/* Under the name itself, past the mark and its gap, and clear of the
            star's column, which Check now takes on this line where it is offered. */}
        {reason && (
          <span className={cn('flex items-start gap-2 text-xs text-(--color-text-tertiary)', !check && 'pe-7', provider && llmProviderArt(provider) && 'ps-[1.375rem]')}>
            <span className="min-w-0 flex-1 text-pretty">
              {reason}
              {/* Picking the row runs the fix; the label says where it goes. */}
              {fixLabel && <span aria-hidden="true" className="font-medium text-(--color-accent-primary)"> {fixLabel} →</span>}
            </span>
            {check && <CheckNowFor target={check} icon />}
          </span>
        )}
      </span>
    </GridListItem>
  );
}
