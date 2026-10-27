import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { createPortal } from 'react-dom';
import { ChevronRight, X, Calendar } from 'lucide-react';
import { motion, AnimatePresence } from '@/lib/framer';
import { TWEEN } from '@/lib/motion';
import { SwapCell } from '@/components/ui/crossfade';
import { useBackdropDismiss } from '@/hooks/useDialogA11y';
import { useLocale } from '@/hooks/useLocale';
import { useNow } from '@/hooks/useNow';
import { createDateFormatter, shortMonthDay, shortWeekday } from '@/lib/format';
import { localDateStr } from '@/lib/utils';
// DALTON-HOOK(user-coverage): per-user coverage earnings calendar tab, badges, and 90-day modal view
import {
  type DaltonEarningsEntry,
  type EarningsScope,
  EarningsScopeTabs,
  UserEarningsBadges,
  useEarningsCalendarQuery,
} from './dalton/UserEarningsCalendar';

interface EarningsEntry extends DaltonEarningsEntry {
  symbol: string;
  date: string;
  companyName?: string;
  [key: string]: unknown;
}

interface EarningsPreviewItem extends EarningsEntry {
  _isPast: boolean;
}

interface LogoFallbackProps {
  symbol: string;
}

interface EarningsItemProps {
  item: EarningsEntry;
  index: number;
  isPast?: boolean;
}

interface SectionLabelProps {
  label: string;
}

interface EarningsModalProps {
  earnings: EarningsEntry[];
  onClose: () => void;
}

interface DateGroup {
  date: string;
  items: EarningsEntry[];
}

interface DateTabInfo {
  weekday: string;
  label: string;
}

function LogoFallback({ symbol }: LogoFallbackProps) {
  return (
    <span className="font-bold text-xs" style={{ color: 'var(--color-text-primary)' }}>
      {(symbol || '??').substring(0, 2)}
    </span>
  );
}

const shortMonth = createDateFormatter({ month: 'short' });

function formatDate(dateStr: string | undefined, locale: string): string {
  if (!dateStr) return '';
  const d = new Date(dateStr + 'T00:00:00');
  return Number.isNaN(d.getTime()) ? '' : shortMonthDay(d, locale);
}

function EarningsItem({ item, index: _index, isPast }: EarningsItemProps) {
  const locale = useLocale();
  const dateStr = formatDate(item.date, locale);

  return (
    <div
      className="group flex items-center justify-between p-3 rounded-xl border border-transparent transition-all cursor-pointer"
      style={{ backgroundColor: 'transparent', opacity: isPast ? 0.6 : 1 }}
      onMouseEnter={(e) => {
        e.currentTarget.style.backgroundColor = 'var(--color-bg-hover)';
        e.currentTarget.style.borderColor = 'var(--color-border-muted)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.backgroundColor = 'transparent';
        e.currentTarget.style.borderColor = 'transparent';
      }}
    >
      <div className="flex items-center gap-3">
        <div
          className="w-10 h-10 rounded-lg flex items-center justify-center overflow-hidden border"
          style={{
            backgroundColor: 'var(--color-bg-tag)',
            borderColor: 'var(--color-border-muted)',
          }}
        >
          <LogoFallback symbol={item.symbol} />
        </div>
        <div>
          <div className="font-bold text-sm" style={{ color: 'var(--color-text-primary)' }}>
            {item.symbol}
          </div>
          <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {item.companyName || item.symbol}
          </div>
          {/* DALTON-HOOK(user-coverage): render tier/status/timing badges for user coverage items */}
          <UserEarningsBadges item={item} />
        </div>
      </div>

      <div className="text-right">
        <div className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {dateStr}
        </div>
      </div>
    </div>
  );
}

function SectionLabel({ label }: SectionLabelProps) {
  return (
    <div
      className="text-[0.625rem] font-bold uppercase tracking-wider px-3 pt-2 pb-1"
      style={{ color: 'var(--color-text-secondary)' }}
    >
      {label}
    </div>
  );
}

function formatDateTab(dateStr: string | undefined, locale: string): DateTabInfo {
  if (!dateStr) return { weekday: '', label: '' };
  const d = new Date(dateStr + 'T00:00:00');
  if (Number.isNaN(d.getTime())) return { weekday: '', label: '' };
  return { weekday: shortWeekday(d, locale), label: `${shortMonth(d, locale)} ${d.getDate()}` };
}

