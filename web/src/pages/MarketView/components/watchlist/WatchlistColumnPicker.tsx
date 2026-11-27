import React, { useState, useRef, useEffect } from 'react';
import { Columns3, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  BATCH1_WATCHLIST_COLUMNS,
  DEFAULT_WATCHLIST_COLUMNS,
  type WatchlistColumnKey,
} from '@/lib/dalton/watchlist';

interface WatchlistColumnPickerProps {
  visibleColumns: readonly WatchlistColumnKey[];
  onChange: (next: WatchlistColumnKey[]) => void;
}

const COLUMN_LABEL_KEYS: Record<WatchlistColumnKey, string> = {
  tier: 'marketView:watchlist.columns.tier',
  price: 'marketView:watchlist.columns.price',
  chg_1d: 'marketView:watchlist.columns.chg_1d',
  chg_1w: 'marketView:watchlist.columns.chg_1w',
  chg_1m: 'marketView:watchlist.columns.chg_1m',
  chg_3m: 'marketView:watchlist.columns.chg_3m',
  chg_ytd: 'marketView:watchlist.columns.chg_ytd',
  chg_1y: 'marketView:watchlist.columns.chg_1y',
  pe_fy1: 'marketView:watchlist.columns.pe_fy1',
  pe_fy2: 'marketView:watchlist.columns.pe_fy2',
  eps_growth_fy2: 'marketView:watchlist.columns.eps_growth_fy2',
  range_52w: 'marketView:watchlist.columns.range_52w',
  rsi_14d: 'marketView:watchlist.columns.rsi_14d',
  rsi_30d: 'marketView:watchlist.columns.rsi_30d',
  market_cap: 'marketView:watchlist.columns.market_cap',
  next_earnings: 'marketView:watchlist.columns.next_earnings',
};

export function WatchlistColumnPicker({
  visibleColumns,
  onChange,
}: WatchlistColumnPickerProps): React.ReactElement {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open]);

  const visibleSet = new Set(visibleColumns);

  const toggleColumn = (col: WatchlistColumnKey) => {
    const next = new Set(visibleSet);
    if (next.has(col)) {
      if (next.size <= 1) return;
      next.delete(col);
    } else {
      next.add(col);
    }
    onChange(BATCH1_WATCHLIST_COLUMNS.filter((c) => next.has(c)));
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={t('marketView:watchlist.columnPicker')}
        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium border transition-colors hover:bg-(--color-bg-hover)"
        style={{
          backgroundColor: 'var(--color-bg-card)',
          borderColor: 'var(--color-border-muted)',
          color: 'var(--color-text-secondary)',
        }}
      >
        <Columns3 className="h-3.5 w-3.5" />
        <span>{t('marketView:watchlist.columnPicker')}</span>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label={t('marketView:watchlist.columnPickerTitle')}
          className="absolute right-0 mt-1.5 w-56 rounded-lg border p-2.5 shadow-lg z-30"
          style={{
            backgroundColor: 'var(--color-bg-elevated)',
            borderColor: 'var(--color-border-muted)',
          }}
        >
          <div className="flex items-center justify-between pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border-muted)' }}>
            <span className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('marketView:watchlist.columnPickerTitle')}
            </span>
            <button
              type="button"
              onClick={() => onChange([...DEFAULT_WATCHLIST_COLUMNS])}
              className="inline-flex items-center gap-1 text-[11px] hover:underline"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              <RotateCcw className="h-3 w-3" />
              {t('marketView:watchlist.resetColumns')}
            </button>
          </div>

          <div className="flex flex-col gap-1 max-h-64 overflow-y-auto">
            {BATCH1_WATCHLIST_COLUMNS.map((col) => {
              const checked = visibleSet.has(col);
              return (
                <label
                  key={col}
                  className="flex items-center gap-2 px-1.5 py-1 rounded text-xs cursor-pointer hover:bg-(--color-bg-hover)"
                  style={{ color: 'var(--color-text-primary)' }}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleColumn(col)}
                    className="rounded"
                  />
                  <span>{t(COLUMN_LABEL_KEYS[col])}</span>
                </label>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
