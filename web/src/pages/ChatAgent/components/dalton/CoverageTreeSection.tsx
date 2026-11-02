import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Building2, ChevronRight, Folder, Layers } from 'lucide-react';
import {
  coverageNoEntryReason,
  splitCoverageUnits,
  useDaltonCoverageUnits,
  type CoverageUnitEntry,
  type SectorMember,
} from '@/lib/dalton/coverage';
import './coverageTree.css';

/** Enter / Space activate a clickable row, as the workspace rows' handle does. */
function rowKeyDown(activate: () => void) {
  return (e: React.KeyboardEvent<HTMLDivElement>) => {
    // A key pressed on a control inside the row (the collapse chevron) is that
    // control's; claiming it here would navigate instead of toggling.
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      activate();
    }
  };
}

/** 折叠状态持久化键；缺省（无记录）为展开。 */
const SECTION_COLLAPSED_KEY = 'sidebar.coverageTree.collapsed';

// NavigationRows 的 CHEVRON_CLASS 同串：与工作区行的展开箭头同一套
// 尺寸与折叠动画（h-4 w-4 + duration-fold motion token）。
const CHEVRON_CLASS = 'h-4 w-4 transition-transform duration-fold';


// 隐私模式下 localStorage 读写会抛错：失败即退回默认展开，不持久化。
function readSectionCollapsed(): boolean {
  try {
    return localStorage.getItem(SECTION_COLLAPSED_KEY) === 'true';
  } catch {
    return false;
  }
}

function writeSectionCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(SECTION_COLLAPSED_KEY, String(collapsed));
  } catch {
    // 无法持久化时保持会话内状态即可
  }
}

/**
 * 侧栏"覆盖结构"区块（行业层可视化）：行业 → 成员公司两级树，独立公司
 * 与未分类数量并列在后。门控与设置页覆盖卡片完全一致——后端
 * enabled:false、接口失败（含刷新失败，isError）、或没有任何本人可读
 * 单元时整个区块不渲染。只读展示；管理入口在设置页。
 *
 * 只列本人可读的单元（readable）：只能委派、不可读的同事单元不进导航
 * 树。视觉度量全部取自侧栏现有样式：区块头 = "工作区"区块的
 * sidebar-section-header / -title / -actions；行 = nav-panel-row 一系
 * （行业/独立公司行同工作区行度量，成员行同线程行缩进）；当前项与工作
 * 区行一样只把文字提到主色，不加底色块。
 */
