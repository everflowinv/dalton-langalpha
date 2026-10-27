/**
 * The near-full disk warning for one computer.
 *
 * Every workspace now lives on one shared machine, so files from all of them
 * fill the same disk, and the moment it fills, saving a file or running code
 * fails in whichever workspace happens to be active. The warning therefore
 * shows wherever the user works (the gallery and every chat on that machine).
 * Its call to action is the spec change. Whether the plan allows a bigger tier
 * is the dialog's to say, so the banner never guesses at it. At `critical` the
 * storage breakdown also lets the user delete by hand.
 */
import { useEffect, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, HardDrive, X } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { useLocale } from '@/hooks/useLocale';
import { formatBytes } from '@/lib/format';
import { cn } from '@/lib/utils';
import { createValueStore } from '@/lib/valueStore';
import type { Computer, ComputerDiskLevel } from '@/types/api';

import { providerManagesSpec } from './providerUi';
import { openComputerSpec, openComputersPanel } from '../hooks/computerPanelStore';
import { diskLevelRank, isDiskAlertLevel, nextTier } from './computerDiskUi';
import { activeSpecChange, effectiveTier, SpecChangeProgress } from './specChangeUi';

// --- Dismissal, per computer for the browser session --------------------------

type Dismissals = Record<string, ComputerDiskLevel>;

const DISMISS_KEY = 'la:disk-warning-dismissed';

