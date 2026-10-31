import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';

// 行业层 P4 覆盖视图的数据层。类型与后端 `/api/v1/dalton/coverage/units*`
// 的真实响应逐字段一致（src/dalton/autonomy/units_view.py）——后端改了
// 这里要同步，不在这层补后端不返回的字段。

export type CoverageUnitKind = 'sector' | 'company';

/** One member row of a sector unit (d025 projection, viewer-filtered). */
export interface SectorMember {
  member_id: string | null;
  name: string | null;
  ticker: string | null;
  company_unit_id: string | null;
  workspace_created: boolean;
  workspace_id: string | null;
  scope: string;
  reason: string;
  valid_until: string | null;
}

/** A coverage unit as the list view and the board `units` output it (P2). */
export interface CoverageUnitEntry {
  unit_id: string;
  kind: CoverageUnitKind;
  slug: string | null;
  display_name: string | null;
  owner_user_id: string;
  workspace_id: string | null;
  readable: boolean;
  delegatable: boolean;
  auto_tracked: boolean;
  ticker?: string | null;
  members?: SectorMember[];
}

export interface UnclassifiedWorkspace {
  workspace_id: string;
  name: string | null;
  description: string | null;
  updated_at: string | null;
}

/** 开关关闭时后端只返回 `{enabled: false}`，不带其余键。 */
export type DaltonCoverageUnitsResponse =
  | { enabled: false }
  | { enabled: true; units: CoverageUnitEntry[]; unclassified: UnclassifiedWorkspace[] };

export interface SectorDocSummary {
  version_no: number | null;
  name: string | null;
  name_zh: string | null;
  definition: string | null;
  boundaries: string | null;
  one_liner: string | null;
  coverage_stage: string | null;
  coverage_kind: string | null;
  wiki_slug: string | null;
  research_tz: string | null;
  regions: string[];
  markets: string[];
}

export interface ThesisSummary {
  version_no: number | null;
  one_liner: string | null;
  aggregate_status: string | null;
  stance: string | null;
  conviction: string | null;
  pillars: Array<{
    id: string | null;
    claim: string | null;
    priority: string | null;
    status: string | null;
  }>;
}

export interface DriverIndicator {
  id: string | null;
  name: string | null;
  frequency: string | null;
  good_direction: string | null;
  latest: {
    value: number | null;
    unit: string | null;
    period: string | null;
    as_of: string | null;
  } | null;
}

export interface DriversSummary {
  version_no: number | null;
  as_of: string | null;
  drivers: Array<{
    id: string | null;
    name: string | null;
    kind: string | null;
    updated_at: string | null;
    leading_indicators: DriverIndicator[];
  }>;
}

export interface AgendaTodos {
  questions: Array<{
    id: string;
    question: string;
    why: string;
    value: string;
    urgency: string;
    origin: string | null;
    waiting_pm: boolean;
  }>;
  validations: Array<{ id: string; what: string; due: string | null }>;
  events: Array<{
    type: string;
    title: string;
    date: string | null;
    date_status: string;
  }>;
}

export interface EvidenceGap {
  request_id: string | null;
  question: string | null;
  evidence_needed: unknown[];
  completion_condition: string | null;
  target_workspace_id: string | null;
  status: string | null;
  created_at: string | null;
}

export interface LedgerGap {
  request_id: string | null;
  kind: string | null;
  question: string | null;
  target_workspace_id: string | null;
  status: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface RelatedSector {
  unit_id: string;
  slug: string | null;
  display_name: string | null;
  member_id: string | null;
  scope: string;
  valid_until: string | null;
  readable: boolean;
  delegatable: boolean;
  workspace_id: string | null;
}

export interface ReReviewItem {
  id: string;
  question: string;
  value: string;
  urgency: string;
  status: string;
  source_ref: Record<string, unknown> | null;
}

export interface CoverageUnitDetail {
  unit_id: string;
  kind: CoverageUnitKind;
  slug: string | null;
  display_name: string | null;
  owner_user_id: string;
  workspace_id: string | null;
  readable: boolean;
  delegatable: boolean;
  auto_tracked: boolean;
  ticker?: string | null;
  sector?: {
    docs: {
      sector: SectorDocSummary | null;
      thesis: ThesisSummary | null;
      drivers: DriversSummary | null;
      agenda_version_no: number | null;
    } | null;
    agenda_todos: AgendaTodos | null;
    members: SectorMember[];
    evidence_gaps: EvidenceGap[];
    ledger_gaps: LedgerGap[];
    /** 该行业待处理台账行的总数（SQL 先过滤再 LIMIT）；超过展示条数时 UI 提示 */
    pending_total: number;
  };
  company?: {
    docs: { thesis: ThesisSummary | null } | null;
    sectors: RelatedSector[];
    re_review: ReReviewItem[];
  };
}

export async function fetchDaltonCoverageUnits(): Promise<DaltonCoverageUnitsResponse> {
  const { data } = await api.get<DaltonCoverageUnitsResponse>('/api/v1/dalton/coverage/units');
  return data;
}

export async function fetchDaltonCoverageUnitDetail(
  unitId: string,
): Promise<CoverageUnitDetail> {
  const { data } = await api.get<CoverageUnitDetail>(
    `/api/v1/dalton/coverage/units/${encodeURIComponent(unitId)}`,
  );
  return data;
}

/**
 * The coverage listing doubles as the flag channel: `enabled === false` is the
 * backend saying the sector layer is off, and every sector-related surface
 * (coverage card, sector scope in the system-jobs card) hides on it.
 */
export function useDaltonCoverageUnits() {
  return useQuery({
    queryKey: queryKeys.dalton.coverageUnits(),
    queryFn: fetchDaltonCoverageUnits,
    staleTime: 30_000,
  });
}

export function useDaltonCoverageUnitDetail(unitId: string | null) {
  return useQuery({
    queryKey: queryKeys.dalton.coverageUnitDetail(unitId ?? ''),
    queryFn: () => fetchDaltonCoverageUnitDetail(unitId as string),
    enabled: unitId !== null,
    staleTime: 30_000,
  });
}
