import React from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Building2, Layers } from 'lucide-react';
import {
  coverageNoEntryReason,
  useCoverageRoleByWorkspace,
  type CoverageRoleSector,
} from '@/lib/dalton/coverage';

// 与同页"共享设置"按钮同款 pill（rounded-full border + transition-colors +
// hover:bg-foreground/5，WorkspaceSharingModal 的触发按钮）。
const CHIP_CLASS =
  'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] transition-colors';
const CHIP_STYLE = {
  borderColor: 'var(--color-border-muted)',
} as const;

/** 可进入的 chip：文字 secondary（共享设置按钮同款），hover 态同款。 */
function EnterableChip({ label, workspaceId }: { label: string; workspaceId: string }) {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      data-testid="coverage-affiliation-chip"
      onClick={() => navigate(`/chat/${workspaceId}`)}
      className={`${CHIP_CLASS} cursor-pointer hover:bg-foreground/5`}
      style={{ ...CHIP_STYLE, color: 'var(--color-text-secondary)' }}
    >
      {label}
    </button>
  );
}

/** 不可进入的 chip：quaternary 文字（静态下也能与可点 chip 区分）、
 *  不响应 hover，tooltip 说明是未建还是无法访问。 */
function PlainChip({ label, reason }: { label: string; reason: string }) {
  return (
    <span
      data-testid="coverage-affiliation-chip"
      className={`${CHIP_CLASS} cursor-default`}
      style={{ ...CHIP_STYLE, color: 'var(--color-text-quaternary)' }}
      title={reason}
    >
      {label}
    </span>
  );
}

function SectorChip({ sector }: { sector: CoverageRoleSector }) {
  const { t } = useTranslation();
  // 行业绑定了工作区且本人可读时点名进入；否则只显示名字，tooltip
  // 区分"未建工作区"与"无法访问"（同事的 delegatable-only 单元）。
  if (sector.workspaceId && sector.readable) {
    return <EnterableChip label={sector.displayName} workspaceId={sector.workspaceId} />;
  }
  const reason = sector.workspaceId
    ? t('dalton.coverage.noAccess')
    : t('dalton.coverage.noWorkspace');
  return <PlainChip label={sector.displayName} reason={reason} />;
}

/**
 * 工作区页头部的从属关系条（行业层可视化，只读）：
 * 行业工作区显示成员 chips；成员公司显示所属行业；独立公司显示身份。
 * 数据即开关——未分类工作区和 Home 不在覆盖角色映射里，自然不渲染。
 */
export function CoverageAffiliationBar({
  workspaceId,
}: {
  workspaceId: string;
}): React.ReactElement | null {
  const { t } = useTranslation();
  const roles = useCoverageRoleByWorkspace();
  const role = roles.get(workspaceId);
  if (!role) return null;

  if (role.kind === 'sector') {
    const members = role.unit.members ?? [];
    return (
      <div
        data-testid="coverage-affiliation-bar"
        className="mt-2 flex flex-wrap items-center justify-center gap-1.5 text-xs"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        <span className="inline-flex items-center gap-1">
          <Layers className="h-3.5 w-3.5" />
          {t('dalton.coverage.sectorChip')} · {role.unit.display_name ?? role.unit.slug} ·{' '}
          {t('dalton.coverage.memberCount', { count: members.length })}
        </span>
        {members.map((member, index) => {
          const label = member.name ?? member.ticker ?? member.member_id ?? '';
          const key = member.member_id ?? member.company_unit_id ?? `${label}-${index}`;
          // 成员的 workspace_id 由后端按查看者过滤：有值即可进入；无值时
          // tooltip 区分未建（workspace_created false）与无法访问。
          return member.workspace_id ? (
            <EnterableChip
              key={key}
              label={member.ticker ? `${label} (${member.ticker})` : label}
              workspaceId={member.workspace_id}
            />
          ) : (
            <PlainChip
              key={key}
              label={member.ticker ? `${label} (${member.ticker})` : label}
              reason={t(`dalton.coverage.${coverageNoEntryReason(member.workspace_created)}`)}
            />
          );
        })}
      </div>
    );
  }

  if (role.kind === 'member') {
    return (
      <div
        data-testid="coverage-affiliation-bar"
        className="mt-2 flex flex-wrap items-center justify-center gap-1.5 text-xs"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        <span>{t('dalton.coverage.belongsTo')}</span>
        {role.sectors.map((sector) => (
          <SectorChip key={sector.unitId} sector={sector} />
        ))}
      </div>
    );
  }

  return (
    <div
      data-testid="coverage-affiliation-bar"
      className="mt-2 flex items-center justify-center gap-1.5 text-xs"
      style={{ color: 'var(--color-text-tertiary)' }}
    >
      <Building2 className="h-3.5 w-3.5" />
      {t('dalton.coverage.standaloneCompany')}
    </div>
  );
}
