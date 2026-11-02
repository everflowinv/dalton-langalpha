import React, { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, ExternalLink, Loader2, Plus, RefreshCw } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import {
  archiveDaltonCoverageUnit,
  bindDaltonCoverageUnit,
  coverageUnitErrorCode,
  type CoverageUnitDetail,
  type CoverageUnitEntry,
  type RegisterCoverageUnitPayload,
  type SectorMember,
  type UnclassifiedWorkspace,
  registerDaltonCoverageUnit,
  splitCoverageUnits,
  unbindDaltonCoverageUnit,
  useDaltonCoverageUnitDetail,
  useDaltonCoverageUnits,
} from '@/lib/dalton/coverage';
import { queryKeys } from '@/lib/queryKeys';
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

type TFunc = ReturnType<typeof useTranslation>['t'];

/** 写路径错误文案：有稳定错误码走 i18n，否则退回原始 message。 */
function coverageMutationError(err: unknown, t: TFunc): string {
  const code = coverageUnitErrorCode(err);
  if (code) {
    const keyed = t(`dalton.coverage.errors.${code}`, { defaultValue: '' });
    if (keyed) return keyed;
  }
  return err instanceof Error && err.message
    ? err.message
    : t('dalton.coverage.errors.fallback');
}

/** 新建行业单元（P5）：只登记 sector；绑定在单元行上单独做。 */
function RegisterSectorForm() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [slug, setSlug] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [researchTz, setResearchTz] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [createdSlug, setCreatedSlug] = useState<string | null>(null);

  const registerMutation = useMutation({
    mutationFn: (payload: RegisterCoverageUnitPayload) => registerDaltonCoverageUnit(payload),
    onSuccess: (unit) => {
      setError(null);
      setCreatedSlug(unit.slug);
      setSlug('');
      setDisplayName('');
      setResearchTz('');
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.coverageUnits() });
    },
    onError: (err: unknown) => {
      setCreatedSlug(null);
      setError(coverageMutationError(err, t));
    },
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const cleanSlug = slug.trim();
    if (!cleanSlug) {
      setError(t('dalton.coverage.errors.invalid_slug'));
      return;
    }
    registerMutation.mutate({
      kind: 'sector',
      slug: cleanSlug,
      display_name: displayName.trim() || undefined,
      research_tz: researchTz.trim() || undefined,
    });
  };

  return (
    <form
      onSubmit={submit}
      data-testid="coverage-register-form"
      className="mb-3 rounded-md border border-border/50 bg-background/40 p-2"
    >
      <div className="flex flex-wrap items-center gap-2 pb-2">
        <span className="inline-flex items-center gap-1 text-xs font-medium text-foreground">
          <Plus className="h-3.5 w-3.5 text-muted-foreground" />
          {t('dalton.coverage.registerTitle')}
        </span>
        <span className="text-[11px] text-muted-foreground">
          {t('dalton.coverage.registerHint')}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          placeholder={t('dalton.coverage.fieldSlug')}
          aria-label={t('dalton.coverage.fieldSlug')}
          data-testid="coverage-register-slug"
          className="w-56 rounded border border-border/60 bg-background px-2 py-1 text-xs"
        />
        <input
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder={t('dalton.coverage.fieldDisplayName')}
          aria-label={t('dalton.coverage.fieldDisplayName')}
          data-testid="coverage-register-display-name"
          className="w-44 rounded border border-border/60 bg-background px-2 py-1 text-xs"
        />
        <input
          value={researchTz}
          onChange={(e) => setResearchTz(e.target.value)}
          placeholder={t('dalton.coverage.fieldResearchTz')}
          aria-label={t('dalton.coverage.fieldResearchTz')}
          data-testid="coverage-register-tz"
          className="w-56 rounded border border-border/60 bg-background px-2 py-1 text-xs"
        />
        <button
          type="submit"
          disabled={registerMutation.isPending}
          data-testid="coverage-register-submit"
          className="rounded-md border border-border/60 bg-background px-2.5 py-1 text-xs font-medium hover:bg-muted/60 disabled:opacity-50"
        >
          {registerMutation.isPending
            ? t('dalton.coverage.registering')
            : t('dalton.coverage.register')}
        </button>
      </div>
      {createdSlug && (
        <div
          data-testid="coverage-register-done"
          className="pt-1 text-[11px] text-emerald-600 dark:text-emerald-400"
        >
          {t('dalton.coverage.registeredSlug', { slug: createdSlug })}
        </div>
      )}
      {error && (
        <div
          role="alert"
          data-testid="coverage-register-error"
          className="pt-1 text-[11px] text-destructive"
        >
          {error}
        </div>
      )}
    </form>
  );
}

