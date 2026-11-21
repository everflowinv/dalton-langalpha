import type { TFunction } from 'i18next';
import type { Workspace } from '@/types/api';

const FALLBACK_BY_CODE: Record<string, string> = {
  ticker_conflict:
    'Ticker {{ticker}} is already registered to another coverage unit. The workspace was created without auto-registering a coverage unit.',
  slug_conflict:
    'A coverage unit with the same name is already registered. The workspace was created without auto-registering a coverage unit.',
  workspace_conflict:
    'This workspace is already bound to another coverage unit.',
  invalid_ticker:
    'Invalid ticker format. The workspace was created without auto-registering a coverage unit.',
  sector_ticker_refused:
    'Sector coverage units cannot carry a ticker. The workspace was created without auto-registering a coverage unit.',
  ticker_too_long:
    'Ticker cannot exceed {{max_length}} characters. The workspace was created without auto-registering a coverage unit.',
  display_name_too_long:
    'Display name cannot exceed {{max_length}} characters. The workspace was created without auto-registering a coverage unit.',
  token_not_allowed:
    'This access token cannot register team coverage units. The workspace was created without auto-registering a coverage unit.',
  external_member_forbidden:
    'External members cannot register team coverage units. The workspace was created without auto-registering a coverage unit.',
  authentication_required:
    'Authentication could not be verified. The workspace was created without auto-registering a coverage unit.',
  feature_disabled:
    'Coverage units are disabled. The workspace was created without registering a coverage unit.',
  unsupported_kind:
    'Unsupported coverage type "{{kind}}". The workspace was created without registering a coverage unit.',
  registration_failed:
    'Could not auto-register the coverage unit. The workspace was created and you can register it manually in Coverage Management.',
};

export function formatCoverageUnitWarning(t: TFunction, workspace: Workspace): string | null {
  const detail = workspace.coverage_unit_warning_detail;
  if (detail && typeof detail.code === 'string' && detail.code) {
    const params = detail.params ?? {};
    if (detail.code === 'ticker_conflict' && !params.ticker) {
      return t(
        'workspace.coverage.warnings.ticker_conflict_no_ticker',
        'This ticker is already registered to another coverage unit. The workspace was created without auto-registering a coverage unit.',
      );
    }
    const fallback = FALLBACK_BY_CODE[detail.code] ?? workspace.coverage_unit_warning ?? FALLBACK_BY_CODE.registration_failed;
    return t(`workspace.coverage.warnings.${detail.code}`, fallback, params);
  }
  return workspace.coverage_unit_warning ?? null;
}
