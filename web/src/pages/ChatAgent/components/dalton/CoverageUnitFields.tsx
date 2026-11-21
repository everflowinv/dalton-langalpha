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
        </div>
      )}
    </>
  );
}
