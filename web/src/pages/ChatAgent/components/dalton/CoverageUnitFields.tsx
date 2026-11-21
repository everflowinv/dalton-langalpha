import React, { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Input } from '@/components/ui/input';
import { SegmentedControl } from '@/components/ui/segmented-control';
import type { CoverageUnitKindChoice } from '@/types/api';

interface CoverageUnitFieldsProps {
  coverageKind: CoverageUnitKindChoice;
  ticker: string;
  displayName: string;
  prefilled?: boolean;
  disabled?: boolean;
  onChangeKind: (kind: CoverageUnitKindChoice) => void;
  onChangeTicker: (ticker: string) => void;
  onChangeDisplayName: (displayName: string) => void;
}

const INPUT_STYLE: React.CSSProperties = {
  backgroundColor: 'var(--color-shell-fill, var(--color-bg-card))',
  border: '1px solid var(--color-shell-edge, var(--color-border-muted))',
  color: 'var(--color-text-primary)',
};

const MAX_TICKER_LEN = 32;
const MAX_DISPLAY_NAME_LEN = 120;

export function CoverageUnitFields({
  coverageKind,
  ticker,
  displayName,
  prefilled = false,
  disabled = false,
  onChangeKind,
  onChangeTicker,
  onChangeDisplayName,
}: CoverageUnitFieldsProps) {
  const { t } = useTranslation();
  const typeLabelId = useId();
  const tickerId = useId();
  const displayNameId = useId();
  const tickerTooLong = ticker.trim().length > MAX_TICKER_LEN;
  const displayNameTooLong = displayName.trim().length > MAX_DISPLAY_NAME_LEN;

  return (
    <>
      <div className="cwm-field">
        <span id={typeLabelId} className="cwm-label">
          {t('workspace.coverage.typeLabel', 'Type')}
        </span>
        <SegmentedControl<CoverageUnitKindChoice>
          value={coverageKind}
          onChange={onChangeKind}
          labelledBy={typeLabelId}
          disabled={disabled}
          options={[
            { value: 'company', label: t('workspace.coverage.company', 'Company') },
            { value: 'sector', label: t('workspace.coverage.sector', 'Sector') },
            { value: 'none', label: t('workspace.coverage.other', 'Other') },
          ]}
        />
        {prefilled && coverageKind !== 'none' && (
          <div className="cwm-field-hint">
            {t(
              'workspace.coverage.prefilledHint',
              'Pre-filled from workspace check — you can edit these before creating.',
            )}
          </div>
        )}
      </div>

      {coverageKind === 'company' && (
        <>
          <div className="cwm-field">
            <label className="cwm-label" htmlFor={tickerId}>
              {t('workspace.coverage.tickerLabel', 'Ticker')}{' '}
              <span className="cwm-label-optional">{t('common.optional')}</span>
            </label>
            <Input
              id={tickerId}
              type="text"
              value={ticker}
              onChange={(e) => onChangeTicker(e.target.value)}
              placeholder={t(
                'workspace.coverage.tickerPlaceholder',
                'e.g. NVDA, 0700.HK (optional if unlisted)',
              )}
              disabled={disabled}
              className="w-full"
              style={INPUT_STYLE}
            />
            <div className="cwm-field-hint" style={tickerTooLong ? { color: 'var(--color-loss)' } : undefined}>
              {tickerTooLong
                ? t(
                    'workspace.coverage.tickerLengthExceeded',
                    'Max {{max}} characters (currently {{count}}) — workspace will be created without registering.',
                    { max: MAX_TICKER_LEN, count: ticker.trim().length },
                  )
                : t('workspace.coverage.tickerLengthHint', 'Max {{max}} characters', { max: MAX_TICKER_LEN })}
            </div>
          </div>
          <div className="cwm-field">
            <label className="cwm-label" htmlFor={displayNameId}>
              {t('workspace.coverage.companyNameLabel', 'Company Name')}{' '}
              <span className="cwm-label-optional">{t('common.optional')}</span>
            </label>
            <Input
              id={displayNameId}
              type="text"
              value={displayName}
              onChange={(e) => onChangeDisplayName(e.target.value)}
              placeholder={t(
                'workspace.coverage.companyNamePlaceholder',
                'e.g. NVIDIA Corporation (defaults to workspace name)',
              )}
              disabled={disabled}
              className="w-full"
              style={INPUT_STYLE}
            />
            <div className="cwm-field-hint" style={displayNameTooLong ? { color: 'var(--color-loss)' } : undefined}>
              {displayNameTooLong
                ? t(
                    'workspace.coverage.displayNameLengthExceeded',
                    'Max {{max}} characters (currently {{count}}) — workspace will be created without registering.',
                    { max: MAX_DISPLAY_NAME_LEN, count: displayName.trim().length },
                  )
                : t('workspace.coverage.displayNameLengthHint', 'Max {{max}} characters', { max: MAX_DISPLAY_NAME_LEN })}
            </div>
          </div>
        </>
      )}

      {coverageKind === 'sector' && (
        <div className="cwm-field">
          <label className="cwm-label" htmlFor={displayNameId}>
            {t('workspace.coverage.sectorNameLabel', 'Sector Name')}{' '}
            <span className="cwm-label-optional">{t('common.optional')}</span>
          </label>
          <Input
            id={displayNameId}
            type="text"
            value={displayName}
            onChange={(e) => onChangeDisplayName(e.target.value)}
            placeholder={t(
              'workspace.coverage.sectorNamePlaceholder',
              'e.g. AI Infrastructure (defaults to workspace name)',
            )}
            disabled={disabled}
            className="w-full"
            style={INPUT_STYLE}
          />
          <div className="cwm-field-hint" style={displayNameTooLong ? { color: 'var(--color-loss)' } : undefined}>
            {displayNameTooLong
              ? t(
                  'workspace.coverage.displayNameLengthExceeded',
                  'Max {{max}} characters (currently {{count}}) — workspace will be created without registering.',
                  { max: MAX_DISPLAY_NAME_LEN, count: displayName.trim().length },
                )
              : t('workspace.coverage.displayNameLengthHint', 'Max {{max}} characters', { max: MAX_DISPLAY_NAME_LEN })}
          </div>
        </div>
      )}
    </>
  );
}