export function CoverageTreeSection({
  currentWorkspaceId,
}: {
  currentWorkspaceId?: string | null;
}): React.ReactElement | null {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { data, isError } = useDaltonCoverageUnits();
  const [sectionExpanded, setSectionExpanded] = useState(() => !readSectionCollapsed());
  const [expandedSectors, setExpandedSectors] = useState<Set<string>>(() => new Set());

  // 只列本人可读的单元；成员公司判定也基于过滤后的行业集合。
  const split = useMemo(
    () => (data?.enabled && !isError
      ? splitCoverageUnits(data.units.filter((u) => u.readable))
      : null),
    [data, isError],
  );

  const toggleSection = useCallback(() => {
    setSectionExpanded((prev) => !prev);
  }, []);

  // 折叠状态随每次变化写入 localStorage（含首次挂载写回默认值，幂等；
  // 写入失败即静默放弃持久化）。
  useEffect(() => {
    writeSectionCollapsed(!sectionExpanded);
  }, [sectionExpanded]);

  const toggleSector = useCallback((unitId: string) => {
    setExpandedSectors((prev) => {
      const next = new Set(prev);
      if (next.has(unitId)) next.delete(unitId);
      else next.add(unitId);
      return next;
    });
  }, []);

  // 当前工作区是某行业的成员时自动展开该行业（只加不减：用户随后手动
  // 收起不被覆盖，与工作区树自动展开当前工作区同一行为）。
  useEffect(() => {
    if (!split || !currentWorkspaceId) return;
    const holders: string[] = [];
    for (const sector of split.sectors) {
      if ((sector.members ?? []).some((m) => m.workspace_id === currentWorkspaceId)) {
        holders.push(sector.unit_id);
      }
    }
    if (holders.length === 0) return;
    setExpandedSectors((prev) => {
      const next = new Set(prev);
      let changed = false;
      for (const id of holders) {
        if (!next.has(id)) {
          next.add(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [split, currentWorkspaceId]);

  // 刷新失败（isError）不显示旧数据：与 enabled:false 同样整个隐藏。
  if (isError || !data?.enabled) return null;
  if (!data.units.some((u) => u.readable)) return null;
  if (!split || (split.sectors.length === 0 && split.standaloneCompanies.length === 0)) return null;

  const unclassifiedCount = data.unclassified?.length ?? 0;
  const enterWorkspace = (wsId: string) => navigate(`/chat/${wsId}`);

  return (
    <div className="shrink-0" data-testid="coverage-tree-section">
      {/* 与"工作区"区块同款标题：sidebar-section-header 自带上下间距，
          折叠按钮是 actions 区的同款 action 按钮（hover 才显示）。 */}
      <div className="sidebar-section-header">
        <button
          type="button"
          className="sidebar-section-title"
          onClick={toggleSection}
          aria-expanded={sectionExpanded}
          data-testid="coverage-tree-toggle"
        >
          {t('dalton.coverage.treeTitle')}
        </button>
        <div className="sidebar-section-actions">
          <button
            type="button"
            className="nav-panel-dismiss-btn sidebar-section-action-btn"
            onClick={toggleSection}
            aria-label={t('dalton.coverage.toggleSection')}
            title={t('dalton.coverage.toggleSection')}
          >
            <ChevronRight
              className={`h-3.5 w-3.5 transition-transform duration-fold${sectionExpanded ? ' rotate-90' : ''}`}
              style={{ color: 'var(--color-text-tertiary)' }}
            />
          </button>
        </div>
      </div>

      {sectionExpanded && (
        <div className="flex flex-col py-2" data-testid="coverage-tree-body">
          {split.sectors.map((sector) => (
            <SectorRow
              key={sector.unit_id}
              sector={sector}
              expanded={expandedSectors.has(sector.unit_id)}
              currentWorkspaceId={currentWorkspaceId}
              onToggle={() => toggleSector(sector.unit_id)}
              onEnterWorkspace={enterWorkspace}
            />
          ))}

          {split.standaloneCompanies.length > 0 && (
            <div data-testid="coverage-tree-standalone-group">
              {/* 分组标签：区块标题同款 class（sidebar-section-header 自带
                  间距），不可点、不变色（pointer-events-none 不触发 hover）。 */}
              <div className="sidebar-section-header">
                <div className="sidebar-section-title pointer-events-none">
                  {t('dalton.coverage.tabCompanies')}
                </div>
              </div>
              {split.standaloneCompanies.map((unit) => {
                const active = !!unit.workspace_id && unit.workspace_id === currentWorkspaceId;
                const enterable = !!unit.workspace_id && unit.readable;
                const label = unit.display_name ?? unit.slug ?? unit.unit_id;
                return enterable ? (
                  <div
                    key={unit.unit_id}
                    className="nav-panel-row"
                    style={{ paddingLeft: 10 }}
                    data-testid={`coverage-tree-standalone-${unit.unit_id}`}
                    data-active={active || undefined}
                    role="button"
                    tabIndex={0}
                    onClick={() => enterWorkspace(unit.workspace_id as string)}
                    onKeyDown={rowKeyDown(() => enterWorkspace(unit.workspace_id as string))}
                  >
                    <Building2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                    <span
                      className="text-sm font-medium truncate"
                      style={{ color: active ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)' }}
                    >
                      {label}
                    </span>
                    {unit.ticker && (
                      <span className="ml-auto shrink-0 text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                        {unit.ticker}
                      </span>
                    )}
                  </div>
                ) : (
                  <div
                    key={unit.unit_id}
                    className="nav-panel-row coverage-row-inert"
                    style={{ paddingLeft: 10 }}
                    data-testid={`coverage-tree-standalone-${unit.unit_id}`}
                    title={t('dalton.coverage.noWorkspace')}
                  >
                    {/* 未绑定工作区：图标与文字同为 quaternary，弱化为不可进。 */}
                    <Building2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-quaternary)' }} />
                    <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-quaternary)' }}>
                      {label}
                    </span>
                    {unit.ticker && (
                      <span className="ml-auto shrink-0 text-[11px]" style={{ color: 'var(--color-text-quaternary)' }}>
                        {unit.ticker}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {unclassifiedCount > 0 && (
            <div
              className="nav-panel-row"
              style={{ paddingLeft: 10 }}
              data-testid="coverage-tree-unclassified"
              title={t('dalton.coverage.openSettings')}
              role="button"
              tabIndex={0}
              onClick={() => navigate('/settings?tab=model#dalton-coverage-unclassified')}
              onKeyDown={rowKeyDown(() => navigate('/settings?tab=model#dalton-coverage-unclassified'))}
            >
              <Folder className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
              <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('dalton.coverage.unclassifiedLabel')}
              </span>
              <span className="ml-auto shrink-0 text-[11px] tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>
                {unclassifiedCount}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * One sector row on the workspace row's own metrics (nav-panel-row, h-4 w-4
 * icon, text-sm font-medium). Clicking the row enters the sector's bound
 * readable workspace, or toggles the member list; the trailing chevron lives
 * in the workspace row's own hover overlay (nav-panel-row-actions) so it
 * never takes width from the name. A sector with neither a workspace nor
 * members is inert: no pointer, no hover tint, just the tooltip.
 */
function SectorRow({
  sector,
  expanded,
  currentWorkspaceId,
  onToggle,
  onEnterWorkspace,
}: {
  sector: CoverageUnitEntry;
  expanded: boolean;
  currentWorkspaceId?: string | null;
  onToggle: () => void;
  onEnterWorkspace: (wsId: string) => void;
}) {
  const { t } = useTranslation();
  const members = sector.members ?? [];
  const label = sector.display_name ?? sector.slug ?? sector.unit_id;
  const active = !!sector.workspace_id && sector.workspace_id === currentWorkspaceId;
  // 绑定了可读工作区的行业：点行进入；否则点行即展开/收起。
  const enterable = !!sector.workspace_id && sector.readable;
  const inert = !enterable && members.length === 0;

  if (inert) {
    return (
      <div
        className="nav-panel-row coverage-row-inert"
        style={{ paddingLeft: 10 }}
        data-testid={`coverage-tree-sector-${sector.unit_id}`}
        title={t('dalton.coverage.noWorkspace')}
      >
        <Layers className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-quaternary)' }} />
        <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-quaternary)' }}>
          {label}
        </span>
        <span className="ml-auto shrink-0 text-[11px] tabular-nums" style={{ color: 'var(--color-text-quaternary)' }}>
          {members.length}
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col" data-testid={`coverage-tree-sector-group-${sector.unit_id}`}>
      <div
        className="nav-panel-row group"
        style={{ paddingLeft: 10, position: 'relative' }}
        data-testid={`coverage-tree-sector-${sector.unit_id}`}
        data-active={active || undefined}
        role="button"
        tabIndex={0}
        onClick={enterable ? () => onEnterWorkspace(sector.workspace_id as string) : onToggle}
        onKeyDown={rowKeyDown(enterable ? () => onEnterWorkspace(sector.workspace_id as string) : onToggle)}
      >
        <Layers className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
        <span
          className="text-sm font-medium truncate"
          style={{ color: active ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)' }}
        >
          {label}
        </span>
        <span className="ml-auto shrink-0 text-[11px] tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>
          {members.length}
        </span>
        {members.length > 0 && (
          /* 工作区行同款行尾浮层：hover / 键盘聚焦 / 触屏才显示，不占名称宽度。 */
          <div className="nav-panel-row-actions">
            <button
              type="button"
              data-testid={`coverage-tree-sector-toggle-${sector.unit_id}`}
              onClick={(e) => {
                e.stopPropagation();
                onToggle();
              }}
              aria-expanded={expanded}
              aria-label={label}
              className="flex shrink-0 items-center justify-center p-0.5 rounded bg-transparent border-none cursor-pointer"
            >
              <ChevronRight
                className={`${CHEVRON_CLASS}${expanded ? ' rotate-90' : ''}`}
                style={{ color: 'var(--color-text-tertiary)' }}
              />
            </button>
          </div>
        )}
      </div>

      {expanded && members.length > 0 && (
        <div className="flex flex-col" data-testid={`coverage-tree-members-${sector.unit_id}`}>
          {members.map((member, index) => (
            <MemberRow
              key={member.member_id ?? member.company_unit_id ?? `${member.name}-${index}`}
              member={member}
              active={!!member.workspace_id && member.workspace_id === currentWorkspaceId}
              onEnterWorkspace={onEnterWorkspace}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * One member company row on the workspace row's own metrics with the thread
 * row's indent (nav-panel-row, paddingLeft 28, text-sm font-medium; primary
 * when current, tertiary otherwise — hierarchy rides the indent alone). The
 * backend only outputs a member's workspace_id when the viewer may read it;
 * a greyed row distinguishes "no workspace yet" from "not accessible" in its
 * tooltip. Keyboard handling matches the upstream workspace row: none on the
 * row itself (its chevron button is natively focusable).
 */
function MemberRow({
  member,
  active,
  onEnterWorkspace,
}: {
  member: SectorMember;
  active: boolean;
  onEnterWorkspace: (wsId: string) => void;
}) {
  const { t } = useTranslation();
  const label = member.name ?? member.ticker ?? member.member_id ?? '';
  const testId = `coverage-tree-member-${member.member_id ?? member.company_unit_id ?? label}`;
  const ticker = member.ticker ? (
    <span
      className="ml-auto shrink-0 text-[11px]"
      style={{ color: member.workspace_id ? 'var(--color-text-tertiary)' : 'var(--color-text-quaternary)' }}
    >
      {member.ticker}
    </span>
  ) : null;

  if (member.workspace_id) {
    return (
      <div
        className="nav-panel-row"
        style={{ paddingLeft: 28 }}
        data-testid={testId}
        data-active={active || undefined}
        role="button"
        tabIndex={0}
        onClick={() => onEnterWorkspace(member.workspace_id as string)}
        onKeyDown={rowKeyDown(() => onEnterWorkspace(member.workspace_id as string))}
      >
        <Building2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
        <span
          className="text-sm font-medium truncate"
          style={{ color: active ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)' }}
        >
          {label}
        </span>
        {ticker}
      </div>
    );
  }

  // 未建工作区（workspace_created false）与无法访问（已建但按可见性
  // 过滤掉 workspace_id）共用置灰样式，tooltip 分开说明。
  return (
    <div
      className="nav-panel-row coverage-row-inert"
      style={{ paddingLeft: 28 }}
      data-testid={testId}
      title={t(`dalton.coverage.${coverageNoEntryReason(member.workspace_created)}`)}
    >
      <Building2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-quaternary)' }} />
      <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-quaternary)' }}>
        {label}
      </span>
      {ticker}
    </div>
  );
}