/** UnitManageActions 只消费这两个字段；CoverageUnitEntry 与 CoverageUnitDetail 都结构满足。 */
interface ManageableUnit {
  unit_id: string;
  workspace_id: string | null;
}

/** 本人单元行上的管理操作（P5）：绑定（选本人的未绑定工作区）/ 解绑 / 归档。 */
function UnitManageActions({
  unit,
  unclassified,
}: {
  unit: ManageableUnit;
  unclassified: UnclassifiedWorkspace[];
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [wsChoice, setWsChoice] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.coverageUnits() });
    void queryClient.invalidateQueries({
      queryKey: queryKeys.dalton.coverageUnitDetail(unit.unit_id),
    });
  };

  const bindMutation = useMutation({
    mutationFn: (wsId: string) => bindDaltonCoverageUnit(unit.unit_id, wsId),
    onSuccess: (row) => {
      setError(null);
      setWsChoice('');
      setHint(row.kind === 'sector' ? t('dalton.coverage.boundHintSector') : null);
      invalidate();
    },
    onError: (err: unknown) => setError(coverageMutationError(err, t)),
  });
  const unbindMutation = useMutation({
    mutationFn: () => unbindDaltonCoverageUnit(unit.unit_id),
    onSuccess: () => {
      setError(null);
      setHint(null);
      invalidate();
    },
    onError: (err: unknown) => setError(coverageMutationError(err, t)),
  });
  const archiveMutation = useMutation({
    mutationFn: () => archiveDaltonCoverageUnit(unit.unit_id),
    onSuccess: () => {
      setError(null);
      setHint(null);
      invalidate();
    },
    onError: (err: unknown) => setError(coverageMutationError(err, t)),
  });

  const busy =
    bindMutation.isPending || unbindMutation.isPending || archiveMutation.isPending;
  const actionBtn =
    'rounded border border-border/60 bg-background px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50';

  return (
    <div
      className="flex w-full flex-col gap-1 pt-1"
      data-testid={`coverage-manage-${unit.unit_id}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        {!unit.workspace_id && (
          <>
            <select
              value={wsChoice}
              onChange={(e) => setWsChoice(e.target.value)}
              data-testid={`coverage-bind-select-${unit.unit_id}`}
              aria-label={t('dalton.coverage.bindTargetNone')}
              disabled={busy || unclassified.length === 0}
              className="max-w-56 rounded border border-border/60 bg-background px-1.5 py-0.5 text-[11px]"
            >
              <option value="">{t('dalton.coverage.bindTargetNone')}</option>
              {unclassified.map((ws) => (
                <option key={ws.workspace_id} value={ws.workspace_id}>
                  {ws.name ?? ws.workspace_id}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => {
                if (wsChoice) bindMutation.mutate(wsChoice);
              }}
              disabled={busy || !wsChoice}
              data-testid={`coverage-bind-btn-${unit.unit_id}`}
              className={actionBtn}
            >
              {bindMutation.isPending
                ? t('dalton.coverage.binding')
                : t('dalton.coverage.bind')}
            </button>
          </>
        )}
        {unit.workspace_id && (
          <button
            type="button"
            onClick={() => unbindMutation.mutate()}
            disabled={busy}
            data-testid={`coverage-unbind-btn-${unit.unit_id}`}
            className={actionBtn}
          >
            {t('dalton.coverage.unbind')}
          </button>
        )}
        <button
          type="button"
          onClick={() => archiveMutation.mutate()}
          disabled={busy}
          data-testid={`coverage-archive-btn-${unit.unit_id}`}
          className={actionBtn}
        >
          {t('dalton.coverage.archiveUnit')}
        </button>
      </div>
      {hint && (
        <div
          data-testid={`coverage-bind-hint-${unit.unit_id}`}
          className="text-[11px] text-muted-foreground"
        >
          {hint}
        </div>
      )}
      {error && (
        <div
          role="alert"
          data-testid={`coverage-manage-error-${unit.unit_id}`}
          className="text-[11px] text-destructive"
        >
          {error}
        </div>
      )}
    </div>
  );
}

/** 行业成员行：登记了公司单元的成员可展开该公司详情（关联行业 + 待复核）。 */
function MemberRow({
  member,
  expanded,
  onToggle,
  unclassified,
}: {
  member: SectorMember;
  expanded: boolean;
  onToggle: () => void;
  unclassified: UnclassifiedWorkspace[];
}) {
  const { t } = useTranslation();
  const { userId } = useAuth();
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
            <>
              <CompanyDetail detail={detailQuery.data} />
              {/* 成员公司同样有自己的管理入口（codex r2 #1）：归属判断用
                  详情响应的 owner_user_id，与 UnitRow 同一规则；成员被
                  "独立公司"入口排除，这里是本人公司单元唯一的行内入口。 */}
              {detailQuery.data.owner_user_id === userId && (
                <UnitManageActions unit={detailQuery.data} unclassified={unclassified} />
              )}
            </>
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

function SectorDetail({
  detail,
  unclassified,
}: {
  detail: CoverageUnitDetail;
  unclassified: UnclassifiedWorkspace[];
}) {
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
              unclassified={unclassified}
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

function UnitRow({
  unit,
  expanded,
  onToggle,
  own,
  unclassified,
}: {
  unit: CoverageUnitEntry;
  expanded: boolean;
  onToggle: () => void;
  own: boolean;
  unclassified: UnclassifiedWorkspace[];
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
              <SectorDetail detail={detailQuery.data} unclassified={unclassified} />
            ) : (
              <CompanyDetail detail={detailQuery.data} />
            )
          ) : null}
          {/* 登记 / 绑定 / 解绑 / 归档只出现在本人的单元行上（P5） */}
          {own && <UnitManageActions unit={unit} unclassified={unclassified} />}
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
  const { userId } = useAuth();
  // 侧栏"未分类"入口带着 #dalton-coverage-unclassified 跳来：挂载时读到
  // 该 hash 就直接落在"未分类工作区" tab（Settings 的 hash 机制会滚动到
  // id 同名的 tab 按钮上）。
  const [tab, setTab] = useState<CoverageTab>(() =>
    typeof window !== 'undefined' && window.location.hash === '#dalton-coverage-unclassified'
      ? 'unclassified'
      : 'sectors',
  );
  const [expandedUnit, setExpandedUnit] = useState<string | null>(null);
  const { data, isLoading, isFetching, refetch, error } = useDaltonCoverageUnits();
  // Already on this page when the sidebar link is followed again: the card
  // stays mounted, so the initializer above never re-reads the hash. Each
  // navigation gets a new location.key, which re-runs this.
  const location = useLocation();
  useEffect(() => {
    if (location.hash === '#dalton-coverage-unclassified') setTab('unclassified');
  }, [location.hash, location.key]);

  // 开关关闭（或还在加载/失败）时不渲染任何行业相关入口与文案
  if (!data || data.enabled === false) return null;

  // 行业 / 独立公司两分（独立公司 = 没被任何可见行业列为成员的公司单元）
  // 与侧栏覆盖结构树、工作区从属标记共用同一推导（lib/dalton/coverage）。
  const { sectors, standaloneCompanies } = splitCoverageUnits(data.units);
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
      id="dalton-coverage"
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

      {/* 新建行业单元：绑定目标 = 未分类工作区（本人的、未绑 active 单元、非 Home） */}
      <RegisterSectorForm />

      <div className="mb-2 flex gap-1" data-testid="coverage-tabs">
        {tabs.map(({ key, label, count }) => (
          <button
            key={key}
            type="button"
            data-testid={`coverage-tab-${key}`}
            // Settings 的 hash 滚动机制以元素 id 为锚：#dalton-coverage-unclassified 落在未分类 tab 上
            id={key === 'unclassified' ? 'dalton-coverage-unclassified' : undefined}
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
                  own={u.owner_user_id === userId}
                  unclassified={unclassified}
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
                  own={u.owner_user_id === userId}
                  unclassified={unclassified}
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
