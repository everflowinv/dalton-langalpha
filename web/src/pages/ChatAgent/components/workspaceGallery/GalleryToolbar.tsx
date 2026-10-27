/**
 * The gallery's one toolbar row: search, the computer (a chip naming the one
 * there is, or a filter once there are two), sort. Custom is the order cards
 * are dragged into, and its menu item says so.
 * Below a laptop's width the search takes the first line and the controls the
 * next, filling it; on a wide screen the search stops at a field's width and
 * the controls stay beside it.
 */
import { useId } from 'react';
import { ArrowDownUp, Check, ChevronDown, Search, Server } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { Computer } from '@/types/api';

import { ComputerBadge } from '../ComputerBadge';
import { ComputerStatusIndicator } from '../computerStatusUi';
import { isComputerPreparing } from '../../utils/computerStatus';
import { ComputerChip } from './ComputerChip';
import { DiskPercent } from './DiskPercent';

export type GallerySort = 'activity' | 'name' | 'custom';

/** The sort menu's options, in its order. */
export const GALLERY_SORTS: readonly GallerySort[] = ['activity', 'name', 'custom'];

/** The filter's value for every computer. */
export const ALL_COMPUTERS = 'all';

const CONTROL = 'flex h-9 items-center gap-1.5 rounded-lg border px-2.5 text-sm transition-colors hover:bg-foreground/5';
const CONTROL_STYLE = { borderColor: 'var(--color-border-muted)', color: 'var(--color-text-secondary)' };

interface GalleryToolbarProps {
  search: string;
  onSearchChange: (value: string) => void;
  /** The machines the filter offers when there are two or more; one is a chip. */
  computers: readonly Computer[];
  isThisMac: (computer: Computer) => boolean;
  computerFilter: string;
  onComputerFilterChange: (computerId: string) => void;
  sortBy: GallerySort;
  onSortChange: (sort: GallerySort) => void;
  /** Open the Computers dialog on a machine, from the one-computer chip. */
  onOpenComputer: (computerId: string) => void;
}

export function GalleryToolbar({
  search,
  onSearchChange,
  computers,
  isThisMac,
  computerFilter,
  onComputerFilterChange,
  sortBy,
  onSortChange,
  onOpenComputer,
}: GalleryToolbarProps) {
  const { t } = useTranslation();
  const customHintId = useId();
  const sortLabels: Record<GallerySort, string> = {
    activity: t('workspace.sortRecent', 'Recent'),
    name: t('common.name'),
    custom: t('workspace.custom'),
  };
  const filtered = computers.find((c) => c.computer_id === computerFilter) ?? null;
  const filterLabel = filtered ? filtered.name : t('workspace.allComputers', 'All computers');

  return (
    <div className="flex flex-wrap items-center gap-2 md:flex-nowrap" data-testid="gallery-toolbar">
      {/* The pill rings for the field inside it: a ring drawn on the field
          alone would cut this border and leave the icon outside the
          indicator. `rings-within` in tokens.css owns the rule. */}
      <div
        className="rings-within owns-its-edge flex h-9 min-w-0 basis-full items-center gap-2 rounded-lg border px-3 transition-colors md:max-w-md md:flex-1 md:basis-auto"
        style={{ backgroundColor: 'var(--color-bg-input)', borderColor: 'var(--color-border-muted)' }}
      >
        <Search className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
        <input
          type="search"
          className="w-full bg-transparent text-base sm:text-sm"
          style={{ color: 'var(--color-text-primary)' }}
          placeholder={t('workspace.searchWorkspaces')}
          aria-label={t('workspace.searchWorkspaces')}
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
        />
      </div>

      {computers.length === 1 && (
        <ComputerChip
          computer={computers[0]}
          isThisMac={isThisMac(computers[0])}
          onOpen={onOpenComputer}
          // Below a laptop's width it fills the second line beside sort, and its
          // name gives way rather than pushing sort onto a third.
          className={`${CONTROL} min-w-0 flex-1 basis-0 md:max-w-[22rem] md:flex-none md:basis-auto`}
          style={CONTROL_STYLE}
        />
      )}

      {computers.length > 1 && (
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className={`${CONTROL} min-w-0 flex-1 basis-0 md:max-w-[14rem] md:flex-none md:basis-auto`}
              style={CONTROL_STYLE}
              aria-label={`${t('workspace.filterByComputer', 'Filter by computer')}: ${filterLabel}`}
              data-testid="computer-filter"
            >
              <Server className="h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1 truncate text-left">{filterLabel}</span>
              <ChevronDown className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-56">
            <DropdownMenuRadioGroup value={filtered ? filtered.computer_id : ALL_COMPUTERS} onValueChange={onComputerFilterChange}>
              <DropdownMenuRadioItem value={ALL_COMPUTERS}>
                <Mark on={!filtered} />
                {t('workspace.allComputers', 'All computers')}
              </DropdownMenuRadioItem>
              <DropdownMenuSeparator />
              {computers.map((c) => (
                <DropdownMenuRadioItem key={c.computer_id} value={c.computer_id}>
                  <Mark on={filtered?.computer_id === c.computer_id} />
                  <ComputerStatusIndicator status={c.status} preparing={isComputerPreparing(c)} glyphSize={10} glyphOnly className="shrink-0" />
                  <span className="min-w-0 flex-1 truncate">{c.name}</span>
                  <ComputerBadge computer={c} isThisMac={isThisMac(c)} name={c.name} />
                  <DiskPercent computer={c} className="text-xs" />
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={`${CONTROL} shrink-0`}
            style={CONTROL_STYLE}
            aria-label={`${t('workspace.sortBy')}: ${sortLabels[sortBy]}`}
            data-testid="gallery-sort"
          >
            <ArrowDownUp className="h-4 w-4 shrink-0" />
            <span>{sortLabels[sortBy]}</span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-40">
          <DropdownMenuRadioGroup value={sortBy} onValueChange={(v) => onSortChange(v as GallerySort)}>
            {GALLERY_SORTS.map((id) => (
              <DropdownMenuRadioItem
                key={id}
                value={id}
                className="items-start"
                // The hint is read as a description, so the item's name stays the order's.
                aria-label={sortLabels[id]}
                aria-describedby={id === 'custom' ? customHintId : undefined}
              >
                <Mark on={sortBy === id} className="mt-0.5" />
                {id === 'custom' ? (
                  <span className="flex flex-col">
                    <span>{sortLabels[id]}</span>
                    <span id={customHintId} className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                      {t('workspace.customSortHint')}
                    </span>
                  </span>
                ) : sortLabels[id]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/** The chosen row's check, in the column the other rows leave empty. */
function Mark({ on, className = '' }: { on: boolean; className?: string }) {
  return <Check className={`h-3.5 w-3.5 shrink-0 ${className}`} style={{ opacity: on ? 1 : 0 }} aria-hidden="true" />;
}

export default GalleryToolbar;
