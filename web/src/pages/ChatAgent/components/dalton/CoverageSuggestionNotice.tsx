import React from 'react';
import { useTranslation } from 'react-i18next';
import { buttonSlot } from '@/components/ui/button-class';
import type { CoverageUnitKindChoice, DedupCoverageSuggestion } from '@/types/api';
import './DuplicateCompanyNotice.css';

interface CoverageSuggestionNoticeProps {
  requestedName: string;
  suggestion: DedupCoverageSuggestion;
  currentCoverageKind: CoverageUnitKindChoice;
  registrable?: boolean;
  disabled?: boolean;
  onRegisterAndCreate: (requestedName: string) => void;
  onCreateWithoutRegistering: (requestedName: string) => void;
}

export function CoverageSuggestionNotice({
  requestedName,
  suggestion,
  currentCoverageKind,
  registrable = true,
  disabled = false,
  onRegisterAndCreate,
  onCreateWithoutRegistering,
}: CoverageSuggestionNoticeProps) {
  const { t } = useTranslation();

  const isCompany = suggestion.coverage_kind === 'company';
  const summaryText = registrable
    ? isCompany
      ? suggestion.ticker
        ? t(
            'workspace.coverageSuggestion.companyWithTicker',
            '"{{name}}" looks like a company ({{ticker}}). Review the pre-filled coverage fields above and choose whether to register a coverage unit when creating.',
            { name: requestedName, ticker: suggestion.ticker },
          )
        : t(
            'workspace.coverageSuggestion.company',
            '"{{name}}" looks like a company. Review the pre-filled coverage fields above and choose whether to register a coverage unit when creating.',
            { name: requestedName },
          )
      : t(
          'workspace.coverageSuggestion.sector',
          '"{{name}}" looks like a sector. Review the pre-filled coverage fields above and choose whether to register a coverage unit when creating.',
          { name: requestedName },
        )
    : isCompany
      ? suggestion.ticker
        ? t(
            'workspace.coverageSuggestion.companyWithTickerUnavailable',
            '"{{name}}" looks like a company ({{ticker}}). Coverage registration fields are currently unavailable; you can create the workspace without registering.',
            { name: requestedName, ticker: suggestion.ticker },
          )
        : t(
            'workspace.coverageSuggestion.companyUnavailable',
            '"{{name}}" looks like a company. Coverage registration fields are currently unavailable; you can create the workspace without registering.',
            { name: requestedName },
          )
      : t(
          'workspace.coverageSuggestion.sectorUnavailable',
          '"{{name}}" looks like a sector. Coverage registration fields are currently unavailable; you can create the workspace without registering.',
          { name: requestedName },
        );

  const canRegister = currentCoverageKind === 'company' || currentCoverageKind === 'sector';

  return (
    <div className="cwm-dup-card" role="alert">
      <div className="cwm-dup-message">{summaryText}</div>
      <div className="cwm-dup-footer" style={{ gap: '8px' }}>
        <button
          type="button"
          {...buttonSlot('outline')}
          className="cwm-dup-anyway-btn"
          disabled={disabled}
          onClick={() => onCreateWithoutRegistering(requestedName)}
        >
          {t('workspace.coverageSuggestion.createWithoutRegistering', 'Create without registering')}
        </button>
        {registrable && (
          <button
            type="button"
            {...buttonSlot()}
            className="cwm-dup-open-btn"
            disabled={disabled || !canRegister}
            onClick={() => onRegisterAndCreate(requestedName)}
          >
            {t('workspace.coverageSuggestion.registerAndCreate', 'Register & Create')}
          </button>
        )}
      </div>
    </div>
  );
}
