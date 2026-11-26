import { api, type ApiResponse } from '@/api/client';
import type { ReportProfileConfig } from '@/types/automation';

export {
  changedReportProfileForSubmit,
  extractReportProfile,
  reportProfilesEqual,
} from './reportProfileUtils';

export interface ReportDeliveryConfirmRequest {
  action: 'create' | 'update';
  automation_id?: string | null;
  name?: string | null;
  instruction?: string | null;
  report_profile: ReportProfileConfig;
  expected_config_version?: number | null;
}

export interface ReportDeliveryPrecheckResponse {
  needs_confirmation: boolean;
  action: 'create' | 'update';
  automation_id: string | null;
  automation_name: string;
  config_version: number | null;
  profile: string | null;
  report_targets: string[];
  text_targets: string[] | null;
  failure_targets: string[];
  group_targets: string[];
  non_caller_dm_targets: string[];
  target_labels: Record<string, string>;
}

export interface ReportDeliveryConfirmationIssueResponse {
  needs_confirmation: boolean;
  confirmation_id?: string;
  confirmation_token: string | null;
  expires_at: string | null;
  action: 'create' | 'update';
  automation_id: string | null;
  automation_name: string;
  config_version: number | null;
  profile: string | null;
  report_targets: string[];
  text_targets: string[] | null;
  failure_targets: string[];
  target_labels: Record<string, string>;
}

export const precheckReportDeliveryConfirmation = (
  body: ReportDeliveryConfirmRequest,
): Promise<ApiResponse<ReportDeliveryPrecheckResponse>> =>
  api.post('/api/v1/dalton/report-delivery/precheck', body);

export const issueReportDeliveryConfirmation = (
  body: ReportDeliveryConfirmRequest,
): Promise<ApiResponse<ReportDeliveryConfirmationIssueResponse>> =>
  api.post('/api/v1/dalton/report-delivery/confirmations', body);
