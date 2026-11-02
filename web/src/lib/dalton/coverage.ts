import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';

// 行业层 P4 覆盖视图的数据层。类型与后端 `/api/v1/dalton/coverage/units*`
// 的真实响应逐字段一致（src/dalton/autonomy/units_view.py）——后端改了
// 这里要同步，不在这层补后端不返回的字段。
// P5 写路径（登记 / 绑定 / 解绑 / 归档）的响应是服务层的单元行
// （src/dalton/database/coverage_units.py 的 _UNIT_COLS）。

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
export function useDaltonCoverageUnits(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.coverageUnits(),
    queryFn: fetchDaltonCoverageUnits,
    enabled,
    staleTime: 30_000,
  });
}

// ---------------------------------------------------------------------------
// 从属关系推导（侧栏"覆盖结构"树、工作区行 chip、工作区页从属条共用）。
// 全部只读列表响应的字段；成员的 workspace_id 后端已按查看者过滤，
// 有值即代表本人可读，前端不重复判断。
// ---------------------------------------------------------------------------

/** 成员公司判定：被任何（可见）行业列为成员的公司单元 id 集合。
 *  行业 / 独立公司两分与角色映射都从这一处推导。 */
export function coverageMemberUnitIds(units: CoverageUnitEntry[]): Set<string> {
  const memberUnitIds = new Set<string>();
  for (const u of units) {
    if (u.kind !== 'sector') continue;
    for (const m of u.members ?? []) {
      if (m.company_unit_id) memberUnitIds.add(m.company_unit_id);
    }
  }
  return memberUnitIds;
}

/** 行业 / 独立公司两分（未分类工作区不入 units，走 unclassified）。
 *  推导规则与设置页覆盖卡片的 tab 一致，是其唯一实现。 */
export function splitCoverageUnits(units: CoverageUnitEntry[]): {
  sectors: CoverageUnitEntry[];
  standaloneCompanies: CoverageUnitEntry[];
} {
  const memberUnitIds = coverageMemberUnitIds(units);
  return {
    sectors: units.filter((u) => u.kind === 'sector'),
    standaloneCompanies: units.filter(
      (u) => u.kind === 'company' && !memberUnitIds.has(u.unit_id),
    ),
  };
}

/** A member company's parent sector, resolved for display and navigation. */
export interface CoverageRoleSector {
  unitId: string;
  displayName: string;
  workspaceId: string | null;
  readable: boolean;
}

/**
 * A bound workspace's place in the coverage structure:
 * `sector` — the sector unit's own workspace; `member` — a member company's
 * workspace (possibly under several sectors); `standalone` — a company unit
 * bound to no visible sector.
 */
export type CoverageWorkspaceRole =
  | { kind: 'sector'; sectorNames: never[]; sectors: never[]; unit: CoverageUnitEntry }
  | { kind: 'member'; sectorNames: string[]; sectors: CoverageRoleSector[] }
  | { kind: 'standalone'; sectorNames: never[]; sectors: never[] };

/** 空结果共用一个模块级常量：调用方（React.memo 的行）按引用比较。 */
const EMPTY_ROLES: ReadonlyMap<string, CoverageWorkspaceRole> = new Map();

/**
 * Why a member row / chip cannot be entered, in one shared rule:
 * `noWorkspace` — nothing was ever created (`workspace_created` false);
 * `noAccess` — a workspace exists but the backend filtered its id out for
 * this viewer. The i18n keys are `dalton.coverage.noWorkspace` / `.noAccess`.
 */
export function coverageNoEntryReason(workspaceCreated: boolean): 'noWorkspace' | 'noAccess' {
  return workspaceCreated ? 'noAccess' : 'noWorkspace';
}

export function deriveCoverageWorkspaceRoles(
  units: CoverageUnitEntry[],
): ReadonlyMap<string, CoverageWorkspaceRole> {
  // 与 splitCoverageUnits 同一"成员公司"判定：被任何（可见）行业列为成员。
  const memberUnitIds = coverageMemberUnitIds(units);
  const roles = new Map<string, CoverageWorkspaceRole>();
  for (const u of units) {
    if (!u.workspace_id) continue;
    if (u.kind === 'sector') {
      roles.set(u.workspace_id, { kind: 'sector', sectorNames: [], sectors: [], unit: u });
    } else if (!memberUnitIds.has(u.unit_id)) {
      roles.set(u.workspace_id, { kind: 'standalone', sectorNames: [], sectors: [] });
    } else {
      const sectors: CoverageRoleSector[] = [];
      for (const s of units) {
        if (s.kind !== 'sector') continue;
        if (!(s.members ?? []).some((m) => m.company_unit_id === u.unit_id)) continue;
        sectors.push({
          unitId: s.unit_id,
          displayName: s.display_name ?? s.slug ?? s.unit_id,
          workspaceId: s.workspace_id,
          readable: s.readable,
        });
      }
      roles.set(u.workspace_id, {
        kind: 'member',
        sectorNames: sectors.map((s) => s.displayName),
        sectors,
      });
    }
  }
  return roles.size > 0 ? roles : EMPTY_ROLES;
}

