import React, { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, ExternalLink, Loader2, RefreshCw } from 'lucide-react';
import {
  type CoverageUnitDetail,
  type CoverageUnitEntry,
  type SectorMember,
  useDaltonCoverageUnitDetail,
  useDaltonCoverageUnits,
} from '@/lib/dalton/coverage';
import { cn } from '@/lib/utils';

type CoverageTab = 'sectors' | 'companies' | 'unclassified';

function StateBadge({ label, on }: { label: string; on: boolean }) {
  return (
    <span
      data-testid={`coverage-badge-${label}`}
      className={cn(
        'rounded-full px-2 py-0.5 text-[11px] font-medium',
        on
          ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
          : 'bg-muted text-muted-foreground',
      )}
    >
      {label}
    </span>
  );
}

function UnitBadges({ unit }: { unit: CoverageUnitEntry }) {
  const { t } = useTranslation();
  return (
    <span className="flex flex-wrap items-center gap-1">
      <StateBadge label={t('dalton.coverage.readable')} on={unit.readable} />
      <StateBadge label={t('dalton.coverage.delegatable')} on={unit.delegatable} />
      <StateBadge label={t('dalton.coverage.autoTracked')} on={unit.auto_tracked} />
    </span>
  );
}

function OpenWorkspaceLink({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <button
      type="button"
      data-testid={`coverage-open-workspace-${workspaceId}`}
      onClick={(e) => {
        e.stopPropagation();
        navigate(`/chat/${workspaceId}`);
      }}
      className="inline-flex items-center gap-1 rounded border border-border/60 bg-background px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted/60 hover:text-foreground"
    >
      <ExternalLink className="h-3 w-3" />
      {t('dalton.coverage.openWorkspace')}
    </button>
  );
}

/** 行业成员行：登记了公司单元的成员可展开该公司详情（关联行业 + 待复核）。 */
function MemberRow({
  member,
  expanded,
  onToggle,
}: {
  member: SectorMember;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const detailQuery = useDaltonCoverageUnitDetail(
    expanded && member.company_unit_id ? member.company_unit_id : null,
  );
  const hasUnit = Boolean(member.company_unit_id);
  return (
    <div
      data-testid={`coverage-member-${member.member_id ?? member.name ?? ''}`}
      className="py-1"
    >
      <div className="flex flex-wrap items-center gap-2">
        {hasUnit ? (
          <button
            type="button"
            onClick={onToggle}
            data-testid={`coverage-member-toggle-${member.member_id ?? ''}`}
            className="inline-flex items-center gap-1 text-xs text-foreground hover:underline"
          >
            {expanded ? (
              <ChevronDown className="h-3 w-3 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3 w-3 text-muted-foreground" />
            )}
            {member.name}
          </button>
        ) : (
          <span className="text-xs text-foreground">{member.name}</span>
        )}
        {member.ticker && (
          <code className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
            {member.ticker}
          </code>
        )}
        <span
          data-testid={
            member.workspace_created
              ? `coverage-member-covered-${member.member_id ?? ''}`
              : `coverage-member-uncovered-${member.member_id ?? ''}`
          }
          className={cn(
            'rounded-full px-2 py-0.5 text-[11px] font-medium',
            member.workspace_created
              ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
              : 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
          )}
        >
          {member.workspace_created
            ? t('dalton.coverage.covered')
            : t('dalton.coverage.notCovered')}
        </span>
        {member.scope && (
          <span className="text-[11px] text-muted-foreground">{member.scope}</span>
        )}
        {member.workspace_id && <OpenWorkspaceLink workspaceId={member.workspace_id} />}
      </div>
      {hasUnit && expanded && (
        <div className="ml-4 border-l border-border/40 pl-3">
          {detailQuery.isLoading ? (
            <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t('dalton.coverage.detailLoading')}
            </div>
          ) : detailQuery.isError ? (
            <div className="py-2 text-xs text-destructive">
              {t('dalton.coverage.detailError')}
            </div>
          ) : detailQuery.data ? (
            <CompanyDetail detail={detailQuery.data} />
          ) : null}
        </div>
      )}
    </div>
  );
}

function DetailSection({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
        {typeof count === 'number' ? ` (${count})` : ''}
      </div>
      {children}
    </div>
  );
}