function EarningsModal({ earnings, onClose }: EarningsModalProps) {
  const { t } = useTranslation();
  const locale = useLocale();
  const now = useNow();
  const backdrop = useBackdropDismiss<HTMLDivElement>(onClose);
  const todayStr = localDateStr(now);

  // Group by date, sorted chronologically
  const dateGroups = useMemo((): DateGroup[] => {
    const groups: Record<string, EarningsEntry[]> = {};
    for (const e of earnings) {
      if (!e.date) continue;
      if (!groups[e.date]) groups[e.date] = [];
      groups[e.date].push(e);
    }
    return Object.keys(groups)
      .sort((a, b) => a.localeCompare(b))
      .map((date) => ({ date, items: groups[date] }));
  }, [earnings]);

  // Default to today's tab, or the nearest upcoming date
  const defaultDate = useMemo(() => {
    const todayGroup = dateGroups.find((g) => g.date === todayStr);
    if (todayGroup) return todayStr;
    const upcoming = dateGroups.find((g) => g.date > todayStr);
    if (upcoming) return upcoming.date;
    return dateGroups[dateGroups.length - 1]?.date || '';
  }, [dateGroups, todayStr]);

  // DALTON-HOOK(user-coverage): fall back to the 90-day upcoming tab when no date group matches
  const [activeDate, setActiveDate] = useState(defaultDate || 'next90');
  const itemsRef = useRef<HTMLDivElement>(null);
  // The dates dissolve in one cell at the same scroll offset, so the next one
  // opens at its top rather than out of view below a short list.
  const selectDate = (date: string) => {
    if (date !== activeDate) itemsRef.current?.scrollTo({ top: 0 });
    setActiveDate(date);
  };

  // DALTON-HOOK(user-coverage): 90-day upcoming view in modal
  const next90LimitStr = useMemo(() => localDateStr(now + 90 * 86400000), [now]);
  const next90Items = useMemo(
    () =>
      earnings
        .filter((e) => e.date && e.date >= todayStr && e.date <= next90LimitStr)
        .sort((a, b) => a.date.localeCompare(b.date)),
    [earnings, todayStr, next90LimitStr]
  );

  const activeItems = useMemo(
    () =>
      activeDate === 'next90'
        ? next90Items
        : dateGroups.find((g) => g.date === activeDate)?.items || [],
    [dateGroups, activeDate, next90Items]
  );

  const isPastDate = activeDate !== 'next90' && activeDate < todayStr;

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handleEsc);
    return () => window.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      {...backdrop}
      className="fixed inset-0 z-1030 flex items-center justify-center p-4 md:p-8"
      style={{ backgroundColor: 'var(--color-bg-overlay, rgba(0,0,0,0.6))', backdropFilter: 'blur(4px)' }}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20, transition: TWEEN.exit }}
        transition={TWEEN.enter}
        className="w-full max-w-4xl max-h-[80vh] rounded-3xl overflow-hidden shadow-2xl flex flex-col relative border"
        style={{
          backgroundColor: 'var(--color-bg-elevated)',
          borderColor: 'var(--color-border-muted)',
        }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 pt-6 pb-4 shrink-0">
          <h2 className="text-xl font-bold flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
            <Calendar size={20} style={{ color: 'var(--color-accent-light)' }} />
            {t('dashboard.widgets.earningsCalendar.title')}
          </h2>
          <button
            onClick={onClose}
            className="p-2 rounded-full transition-colors"
            style={{ color: 'var(--color-text-secondary)' }}
            onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = 'var(--color-bg-hover)'; }}
            onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = 'transparent'; }}
            aria-label={t('dashboard.earningsCalendarCard.close')}
          >
            <X size={18} />
          </button>
        </div>

        {/* Date tabs */}
        <div
          className="flex gap-1 px-6 pb-4 overflow-x-auto shrink-0"
          style={{ scrollbarWidth: 'none' }}
        >
          {/* DALTON-HOOK(user-coverage): Next 90 Days view tab in modal */}
          <button
            key="next90"
            onClick={() => selectDate('next90')}
            className="flex flex-col items-center justify-center px-4 py-2 rounded-xl text-xs font-medium transition-all shrink-0 min-w-[88px] border"
            style={{
              backgroundColor:
                activeDate === 'next90' ? 'var(--color-accent-primary)' : 'var(--color-bg-tag)',
              color: activeDate === 'next90' ? '#fff' : 'var(--color-text-primary)',
              borderColor:
                activeDate === 'next90' ? 'var(--color-accent-primary)' : 'transparent',
            }}
          >
            <span className="font-bold">{t('dashboard.earningsCalendarCard.next90Days')}</span>
            <span className="text-[0.625rem] mt-0.5" style={{ opacity: 0.7 }}>
              {t('dashboard.earningsCalendarCard.stockCount', { count: next90Items.length })}
            </span>
          </button>
          {dateGroups.map((group) => {
            const isActive = group.date === activeDate;
            const isToday = group.date === todayStr;
            const isPast = group.date < todayStr;
            const { weekday, label } = formatDateTab(group.date, locale);
            return (
              <button
                key={group.date}
                onClick={() => selectDate(group.date)}
                className="flex flex-col items-center px-4 py-2 rounded-xl text-xs font-medium transition-all shrink-0 min-w-[72px] border"
                style={{
                  backgroundColor: isActive
                    ? 'var(--color-accent-primary)'
                    : 'var(--color-bg-tag)',
                  color: isActive
                    ? '#fff'
                    : isPast
                      ? 'var(--color-text-secondary)'
                      : 'var(--color-text-primary)',
                  borderColor: isActive
                    ? 'var(--color-accent-primary)'
                    : isToday
                      ? 'var(--color-accent-overlay)'
                      : 'transparent',
                  opacity: isPast && !isActive ? 0.7 : 1,
                }}
              >
                <span className="text-[0.625rem] uppercase tracking-wider" style={{ opacity: 0.8 }}>
                  {weekday}
                </span>
                <span className="font-bold">{label}</span>
                <span className="text-[0.625rem] mt-0.5" style={{ opacity: 0.7 }}>
                  {t('dashboard.earningsCalendarCard.stockCount', { count: group.items.length })}
                </span>
              </button>
            );
          })}
        </div>

        {/* Divider */}
        <div className="mx-6 border-b shrink-0" style={{ borderColor: 'var(--color-border-muted)' }} />

        {/* Items grid. A change of date dissolves in one grid cell, so the
            list is never replaced through an empty frame. */}
        <div ref={itemsRef} className="overflow-y-auto flex-1 p-6">
          <SwapCell
            as="div"
            className="items-start"
            appear
            shift={{ y: 8 }}
            swapKey={activeItems.length === 0 ? 'empty' : activeDate}
          >
            {activeItems.length === 0 ? (
              <p
                className="text-sm py-8 text-center"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                {t('dashboard.earningsCalendarCard.emptyOnDate')}
              </p>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                {activeItems.map((item, i) => (
                  <div
                    key={item.symbol + item.date + i}
                    className="flex items-center gap-3 p-3 rounded-xl border transition-all"
                    style={{
                      backgroundColor: 'var(--color-bg-card)',
                      borderColor: 'var(--color-border-muted)',
                      opacity: isPastDate ? 0.7 : 1,
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.borderColor = 'var(--color-border-default)';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.borderColor = 'var(--color-border-muted)';
                    }}
                  >
                    <div
                      className="w-10 h-10 rounded-lg flex items-center justify-center overflow-hidden border shrink-0"
                      style={{
                        backgroundColor: 'var(--color-bg-tag)',
                        borderColor: 'var(--color-border-muted)',
                      }}
                    >
                      <LogoFallback symbol={item.symbol} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <div className="font-bold text-sm truncate" style={{ color: 'var(--color-text-primary)' }}>
                          {item.symbol}
                        </div>
                        {/* DALTON-HOOK(user-coverage): show date in 90-day modal view */}
                        {activeDate === 'next90' && item.date && (
                          <div className="text-xs font-medium shrink-0" style={{ color: 'var(--color-text-secondary)' }}>
                            {formatDate(item.date, locale)}
                          </div>
                        )}
                      </div>
                      <div className="text-xs truncate" style={{ color: 'var(--color-text-secondary)' }}>
                        {item.companyName || item.symbol}
                      </div>
                      <UserEarningsBadges item={item} />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </SwapCell>
        </div>
      </motion.div>
    </motion.div>
  );
}

function EarningsCalendarCard() {
  const { t } = useTranslation();
  // DALTON-HOOK(user-coverage): default to "mine" ("我的") tab; React Query keys by scope + user so switching tabs never races
  const [scope, setScope] = useState<EarningsScope>('mine');
  const [modalOpen, setModalOpen] = useState(false);

  const now = useNow();
  const todayStr = localDateStr(now);
  const { data: queryEarnings, isLoading: loading } = useEarningsCalendarQuery(scope, now);
  // DALTON-HOOK(user-coverage): bind earnings rows from scoped React Query cache
  const allEarnings = (queryEarnings ?? []) as EarningsEntry[];

  const { recent, upcoming } = useMemo(() => {
    const r = allEarnings.filter((e) => e.date < todayStr).sort((a, b) => b.date.localeCompare(a.date));
    const u = allEarnings.filter((e) => e.date >= todayStr).sort((a, b) => a.date.localeCompare(b.date));
    return { recent: r, upcoming: u };
  }, [allEarnings, todayStr]);

  // Card preview: show up to 3 upcoming + fill remaining from recent, max 6 total
  const previewItems = useMemo((): EarningsPreviewItem[] => {
    const upSlice = upcoming.slice(0, 3);
    const remaining = 6 - upSlice.length;
    const recSlice = recent.slice(0, remaining);
    return [
      ...recSlice.map((e) => ({ ...e, _isPast: true })),
      ...upSlice.map((e) => ({ ...e, _isPast: false })),
    ];
  }, [recent, upcoming]);

  return (
    <>
      <div className="dashboard-glass-card p-6 flex flex-col">
        {/* DALTON-HOOK(user-coverage): scope tabs ("我的" / "全市场") and empty state without CoS onboarding prompt */}
        <div className="flex items-center justify-between gap-2 mb-4">
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="text-xl font-bold" style={{ color: 'var(--color-text-primary)' }}>
              {t('dashboard.widgets.earningsCalendar.title')}
            </h2>
            <EarningsScopeTabs scope={scope} onScopeChange={setScope} />
          </div>
          <button
            onClick={() => setModalOpen(true)}
            className="text-xs flex items-center gap-1 transition-colors shrink-0"
            style={{ color: 'var(--color-text-secondary)' }}
            onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--color-text-primary)')}
            onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--color-text-secondary)')}
          >
            {t('dashboard.earningsCalendarCard.viewAll')} <ChevronRight size={12} />
          </button>
        </div>

        <div className="flex flex-col gap-1">
          {loading ? (
            Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="flex items-center gap-3 p-3 animate-pulse">
                <div
                  className="w-10 h-10 rounded-lg"
                  style={{ backgroundColor: 'var(--color-border-default)' }}
                />
                <div className="flex-1">
                  <div
                    className="h-4 rounded mb-1"
                    style={{ backgroundColor: 'var(--color-border-default)', width: '50%' }}
                  />
                  <div
                    className="h-3 rounded"
                    style={{ backgroundColor: 'var(--color-border-default)', width: '70%' }}
                  />
                </div>
              </div>
            ))
          ) : previewItems.length === 0 ? (
            /* DALTON-HOOK(user-coverage): per-scope empty state copy ("暂无财报数据" vs market empty period) */
            <p className="text-sm py-4 text-center" style={{ color: 'var(--color-text-secondary)' }}>
              {scope === 'mine'
                ? t('dashboard.earningsCalendarCard.emptyData')
                : t('dashboard.earningsCalendarCard.emptyPeriod')}
            </p>
          ) : (
            <>
              {previewItems.some((e) => e._isPast) && <SectionLabel label={t('dashboard.earningsCalendarCard.sectionRecent')} />}
              {previewItems.filter((e) => e._isPast).map((item, i) => (
                <EarningsItem key={item.symbol + item.date + i} item={item} index={i} isPast />
              ))}
              {previewItems.some((e) => !e._isPast) && <SectionLabel label={t('dashboard.earningsCalendarCard.sectionUpcoming')} />}
              {previewItems.filter((e) => !e._isPast).map((item, i) => (
                <EarningsItem key={item.symbol + item.date + i} item={item} index={i} />
              ))}
            </>
          )}
        </div>
      </div>

      {createPortal(
        <AnimatePresence>
          {modalOpen && (
            <EarningsModal earnings={allEarnings} onClose={() => setModalOpen(false)} />
          )}
        </AnimatePresence>,
        document.body
      )}
    </>
  );
}

export default EarningsCalendarCard;
