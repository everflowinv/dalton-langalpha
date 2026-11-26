import React, { useState } from 'react';
import { X, AlertTriangle, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  WATCHLIST_TIERS,
  previewWatchlistMutation,
  mutateWatchlist,
  type WatchlistPreviewResponse,
  type WatchlistTier,
} from '@/lib/dalton/watchlist';
import { apiErrorDetailMessage, apiErrorStatus } from '@/api/errors';

interface WatchlistAddModalProps {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

const TIER_KEY_MAP: Record<WatchlistTier, string> = {
  core: 'marketView:watchlist.tiers.core',
  watch: 'marketView:watchlist.tiers.watch',
  radar: 'marketView:watchlist.tiers.radar',
};

export function WatchlistAddModal({
  open,
  onClose,
  onSuccess,
}: WatchlistAddModalProps): React.ReactElement | null {
  const { t } = useTranslation();
  const [tickersInput, setTickersInput] = useState('');
  const [tier, setTier] = useState<WatchlistTier>('watch');
  const [preview, setPreview] = useState<WatchlistPreviewResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  if (!open) return null;

  const parsedTickers = tickersInput
    .split(/[\s,，;；]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);

  const resetAndClose = () => {
    setTickersInput('');
    setTier('watch');
    setPreview(null);
    setErrorMsg(null);
    onClose();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!parsedTickers.length) return;
    setSubmitting(true);
    setErrorMsg(null);
    try {
      const prev = await previewWatchlistMutation({
        action: 'add',
        tickers: parsedTickers,
        tier,
      });
      if (!prev.ok || prev.precheck_error) {
        setErrorMsg(prev.precheck_error || t('marketView:watchlist.errors.previewFailed'));
        return;
      }
      if (prev.requires_confirmation) {
        setPreview(prev);
        return;
      }
      // Exempt add (<=5 net-new tickers, no pause override): submit raw request without snapshot_id
      await mutateWatchlist({
        action: 'add',
        tickers: prev.target_tickers.length > 0 ? prev.target_tickers : parsedTickers,
        tier,
      });
      onSuccess();
      resetAndClose();
    } catch (err) {
      const status = apiErrorStatus(err);
      const detail = apiErrorDetailMessage(err);
      if (status === 409) {
        setPreview(null);
        setErrorMsg(t('marketView:watchlist.errors.scopeChanged'));
      } else {
        setErrorMsg(detail || t('marketView:watchlist.errors.mutateFailed'));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleConfirmPreview = async () => {
    if (!preview) return;
    setSubmitting(true);
    setErrorMsg(null);
    try {
      await mutateWatchlist({
        action: 'add',
        snapshot_id: preview.snapshot_id ?? undefined,
      });
      onSuccess();
      resetAndClose();
    } catch (err) {
      const status = apiErrorStatus(err);
      if (status === 409) {
        setPreview(null);
        setErrorMsg(t('marketView:watchlist.errors.scopeChanged'));
      } else {
        setErrorMsg(apiErrorDetailMessage(err) || t('marketView:watchlist.errors.mutateFailed'));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t('marketView:watchlist.addModal.title')}
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'var(--color-bg-overlay)' }}
      onClick={resetAndClose}
    >
      <div
        className="w-full max-w-md max-h-[90vh] flex flex-col overflow-hidden rounded-xl border p-5 shadow-xl"
        style={{
          backgroundColor: 'var(--color-bg-card)',
          borderColor: 'var(--color-border-muted)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4 shrink-0">
          <h2 className="text-base font-semibold" style={{ color: 'var(--color-text-primary)' }}>
            {t('marketView:watchlist.addModal.title')}
          </h2>
          <button
            type="button"
            onClick={resetAndClose}
            aria-label={t('common.close')}
            className="p-1 rounded hover:bg-(--color-bg-hover)"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="overflow-y-auto flex-1 min-h-0">
        {!preview ? (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                {t('marketView:watchlist.addModal.tickersLabel')}
              </label>
              <input
                type="text"
                value={tickersInput}
                onChange={(e) => setTickersInput(e.target.value)}
                placeholder={t('marketView:watchlist.addModal.tickersPlaceholder')}
                className="w-full px-3 py-2 text-sm rounded-md border focus:outline-none"
                style={{
                  backgroundColor: 'var(--color-bg-input)',
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-primary)',
                }}
                autoFocus
              />
            </div>

            <div>
              <span className="block text-xs font-medium mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                {t('marketView:watchlist.addModal.tierLabel')}
              </span>
              <div className="grid grid-cols-3 gap-2">
                {WATCHLIST_TIERS.map((tierOpt) => {
                  const active = tier === tierOpt;
                  return (
                    <button
                      key={tierOpt}
                      type="button"
                      onClick={() => setTier(tierOpt)}
                      aria-pressed={active}
                      className="px-3 py-2 rounded-md text-xs font-medium border transition-colors"
                      style={{
                        backgroundColor: active ? 'var(--color-btn-primary-bg)' : 'var(--color-bg-page)',
                        color: active ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
                        borderColor: active ? 'var(--color-btn-primary-bg)' : 'var(--color-border-muted)',
                      }}
                    >
                      {t(TIER_KEY_MAP[tierOpt])}
                    </button>
                  );
                })}
              </div>
            </div>

            {errorMsg && (
              <div className="flex items-start gap-2 text-xs p-2.5 rounded-md border" style={{ borderColor: 'var(--color-warning)', color: 'var(--color-text-primary)' }}>
                <AlertTriangle className="h-4 w-4 shrink-0" style={{ color: 'var(--color-warning)' }} />
                <span>{errorMsg}</span>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={resetAndClose}
                className="px-3.5 py-2 rounded-md text-xs font-medium"
                style={{ backgroundColor: 'var(--color-bg-hover)', color: 'var(--color-text-secondary)' }}
              >
                {t('common.cancel')}
              </button>
              <button
                type="submit"
                disabled={submitting || parsedTickers.length === 0}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-md text-xs font-medium disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
              >
                <Plus className="h-3.5 w-3.5" />
                {submitting ? t('marketView:watchlist.submitting') : t('marketView:watchlist.addModal.submit')}
              </button>
            </div>
          </form>
        ) : (
          <div className="space-y-3">
            <div className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {preview.card_title || t('marketView:watchlist.confirmModal.title')}
            </div>
            {preview.card_description && (
              <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {preview.card_description}
              </p>
            )}
            <div className="rounded-md border p-2.5 space-y-1.5 max-h-48 overflow-y-auto" style={{ borderColor: 'var(--color-border-muted)' }}>
              {preview.affected_tickers.map((item) => (
                <div key={item.ticker} className="flex items-center justify-between text-xs">
                  <span className="font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {item.ticker}
                  </span>
                  <span style={{ color: 'var(--color-text-tertiary)' }}>
                    {item.old_tier ? `${t(TIER_KEY_MAP[item.old_tier])} → ` : ''}
                    {item.new_tier ? t(TIER_KEY_MAP[item.new_tier]) : ''}
                  </span>
                </div>
              ))}
            </div>
            {preview.affected_workspaces.length > 0 && (
              <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {t('marketView:watchlist.confirmModal.affectedWorkspaces', {
                  count: preview.affected_workspaces.length,
                })}
              </div>
            )}
            {errorMsg && (
              <div className="text-xs" style={{ color: 'var(--color-warning)' }}>
                {errorMsg}
              </div>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => setPreview(null)}
                className="px-3.5 py-2 rounded-md text-xs font-medium"
                style={{ backgroundColor: 'var(--color-bg-hover)', color: 'var(--color-text-secondary)' }}
              >
                {t('common.back')}
              </button>
              <button
                type="button"
                disabled={submitting}
                onClick={handleConfirmPreview}
                className="px-4 py-2 rounded-md text-xs font-medium disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
              >
                {submitting ? t('marketView:watchlist.submitting') : t('marketView:watchlist.confirmModal.confirm')}
              </button>
            </div>
          </div>
        )}
        </div>
      </div>
    </div>
  );
}