function readDismissed(): Dismissals {
  try {
    const raw = sessionStorage.getItem(DISMISS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === 'object' ? (parsed as Dismissals) : {};
  } catch {
    return {};
  }
}

const dismissedStore = createValueStore<Dismissals>(readDismissed());

function writeDismissed(next: Dismissals) {
  dismissedStore.set(next);
  try {
    sessionStorage.setItem(DISMISS_KEY, JSON.stringify(next));
  } catch {
    // Storage blocked: the dismissal still holds for this page's life.
  }
}

function dismiss(computerId: string, level: ComputerDiskLevel) {
  writeDismissed({ ...dismissedStore.get(), [computerId]: level });
}

/**
 * Forget a dismissal once its computer's disk has recovered, so the next
 * fill-up in the same tab warns again instead of staying silent until critical.
 * Mounted once, by ComputersDialogHost, which always holds the list.
 */
export function useForgetRecoveredDismissals(computers: readonly Computer[] | undefined) {
  useEffect(() => {
    const dismissed = dismissedStore.get();
    const recovered = (computers ?? []).filter(
      (c) => dismissed[c.computer_id] && c.disk?.level && !isDiskAlertLevel(c.disk.level),
    );
    if (recovered.length === 0) return;
    const next = { ...dismissed };
    for (const c of recovered) delete next[c.computer_id];
    writeDismissed(next);
  }, [computers]);
}

function useDismissed(): Dismissals {
  return useSyncExternalStore(dismissedStore.subscribe, dismissedStore.get);
}

/**
 * Whether a computer's warning should show. Critical is never dismissible; a
 * dismissed warning returns once the level gets worse than what was dismissed.
 */
export function useDiskWarningVisible(computer: Computer | null | undefined): boolean {
  return isWarningVisible(computer, useDismissed());
}

function isWarningVisible(computer: Computer | null | undefined, dismissed: Dismissals): boolean {
  const level = computer?.disk?.level;
  if (!computer || !isDiskAlertLevel(level)) return false;
  if (level === 'critical') return true;
  const seen = dismissed[computer.computer_id];
  return !seen || diskLevelRank(level) > diskLevelRank(seen);
}

/**
 * The one machine a single banner speaks for: the worst disk among those whose
 * warning still shows, so dismissing one machine hands the banner to the next
 * rather than silencing a machine that is just as full.
 */
export function useDiskAlertComputer(computers: readonly Computer[] | undefined): Computer | null {
  const dismissed = useDismissed();
  let worst: Computer | null = null;
  for (const c of computers ?? []) {
    if (isWarningVisible(c, dismissed) && diskLevelRank(c.disk?.level) > diskLevelRank(worst?.disk?.level)) worst = c;
  }
  return worst;
}

// --- The warning -----------------------------------------------------------

const ACTION_CLASS = 'h-7 px-2.5 text-xs';

interface DiskWarningProps {
  computer: Computer;
  className?: string;
}

export function DiskWarning({ computer, className }: DiskWarningProps) {
  const { t } = useTranslation();
  const locale = useLocale();
  const visible = useDiskWarningVisible(computer);
  const disk = computer.disk;
  const level = disk?.level;

  if (!visible || !disk || !isDiskAlertLevel(level)) return null;

  const critical = level === 'critical';
  const tint = critical ? 'var(--color-loss-soft)' : 'var(--color-warning-soft)';
  const specChange = activeSpecChange(computer);

  const title = critical
    ? t('computer.disk.criticalTitle', '{{name}} is almost out of disk space', { name: computer.name })
    : t('computer.disk.warningTitle', '{{name}} is running low on disk space', { name: computer.name });
  const body = critical
    ? t(
        'computer.disk.criticalBody',
        'Only {{free}} left. Saving files and running code will start to fail in every workspace on this computer.',
        { free: formatBytes(disk.free_bytes, locale) },
      )
    : t(
        'computer.disk.warningBody',
        '{{free}} left, shared by every workspace on this computer. Free up space before it fills up.',
        { free: formatBytes(disk.free_bytes, locale) },
      );

  // A change already running replaces the call to action with its progress:
  // offering it again would only earn a 409. The top tier has nothing bigger.
  const changeSpec = specChange ? (
    <SpecChangeProgress change={specChange} className="text-xs" />
  ) : providerManagesSpec(computer.kind) && nextTier(effectiveTier(computer)) ? (
    <Button size="sm" className={ACTION_CLASS} onClick={() => openComputerSpec(computer.computer_id)}>
      {t('workspace.changeSpec', 'Change spec')}
    </Button>
  ) : null;
  const breakdown = critical ? (
    <Button
      size="sm"
      variant="ghost"
      className={cn(ACTION_CLASS, 'gap-1.5')}
      onClick={() => openComputersPanel({ computerId: computer.computer_id, expandStorage: true })}
    >
      <HardDrive className="h-3.5 w-3.5" />
      {t('computer.disk.openBreakdown', 'Open storage breakdown')}
    </Button>
  ) : null;

  return (
    <div
      role={critical ? 'alert' : 'status'}
      data-testid="disk-warning"
      data-level={level}
      className={cn('flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm', className)}
      // Opaque: the tint is layered over the page surface, since in a chat the
      // warning sits above the composer and the transcript scrolls under it.
      style={{
        backgroundColor: 'var(--color-bg-page)',
        backgroundImage: `linear-gradient(${tint}, ${tint})`,
        borderColor: critical ? 'var(--color-loss-muted)' : 'var(--color-warning)',
        color: 'var(--color-text-primary)',
      }}
    >
      <AlertTriangle
        className="mt-0.5 h-4 w-4 shrink-0"
        style={{ color: critical ? 'var(--color-loss)' : 'var(--color-warning)' }}
        aria-hidden="true"
      />
      {/* Actions sit beside the text, and drop below it where the row is too narrow. */}
      <div className="flex flex-1 min-w-0 flex-col gap-2 sm:flex-row sm:items-center">
        <div className="flex-1 min-w-0">
          <p className="font-medium">{title}</p>
          <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>
            {body}
          </p>
        </div>
        {(changeSpec || breakdown) && (
          <div className="flex shrink-0 flex-wrap items-center gap-1.5">
            {breakdown}
            {changeSpec}
          </div>
        )}
      </div>
      {!critical && (
        <button
          type="button"
          className="-m-1 shrink-0 rounded p-1.5 hover:opacity-80"
          style={{ color: 'var(--color-text-tertiary)' }}
          aria-label={t('computer.disk.dismiss', 'Dismiss')}
          onClick={() => dismiss(computer.computer_id, level)}
        >
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}