/**
 * workspace_id → coverage role, built once per listing for every consumer
 * (sidebar tree chips, gallery affiliation bar). Sharing one query through
 * React Query means one request; the map itself is a single useMemo, never
 * per-row. Gated exactly like DaltonCoverageCard — no auth-mode gate, the
 * listing's `enabled:false` (or its failure) is what hides every surface —
 * so an empty map (no chips, no bar) is also what a non-Dalton deploy sees.
 * A failed refresh keeps no stale roles: `isError` collapses to EMPTY_ROLES.
 */
export function useCoverageRoleByWorkspace(): ReadonlyMap<string, CoverageWorkspaceRole> {
  const { data, isError } = useDaltonCoverageUnits();
  return useMemo(
    () => (isError ? EMPTY_ROLES : deriveCoverageWorkspaceRoles(data?.enabled ? data.units : [])),
    [data, isError],
  );
}

export function useDaltonCoverageUnitDetail(unitId: string | null) {
  return useQuery({
    queryKey: queryKeys.dalton.coverageUnitDetail(unitId ?? ''),
    queryFn: () => fetchDaltonCoverageUnitDetail(unitId as string),
    enabled: unitId !== null,
    staleTime: 30_000,
  });
}

// ---------------------------------------------------------------------------
// P5 写路径：登记 / 绑定 / 解绑 / 归档。服务层校验失败时后端返回
// 4xx，detail 为 `{error: <code>, message: string}`；code 是稳定错误码，
// UI 按 dalton.coverage.errors.<code> 选 i18n 文案，message 仅兜底。
// ---------------------------------------------------------------------------

/** `dalton_coverage_units` 行（服务层 _UNIT_COLS 投影），与写端点响应一致。 */
export interface CoverageUnitRecord {
  unit_id: string;
  kind: CoverageUnitKind;
  owner_user_id: string;
  slug: string;
  display_name: string;
  workspace_id: string | null;
  ticker: string | null;
  status: string;
  research_tz: string | null;
  created_at: string;
  updated_at: string;
}

export interface RegisterCoverageUnitPayload {
  kind: CoverageUnitKind;
  slug: string;
  display_name?: string;
  ticker?: string | null;
  research_tz?: string | null;
}

/** 从被拒请求里取稳定错误码；非本写路径的错误（无结构 detail）返回 null。 */
export function coverageUnitErrorCode(err: unknown): string | null {
  const detail = (
    err as { response?: { data?: { detail?: unknown } } } | null
  )?.response?.data?.detail;
  if (
    detail &&
    typeof detail === 'object' &&
    typeof (detail as { error?: unknown }).error === 'string' &&
    (detail as { error?: unknown }).error
  ) {
    return (detail as { error: string }).error;
  }
  return null;
}

export async function registerDaltonCoverageUnit(
  payload: RegisterCoverageUnitPayload,
): Promise<CoverageUnitRecord> {
  const { data } = await api.post<CoverageUnitRecord>(
    '/api/v1/dalton/coverage/units',
    payload,
  );
  return data;
}

export async function bindDaltonCoverageUnit(
  unitId: string,
  workspaceId: string,
): Promise<CoverageUnitRecord> {
  const { data } = await api.post<CoverageUnitRecord>(
    `/api/v1/dalton/coverage/units/${encodeURIComponent(unitId)}/bind`,
    { workspace_id: workspaceId },
  );
  return data;
}

export async function unbindDaltonCoverageUnit(
  unitId: string,
): Promise<CoverageUnitRecord> {
  const { data } = await api.post<CoverageUnitRecord>(
    `/api/v1/dalton/coverage/units/${encodeURIComponent(unitId)}/unbind`,
  );
  return data;
}

export async function archiveDaltonCoverageUnit(
  unitId: string,
): Promise<CoverageUnitRecord> {
  const { data } = await api.post<CoverageUnitRecord>(
    `/api/v1/dalton/coverage/units/${encodeURIComponent(unitId)}/archive`,
  );
  return data;
}