function SectorDetail({ detail }: { detail: CoverageUnitDetail }) {
  const { t } = useTranslation();
  const [expandedMember, setExpandedMember] = useState<string | null>(null);
  const sector = detail.sector;
  if (!sector) return null;
  const docs = sector.docs;
  return (
    <div className="flex flex-col gap-3 pt-1" data-testid={`coverage-detail-${detail.unit_id}`}>
      <DetailSection title={t('dalton.coverage.thesis')}>
        {docs?.thesis ? (
          <div className="space-y-1 text-xs text-muted-foreground">
            {docs.thesis.one_liner && <div>{docs.thesis.one_liner}</div>}
            <div className="flex flex-wrap items-center gap-2">
              {docs.thesis.stance && (
                <span>
                  {t('dalton.coverage.stance')}: {docs.thesis.stance}
                </span>
              )}
              {docs.thesis.conviction && (
                <span>
                  {t('dalton.coverage.conviction')}: {docs.thesis.conviction}
                </span>
              )}
              <span>
                {t('dalton.coverage.pillarCount', { count: docs.thesis.pillars.length })}
              </span>
            </div>
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">{t('dalton.coverage.noDoc')}</div>
        )}
      </DetailSection>

      <DetailSection title={t('dalton.coverage.drivers')}>
        {docs?.drivers && docs.drivers.drivers.length > 0 ? (
          <div className="space-y-1">
            {docs.drivers.drivers.map((d) => (
              <div key={d.id ?? d.name ?? ''} data-testid={`coverage-driver-${d.id ?? ''}`}>
                <code className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {d.name ?? d.id}
                </code>
                {/* 指标名与最新读数在 driver 名下展开（codex p4 r1 #4） */}
                {d.leading_indicators.length > 0 && (
                  <ul className="mt-0.5 space-y-0.5 pl-3">
                    {d.leading_indicators.map((ind) => (
                      <li
                        key={ind.id ?? ind.name ?? ''}
                        data-testid={`coverage-indicator-${ind.id ?? ''}`}
                        className="text-[11px] text-muted-foreground"
                      >
                        {ind.name ?? ind.id}
                        {ind.latest && ind.latest.value != null && (
                          <>
                            ：{ind.latest.value}
                            {ind.latest.unit ? ` ${ind.latest.unit}` : ''}
                            {ind.latest.period ? `（${ind.latest.period}）` : ''}
                            {ind.latest.as_of ? ` · ${String(ind.latest.as_of).slice(0, 10)}` : ''}
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">{t('dalton.coverage.noDoc')}</div>
        )}
      </DetailSection>

      <DetailSection title={t('dalton.coverage.agendaTodos')}>
        {sector.agenda_todos ? (
          <div className="space-y-1 text-xs text-muted-foreground">
            {sector.agenda_todos.questions.map((q) => (
              <div key={q.id} data-testid={`coverage-agenda-question-${q.id}`}>
                · {q.question}
                {q.waiting_pm && (
                  <span
                    data-testid={`coverage-waiting-pm-${q.id}`}
                    className="ml-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-600 dark:text-amber-400"
                  >
                    {t('dalton.coverage.waitingPm')}
                  </span>
                )}
              </div>
            ))}
            {sector.agenda_todos.validations.map((v) => (
              <div key={v.id}>
                · {t('dalton.coverage.agendaValidations')}: {v.what} ({v.due ?? '—'})
              </div>
            ))}
            {sector.agenda_todos.events.map((e, i) => (
              <div key={`${e.title}-${i}`}>
                · {e.title} ({e.date ?? '—'})
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">{t('dalton.coverage.noDoc')}</div>
        )}
      </DetailSection>

      <DetailSection title={t('dalton.coverage.members')} count={sector.members.length}>
        <div className="divide-y divide-border/40">
          {sector.members.map((m) => (
            <MemberRow
              key={m.member_id ?? m.name ?? ''}
              member={m}
              expanded={expandedMember === (m.member_id ?? '')}
              onToggle={() =>
                setExpandedMember((prev) =>
                  prev === (m.member_id ?? '') ? null : (m.member_id ?? ''),
                )
              }
            />
          ))}
        </div>
      </DetailSection>

      <DetailSection title={t('dalton.coverage.evidenceGaps')} count={sector.evidence_gaps.length}>
        {sector.evidence_gaps.length === 0 ? (
          <div className="text-xs text-muted-foreground">{t('dalton.coverage.none')}</div>
        ) : (
          <div className="space-y-1 text-xs text-muted-foreground">
            {sector.evidence_gaps.map((g) => (
              <div key={g.request_id ?? g.question ?? ''} data-testid={`coverage-evidence-gap-${g.request_id ?? ''}`}>
                · {g.question}
              </div>
            ))}
          </div>
        )}
      </DetailSection>

      <DetailSection title={t('dalton.coverage.ledgerGaps')} count={sector.ledger_gaps.length}>
        {sector.ledger_gaps.length === 0 ? (
          <div className="text-xs text-muted-foreground">{t('dalton.coverage.none')}</div>
        ) : (
          <div className="space-y-1 text-xs text-muted-foreground">
            {sector.ledger_gaps.map((g) => (
              <div key={g.request_id ?? g.question ?? ''} data-testid={`coverage-ledger-gap-${g.request_id ?? ''}`}>
                · {g.question}
              </div>
            ))}
          </div>
        )}
        {/* 台账在 SQL 里先过滤再截断（codex p4 r1 #2）：total 超过展示条数时提示 */}
        {sector.pending_total >
          sector.evidence_gaps.length + sector.ledger_gaps.length && (
          <div
            data-testid={`coverage-pending-truncated-${detail.unit_id}`}
            className="pt-1 text-[11px] text-muted-foreground"
          >
            {t('dalton.coverage.pendingTruncated', {
              count: sector.evidence_gaps.length + sector.ledger_gaps.length,
              total: sector.pending_total,
            })}
          </div>
        )}
      </DetailSection>
    </div>
  );
}

function CompanyDetail({ detail }: { detail: CoverageUnitDetail }) {
  const { t } = useTranslation();
  const company = detail.company;
  if (!company) return null;
  return (
    <div className="flex flex-col gap-3 pt-1" data-testid={`coverage-detail-${detail.unit_id}`}>
      {company.docs?.thesis?.one_liner && (
        <DetailSection title={t('dalton.coverage.thesis')}>
          <div className="text-xs text-muted-foreground">{company.docs.thesis.one_liner}</div>
        </DetailSection>
      )}
      <DetailSection title={t('dalton.coverage.relatedSectors')} count={company.sectors.length}>
        {company.sectors.length === 0 ? (
          <div className="text-xs text-muted-foreground">{t('dalton.coverage.none')}</div>
        ) : (
          <div className="divide-y divide-border/40">
            {company.sectors.map((s) => (
              <div
                key={s.unit_id}
                data-testid={`coverage-related-sector-${s.unit_id}`}
                className="flex flex-wrap items-center gap-2 py-1"
              >
                <span className="text-xs text-foreground">{s.display_name ?? s.slug}</span>
                {s.scope && (
                  <span className="text-[11px] text-muted-foreground">{s.scope}</span>
                )}
                <StateBadge label={t('dalton.coverage.readable')} on={s.readable} />
                {s.workspace_id && <OpenWorkspaceLink workspaceId={s.workspace_id} />}
              </div>
            ))}
          </div>
        )}
      </DetailSection>
      <DetailSection title={t('dalton.coverage.reReview')} count={company.re_review.length}>
        {company.re_review.length === 0 ? (
          <div className="text-xs text-muted-foreground">{t('dalton.coverage.none')}</div>
        ) : (
          <div className="space-y-1 text-xs text-muted-foreground">
            {company.re_review.map((r) => (
              <div key={r.id} data-testid={`coverage-re-review-${r.id}`}>
                · {r.question}
              </div>
            ))}
          </div>
        )}
      </DetailSection>
    </div>
  );
}

function UnitRow({ unit, expanded, onToggle }: {
  unit: CoverageUnitEntry;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const detailQuery = useDaltonCoverageUnitDetail(expanded ? unit.unit_id : null);
  return (
    <div className="py-1" data-testid={`coverage-unit-${unit.unit_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onToggle}
          data-testid={`coverage-unit-toggle-${unit.unit_id}`}
          className="flex min-w-0 flex-1 flex-wrap items-center gap-2 rounded px-1 py-1 text-left hover:bg-muted/40"
        >
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <span className="text-xs font-medium text-foreground">
            {unit.display_name ?? unit.slug}
          </span>
          {unit.kind === 'company' && unit.ticker && (
            <code className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
              {unit.ticker}
            </code>
          )}
          <UnitBadges unit={unit} />
        </button>
        {/* 已绑定工作区的单元自身可直接进入（codex p4 r1 #1）；未绑定的不显示 */}
        {unit.workspace_id && <OpenWorkspaceLink workspaceId={unit.workspace_id} />}
      </div>
      {expanded && (
        <div className="ml-5 border-l border-border/40 pl-3">
          {detailQuery.isLoading ? (
            <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t('dalton.coverage.detailLoading')}
            </div>
          ) : detailQuery.isError ? (
            <div className="py-2 text-xs text-destructive">{t('dalton.coverage.detailError')}</div>
          ) : detailQuery.data ? (
            unit.kind === 'sector' ? (
              <SectorDetail detail={detailQuery.data} />
            ) : (
              <CompanyDetail detail={detailQuery.data} />
            )
          ) : null}
        </div>
      )}
    </div>
  );
}

/**
 * Dalton 覆盖视图（行业层 P4）：行业 / 独立公司 / 未分类工作区三个入口。
 *
 * 开关状态来自后端（列表响应的 `enabled` 字段）——`enabled:false` 或尚未
 * 加载时整个卡片不渲染，前端不做任何开关推断。权限同样只展示后端返回的
 * readable/delegatable/auto_tracked，不在前端二次判断。
 */
export function DaltonCoverageCard(): React.ReactElement | null {
  const { t } = useTranslation();
  const [tab, setTab] = useState<CoverageTab>('sectors');
  const [expandedUnit, setExpandedUnit] = useState<string | null>(null);
  const { data, isLoading, isFetching, refetch, error } = useDaltonCoverageUnits();

  const units = data?.enabled ? data.units : [];
  // 独立公司 = 没有被任何（可见）行业列为成员的公司单元
  const memberUnitIds = useMemo(() => {
    const ids = new Set<string>();
    for (const u of units) {
      for (const m of u.members ?? []) {
        if (m.company_unit_id) ids.add(m.company_unit_id);
      }
    }
    return ids;
  }, [units]);

  // 开关关闭（或还在加载/失败）时不渲染任何行业相关入口与文案
  if (!data || data.enabled === false) return null;

  const sectors = units.filter((u) => u.kind === 'sector');
  const standaloneCompanies = units.filter(
    (u) => u.kind === 'company' && !memberUnitIds.has(u.unit_id),
  );
  const unclassified = data.unclassified ?? [];

  const tabs: Array<{ key: CoverageTab; label: string; count: number }> = [
    { key: 'sectors', label: t('dalton.coverage.tabSectors'), count: sectors.length },
    {
      key: 'companies',
      label: t('dalton.coverage.tabCompanies'),
      count: standaloneCompanies.length,
    },
    {
      key: 'unclassified',
      label: t('dalton.coverage.tabUnclassified'),
      count: unclassified.length,
    },
  ];

  return (
    <div
      className="rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-coverage-card"
    >
      <div className="flex items-center justify-between gap-2 pb-3">
        <div>
          <h4 className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.coverage.title')}
          </h4>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dalton.coverage.subtitle')}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refetch()}
          disabled={isLoading || isFetching}
          className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-background px-2.5 py-1 text-xs font-medium hover:bg-muted/60 disabled:opacity-50"
        >
          <RefreshCw className={cn('h-3.5 w-3.5', isFetching && 'animate-spin')} />
          {t('dalton.coverage.refresh')}
        </button>
      </div>

      {error && (
        <div className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error instanceof Error ? error.message : t('dalton.coverage.loadError')}
        </div>
      )}

      <div className="mb-2 flex gap-1" data-testid="coverage-tabs">
        {tabs.map(({ key, label, count }) => (
          <button
            key={key}
            type="button"
            data-testid={`coverage-tab-${key}`}
            onClick={() => {
              setTab(key);
              setExpandedUnit(null);
            }}
            className={cn(
              'rounded-md px-2.5 py-1 text-xs font-medium',
              tab === key
                ? 'bg-muted text-foreground'
                : 'text-muted-foreground hover:bg-muted/40',
            )}
          >
            {label} ({count})
          </button>
        ))}
      </div>

      <div className="rounded-md border border-border/50 bg-background/40 p-2">
        {tab === 'sectors' &&
          (sectors.length === 0 ? (
            <div className="py-2 text-xs text-muted-foreground">
              {t('dalton.coverage.empty')}
            </div>
          ) : (
            <div className="divide-y divide-border/40">
              {sectors.map((u) => (
                <UnitRow
                  key={u.unit_id}
                  unit={u}
                  expanded={expandedUnit === u.unit_id}
                  onToggle={() =>
                    setExpandedUnit((prev) => (prev === u.unit_id ? null : u.unit_id))
                  }
                />
              ))}
            </div>
          ))}

        {tab === 'companies' &&
          (standaloneCompanies.length === 0 ? (
            <div className="py-2 text-xs text-muted-foreground">
              {t('dalton.coverage.empty')}
            </div>
          ) : (
            <div className="divide-y divide-border/40">
              {standaloneCompanies.map((u) => (
                <UnitRow
                  key={u.unit_id}
                  unit={u}
                  expanded={expandedUnit === u.unit_id}
                  onToggle={() =>
                    setExpandedUnit((prev) => (prev === u.unit_id ? null : u.unit_id))
                  }
                />
              ))}
            </div>
          ))}

        {tab === 'unclassified' &&
          (unclassified.length === 0 ? (
            <div className="py-2 text-xs text-muted-foreground">
              {t('dalton.coverage.empty')}
            </div>
          ) : (
            <div className="divide-y divide-border/40">
              {unclassified.map((ws) => (
                <div
                  key={ws.workspace_id}
                  data-testid={`coverage-unclassified-${ws.workspace_id}`}
                  className="flex flex-wrap items-center gap-2 py-2"
                >
                  <span className="text-xs text-foreground">{ws.name}</span>
                  {ws.description && (
                    <span className="max-w-md truncate text-[11px] text-muted-foreground">
                      {ws.description}
                    </span>
                  )}
                  <OpenWorkspaceLink workspaceId={ws.workspace_id} />
                </div>
              ))}
            </div>
          ))}
      </div>
    </div>
  );
}
