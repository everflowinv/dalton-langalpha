import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowUpRight, ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import TimezonePicker from '@/components/TimezonePicker';
import { useUser } from '@/hooks/useUser';
import {
  archiveDaltonCoverageUnit,
  bindDaltonCoverageUnit,
  coverageUnitErrorCode,
  registerDaltonCoverageUnit,
  splitCoverageUnits,
  unbindDaltonCoverageUnit,
  useDaltonCoverageUnitDetail,
  useDaltonCoverageUnits,
  type CoverageUnitDetail,
  type CoverageUnitEntry,
  type SectorMember,
  type ThesisSummary,
  type UnclassifiedWorkspace,
} from '@/lib/dalton/coverage';

type CoverageCompanyPayload = NonNullable<CoverageUnitDetail['company']>;
type CoverageSectorPayload = NonNullable<CoverageUnitDetail['sector']>;
import { queryKeys } from '@/lib/queryKeys';
import {
  DaltonActionConfirmButton,
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
  DaltonToolbarPill,
} from '@/components/dalton/primitives';

type CoverageTab = 'sectors' | 'companies' | 'unclassified';

interface NoticeState {
  tone: 'success' | 'error' | 'info';
  message: string;
}

function OpenWorkspaceLink({
  workspaceId,
  label,
}: {
  workspaceId: string;
  label: string;
}) {
  const navigate = useNavigate();
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-7 gap-1 px-2 text-xs"
      style={{ color: 'var(--color-accent-primary)' }}
      onClick={() => navigate(`/chat/${workspaceId}`)}
      data-testid={`dalton-coverage-open-ws-${workspaceId}`}
    >
      <span>{label}</span>
      <ArrowUpRight className="h-3 w-3" />
    </Button>
  );
}

function UnitCapabilityBadges({
  readable,
  delegatable,
  autoTracked,
}: {
  readable?: boolean;
  delegatable?: boolean;
  autoTracked?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <>
      {readable && (
        <DaltonStatusBadge tone="neutral" label={t('dalton.coverage.readable')} />
      )}
      {delegatable && (
        <DaltonStatusBadge tone="accent" label={t('dalton.coverage.delegatable')} />
      )}
      {autoTracked && (
        <DaltonStatusBadge tone="success" label={t('dalton.coverage.autoTracked')} />
      )}
    </>
  );
}

interface ManageActionsProps {
  unitId: string;
  unitName: string;
  workspaceId: string | null | undefined;
  unclassified: UnclassifiedWorkspace[];
  selectedWs: string;
  onSelectWs: (workspaceId: string) => void;
  onBind: (unitId: string, workspaceId: string) => Promise<void>;
  onUnbind: (unitId: string) => Promise<void>;
  onArchive: (unitId: string) => Promise<void>;
  binding: boolean;
  unbinding: boolean;
  archiving: boolean;
}

function UnitManageActions({
  unitId,
  unitName,
  workspaceId,
  unclassified,
  selectedWs,
  onSelectWs,
  onBind,
  onUnbind,
  onArchive,
  binding,
  unbinding,
  archiving,
}: ManageActionsProps) {
  const { t } = useTranslation();
  const busy = binding || unbinding || archiving;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {!workspaceId ? (
        <>
          <Select
            value={selectedWs}
            onChange={(e) => onSelectWs(e.target.value)}
            aria-label={t('dalton.coverage.bindTargetNone')}
            disabled={busy || unclassified.length === 0}
            className="w-44 text-xs"
            data-testid={`dalton-coverage-bind-select-${unitId}`}
          >
            <option value="">{t('dalton.coverage.bindTargetNone')}</option>
            {unclassified.map((ws) => (
              <option key={ws.workspace_id} value={ws.workspace_id}>
                {ws.name || ws.workspace_id}
              </option>
            ))}
          </Select>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 text-xs"
            disabled={!selectedWs || busy}
            onClick={() => void onBind(unitId, selectedWs)}
            data-testid={`dalton-coverage-bind-btn-${unitId}`}
          >
            {binding ? t('dalton.coverage.binding') : t('dalton.coverage.bind')}
          </Button>
        </>
      ) : (
        <DaltonActionConfirmButton
          label={t('dalton.coverage.unbind')}
          confirmTitle={t('dalton.coverage.unbindTitle')}
          confirmMessage={t('dalton.coverage.unbindConfirm', { unit: unitName })}
          confirmLabel={t('dalton.coverage.unbind')}
          busy={unbinding}
          onConfirm={() => onUnbind(unitId)}
          testId={`dalton-coverage-unbind-${unitId}`}
        />
      )}

      <DaltonActionConfirmButton
        label={t('dalton.coverage.archiveUnit')}
        confirmTitle={t('dalton.coverage.archiveTitle')}
        confirmMessage={t('dalton.coverage.archiveConfirm', { unit: unitName })}
        confirmLabel={t('dalton.coverage.archiveUnit')}
        busy={archiving}
        onConfirm={() => onArchive(unitId)}
        testId={`dalton-coverage-archive-${unitId}`}
      />
    </div>
  );
}

function ThesisBlock({ thesis }: { thesis: ThesisSummary | null | undefined }) {
  const { t } = useTranslation();
  if (!thesis) {
    return (
      <div className="space-y-1">
        <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('dalton.coverage.thesis')}
        </div>
        <p style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.coverage.noDoc')}</p>
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2 font-medium" style={{ color: 'var(--color-text-primary)' }}>
        <span>{t('dalton.coverage.thesis')}</span>
        {thesis.stance && (
          <DaltonStatusBadge tone="accent" label={`${t('dalton.coverage.stance')}: ${thesis.stance}`} />
        )}
        {thesis.conviction && (
          <DaltonStatusBadge
            tone="neutral"
            label={`${t('dalton.coverage.conviction')}: ${thesis.conviction}`}
          />
        )}
        {thesis.pillars.length > 0 && (
          <DaltonStatusBadge
            tone="neutral"
            label={t('dalton.coverage.pillarCount', { count: thesis.pillars.length })}
          />
        )}
      </div>
      {thesis.one_liner && (
        <p style={{ color: 'var(--color-text-secondary)' }}>{thesis.one_liner}</p>
      )}
    </div>
  );
}

function CompanyDetailView({ company }: { company: CoverageCompanyPayload }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      <ThesisBlock thesis={company.docs?.thesis} />

      <div className="space-y-1">
        <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('dalton.coverage.relatedSectors')} ({company.sectors.length})
        </div>
        {company.sectors.length === 0 ? (
          <p style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.coverage.none')}</p>
        ) : (
          <div className="space-y-1">
            {company.sectors.map((s) => (
              <div
                key={s.unit_id}
                className="flex flex-wrap items-center justify-between gap-2 rounded border px-2.5 py-1.5"
                style={{ borderColor: 'var(--color-border-muted)' }}
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {s.display_name || s.slug || s.unit_id}
                  </span>
                  {s.scope && <DaltonStatusBadge tone="neutral" label={s.scope} />}
                  {s.readable && (
                    <DaltonStatusBadge tone="neutral" label={t('dalton.coverage.readable')} />
                  )}
                </div>
                {s.workspace_id && s.readable !== false && (
                  <OpenWorkspaceLink
                    workspaceId={s.workspace_id}
                    label={t('dalton.coverage.openWorkspace')}
                  />
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="space-y-1">
        <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('dalton.coverage.reReview')} ({company.re_review.length})
        </div>
        {company.re_review.length === 0 ? (
          <p style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.coverage.none')}</p>
        ) : (
          <ul className="space-y-1" style={{ color: 'var(--color-text-secondary)' }}>
            {company.re_review.map((r) => (
              <li key={r.id}>
                {r.question}
                {r.value && (
                  <span style={{ color: 'var(--color-text-tertiary)' }}> · {r.value}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

interface MemberManageHandlers {
  currentUserId?: string;
  unclassified: UnclassifiedWorkspace[];
  bindSelections: Record<string, string>;
  onSelectWs: (unitId: string, workspaceId: string) => void;
  onBind: (unitId: string, workspaceId: string) => Promise<void>;
  onUnbind: (unitId: string) => Promise<void>;
  onArchive: (unitId: string) => Promise<void>;
  bindingUnitId: string | null;
  unbindingUnitId: string | null;
  archivingUnitId: string | null;
}

function SectorMemberRow({
  member,
  manage,
}: {
  member: SectorMember;
  manage: MemberManageHandlers;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const companyUnitId = member.company_unit_id ?? null;
  const detailQuery = useDaltonCoverageUnitDetail(expanded && companyUnitId ? companyUnitId : null);
  const memberLabel = member.name || member.ticker || member.member_id || '-';

  return (
    <div
      className="rounded border px-2.5 py-2 space-y-2"
      style={{
        borderColor: 'var(--color-border-muted)',
        backgroundColor: 'var(--color-bg-elevated)',
      }}
      data-testid={companyUnitId ? `dalton-coverage-member-${companyUnitId}` : undefined}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          {companyUnitId && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              aria-expanded={expanded}
              aria-label={memberLabel}
              onClick={() => setExpanded((v) => !v)}
              data-testid={`dalton-coverage-expand-member-${companyUnitId}`}
            >
              {expanded ? (
                <ChevronDown className="h-3.5 w-3.5" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" />
              )}
            </Button>
          )}
          <span className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {memberLabel}
          </span>
          {member.ticker && <DaltonStatusBadge tone="accent" label={member.ticker} />}
          <DaltonStatusBadge
            tone={member.workspace_created ? 'success' : 'neutral'}
            label={
              member.workspace_created
                ? `${member.name || member.ticker} (${t('dalton.coverage.covered')})`
                : `${member.name || member.ticker} (${t('dalton.coverage.notCovered')})`
            }
          />
          {member.scope && <DaltonStatusBadge tone="neutral" label={member.scope} />}
        </div>

        {member.workspace_id && (
          <OpenWorkspaceLink
            workspaceId={member.workspace_id}
            label={t('dalton.coverage.openWorkspace')}
          />
        )}
      </div>

      {expanded && companyUnitId && (
        <div
          className="pt-2 border-t space-y-3"
          style={{ borderColor: 'var(--color-border-muted)' }}
          data-testid={`dalton-coverage-member-detail-${companyUnitId}`}
        >
          {detailQuery.isLoading ? (
            <DaltonLoadingRow label={t('dalton.coverage.detailLoading')} />
          ) : detailQuery.isError || !detailQuery.data ? (
            <DaltonInlineNotice tone="error">{t('dalton.coverage.detailError')}</DaltonInlineNotice>
          ) : (
            <>
              {(!manage.currentUserId ||
                detailQuery.data.owner_user_id === manage.currentUserId) && (
                <UnitManageActions
                  unitId={detailQuery.data.unit_id}
                  unitName={
                    detailQuery.data.display_name ||
                    detailQuery.data.ticker ||
                    detailQuery.data.slug ||
                    detailQuery.data.unit_id
                  }
                  workspaceId={detailQuery.data.workspace_id}
                  unclassified={manage.unclassified}
                  selectedWs={manage.bindSelections[detailQuery.data.unit_id] ?? ''}
                  onSelectWs={(wsId) => manage.onSelectWs(detailQuery.data!.unit_id, wsId)}
                  onBind={manage.onBind}
                  onUnbind={manage.onUnbind}
                  onArchive={manage.onArchive}
                  binding={manage.bindingUnitId === detailQuery.data.unit_id}
                  unbinding={manage.unbindingUnitId === detailQuery.data.unit_id}
                  archiving={manage.archivingUnitId === detailQuery.data.unit_id}
                />
              )}
              {detailQuery.data.company && (
                <CompanyDetailView company={detailQuery.data.company} />
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function SectorDetailView({
  sector,
  manage,
}: {
  sector: CoverageSectorPayload;
  manage: MemberManageHandlers;
}) {
  const { t } = useTranslation();
  const driversDoc = sector.docs?.drivers;
  const drivers = driversDoc?.drivers ?? [];
  const questions = sector.agenda_todos?.questions ?? [];
  const validations = sector.agenda_todos?.validations ?? [];
  const events = sector.agenda_todos?.events ?? [];
  const todoTotal = questions.length + validations.length + events.length;
  const shownPendingCount = sector.ledger_gaps.length + sector.evidence_gaps.length;

  return (
    <div className="space-y-3">
      <ThesisBlock thesis={sector.docs?.thesis} />

      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-2 font-medium" style={{ color: 'var(--color-text-primary)' }}>
          <span>
            {t('dalton.coverage.drivers')} ({drivers.length})
          </span>
          {driversDoc?.as_of && (
            <span className="text-[11px] font-normal" style={{ color: 'var(--color-text-tertiary)' }}>
              as of {driversDoc.as_of}
            </span>
          )}
        </div>
        {drivers.length === 0 ? (
          <p style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.coverage.noDoc')}</p>
        ) : (
          <div className="space-y-1.5">
            {drivers.map((drv, idx) => (
              <div key={drv.id ?? idx} className="space-y-1">
                <div className="font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {drv.name}
                  {drv.kind ? ` (${drv.kind})` : ''}
                </div>
                {drv.leading_indicators.length > 0 && (
                  <ul className="pl-3 space-y-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {drv.leading_indicators.map((ind, iIdx) => (
                      <li key={ind.id ?? iIdx} className="flex flex-wrap items-center gap-2">
                        <span>{ind.name}</span>
                        {ind.latest && ind.latest.value != null && (
                          <span className="tabular-nums font-medium" style={{ color: 'var(--color-text-primary)' }}>
                            {ind.latest.value}
                            {ind.latest.unit ? ` ${ind.latest.unit}` : ''}
                            {ind.latest.period ? ` (${ind.latest.period})` : ''}
                            {ind.latest.as_of ? ` · ${String(ind.latest.as_of).slice(0, 10)}` : ''}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="space-y-1">
        <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('dalton.coverage.agendaTodos')} ({todoTotal})
        </div>
        {todoTotal === 0 ? (
          <p style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.coverage.none')}</p>
        ) : (
          <ul className="space-y-1" style={{ color: 'var(--color-text-secondary)' }}>
            {questions.map((q) => (
              <li key={q.id} className="flex flex-wrap items-center gap-2">
                <span>{q.question}</span>
                {q.urgency && <DaltonStatusBadge tone="neutral" label={q.urgency} />}
                {q.waiting_pm && (
                  <DaltonStatusBadge tone="warning" label={t('dalton.coverage.waitingPm')} />
                )}
              </li>
            ))}
            {validations.map((v) => (
              <li key={v.id}>
                {v.what} ({v.due ?? '—'})
              </li>
            ))}
            {events.map((ev, eIdx) => (
              <li key={`${ev.title}-${eIdx}`}>
                {ev.title}
                {ev.date ? ` (${ev.date})` : ''}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-1.5">
        <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('dalton.coverage.members')} ({sector.members.length})
        </div>
        {sector.members.length === 0 ? (
          <p style={{ color: 'var(--color-text-tertiary)' }}>{t('dalton.coverage.none')}</p>
        ) : (
          <div className="space-y-1.5">
            {sector.members.map((member, idx) => (
              <SectorMemberRow
                key={member.member_id ?? `${member.ticker}-${idx}`}
                member={member}
                manage={manage}
              />
            ))}
          </div>
        )}
      </div>

      {sector.evidence_gaps.length > 0 && (
        <div className="space-y-1">
          <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.coverage.evidenceGaps')} ({sector.evidence_gaps.length})
          </div>
          <ul className="space-y-1" style={{ color: 'var(--color-text-secondary)' }}>
            {sector.evidence_gaps.map((g, idx) => (
              <li key={g.request_id ?? idx}>{g.question}</li>
            ))}
          </ul>
        </div>
      )}

      {sector.ledger_gaps.length > 0 && (
        <div className="space-y-1">
          <div className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.coverage.ledgerGaps')} ({sector.ledger_gaps.length})
          </div>
          <ul className="space-y-1" style={{ color: 'var(--color-text-secondary)' }}>
            {sector.ledger_gaps.map((g, idx) => (
              <li key={g.request_id ?? idx}>{g.question}</li>
            ))}
          </ul>
        </div>
      )}

      {sector.pending_total > shownPendingCount && (
        <p className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('dalton.coverage.pendingTruncated', {
            count: shownPendingCount,
            total: sector.pending_total,
          })}
        </p>
      )}
    </div>
  );
}

function CoverageUnitDetailSection({
  unitId,
  manage,
}: {
  unitId: string;
  manage: MemberManageHandlers;
}) {
  const { t } = useTranslation();
  const detailQuery = useDaltonCoverageUnitDetail(unitId);

  if (detailQuery.isLoading) {
    return <DaltonLoadingRow label={t('dalton.coverage.detailLoading')} />;
  }
  if (detailQuery.isError || !detailQuery.data) {
    return <DaltonInlineNotice tone="error">{t('dalton.coverage.detailError')}</DaltonInlineNotice>;
  }

  return (
    <div
      className="mt-2 rounded-md border p-3 text-xs"
      style={{
        borderColor: 'var(--color-border-muted)',
        backgroundColor: 'var(--color-bg-card)',
      }}
      data-testid={`dalton-coverage-detail-${unitId}`}
    >
      {detailQuery.data.sector && (
        <SectorDetailView sector={detailQuery.data.sector} manage={manage} />
      )}
      {detailQuery.data.company && <CompanyDetailView company={detailQuery.data.company} />}
    </div>
  );
}

/** Coverage unit registration & workspace binding card (dedicated Coverage Settings section). */
export function DaltonCoverageCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const location = useLocation();
  const { user } = useUser();
  const coverageQuery = useDaltonCoverageUnits(true);

  const [activeTab, setActiveTab] = useState<CoverageTab>(
    location.hash === '#dalton-coverage-unclassified' ? 'unclassified' : 'sectors',
  );
  const [expandedUnitId, setExpandedUnitId] = useState<string | null>(null);
  const [slugInput, setSlugInput] = useState('');
  const [displayNameInput, setDisplayNameInput] = useState('');
  const [tzInput, setTzInput] = useState('');
  const [bindSelections, setBindSelections] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<NoticeState | null>(null);

  useEffect(() => {
    if (location.hash === '#dalton-coverage-unclassified') {
      setActiveTab('unclassified');
    }
  }, [location.hash, location.key]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.coverageUnits() });
    void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.all });
  };

  const resolveError = (err: unknown) => {
    const code = coverageUnitErrorCode(err);
    if (code) {
      return t(`dalton.coverage.errors.${code}`, {
        defaultValue: t('dalton.coverage.errors.fallback'),
      });
    }
    return t('dalton.coverage.errors.fallback');
  };

  const registerMutation = useMutation({
    mutationFn: () =>
      registerDaltonCoverageUnit({
        kind: 'sector',
        slug: slugInput.trim(),
        display_name: displayNameInput.trim() || undefined,
        research_tz: tzInput.trim() || undefined,
      }),
    onSuccess: (created) => {
      setSlugInput('');
      setDisplayNameInput('');
      setTzInput('');
      setActiveTab('sectors');
      setNotice({
        tone: 'success',
        message: t('dalton.coverage.registeredSlug', { slug: created.slug ?? '' }),
      });
      invalidate();
    },
    onError: (err) => {
      setNotice({ tone: 'error', message: resolveError(err) });
    },
  });

  const bindMutation = useMutation({
    mutationFn: ({ unitId, workspaceId }: { unitId: string; workspaceId: string }) =>
      bindDaltonCoverageUnit(unitId, workspaceId),
    onSuccess: (unit) => {
      setNotice({
        tone: 'success',
        message:
          unit.kind === 'sector'
            ? t('dalton.coverage.boundHintSector')
            : t('dalton.common.saved'),
      });
      invalidate();
    },
    onError: (err) => {
      setNotice({ tone: 'error', message: resolveError(err) });
    },
  });

  const unbindMutation = useMutation({
    mutationFn: (unitId: string) => unbindDaltonCoverageUnit(unitId),
    onSuccess: () => {
      setNotice({ tone: 'success', message: t('dalton.common.saved') });
      invalidate();
    },
    onError: (err) => {
      setNotice({ tone: 'error', message: resolveError(err) });
    },
  });

  const archiveMutation = useMutation({
    mutationFn: (unitId: string) => archiveDaltonCoverageUnit(unitId),
    onSuccess: () => {
      setNotice({ tone: 'success', message: t('dalton.common.saved') });
      invalidate();
    },
    onError: (err) => {
      setNotice({ tone: 'error', message: resolveError(err) });
    },
  });

  if (coverageQuery.isLoading || !coverageQuery.data || coverageQuery.data.enabled === false) {
    return null;
  }

  const units = coverageQuery.data.units;
  const unclassified = coverageQuery.data.unclassified;
  const { sectors, standaloneCompanies } = splitCoverageUnits(units);

  const bindingUnitId = bindMutation.isPending ? (bindMutation.variables?.unitId ?? null) : null;
  const unbindingUnitId = unbindMutation.isPending ? (unbindMutation.variables ?? null) : null;
  const archivingUnitId = archiveMutation.isPending ? (archiveMutation.variables ?? null) : null;

  const manageHandlers: MemberManageHandlers = {
    currentUserId: user?.user_id,
    unclassified,
    bindSelections,
    onSelectWs: (unitId, wsId) =>
      setBindSelections((prev) => ({ ...prev, [unitId]: wsId })),
    onBind: async (unitId, workspaceId) => {
      await bindMutation.mutateAsync({ unitId, workspaceId });
    },
    onUnbind: async (unitId) => {
      await unbindMutation.mutateAsync(unitId);
    },
    onArchive: async (unitId) => {
      await archiveMutation.mutateAsync(unitId);
    },
    bindingUnitId,
    unbindingUnitId,
    archivingUnitId,
  };

  const renderUnitRow = (unit: CoverageUnitEntry) => {
    const unitName = unit.display_name || unit.slug || unit.ticker || unit.unit_id;
    const isOwner = !user?.user_id || unit.owner_user_id === user.user_id;
    const isExpanded = expandedUnitId === unit.unit_id;
    const canOpenWs = Boolean(unit.workspace_id && unit.readable !== false);

    return (
      <DaltonItemRow
        key={unit.unit_id}
        testId={`dalton-coverage-unit-${unit.unit_id}`}
        leading={
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0"
            aria-expanded={isExpanded}
            aria-label={unitName}
            onClick={() => setExpandedUnitId(isExpanded ? null : unit.unit_id)}
            data-testid={`dalton-coverage-expand-${unit.unit_id}`}
          >
            {isExpanded ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </Button>
        }
        title={
          <span className="flex flex-wrap items-center gap-2">
            <span>{unitName}</span>
            {unit.slug && <DaltonStatusBadge tone="neutral" label={unit.slug} />}
            {unit.ticker && <DaltonStatusBadge tone="accent" label={unit.ticker} />}
            <UnitCapabilityBadges
              readable={unit.readable}
              delegatable={unit.delegatable}
              autoTracked={unit.auto_tracked}
            />
            {!unit.workspace_id && (
              <DaltonStatusBadge tone="neutral" label={t('dalton.coverage.noWorkspace')} />
            )}
          </span>
        }
        trailing={
          <div className="flex flex-wrap items-center gap-2">
            {canOpenWs && unit.workspace_id && (
              <OpenWorkspaceLink
                workspaceId={unit.workspace_id}
                label={t('dalton.coverage.openWorkspace')}
              />
            )}
            {isOwner && (
              <UnitManageActions
                unitId={unit.unit_id}
                unitName={unitName}
                workspaceId={unit.workspace_id}
                unclassified={unclassified}
                selectedWs={bindSelections[unit.unit_id] ?? ''}
                onSelectWs={(wsId) => manageHandlers.onSelectWs(unit.unit_id, wsId)}
                onBind={manageHandlers.onBind}
                onUnbind={manageHandlers.onUnbind}
                onArchive={manageHandlers.onArchive}
                binding={bindingUnitId === unit.unit_id}
                unbinding={unbindingUnitId === unit.unit_id}
                archiving={archivingUnitId === unit.unit_id}
              />
            )}
          </div>
        }
      >
        <Disclosure open={isExpanded}>
          {isExpanded && (
            <CoverageUnitDetailSection unitId={unit.unit_id} manage={manageHandlers} />
          )}
        </Disclosure>
      </DaltonItemRow>
    );
  };

  return (
    <DaltonSettingsCard
      id="dalton-coverage"
      testId="dalton-coverage-card"
      title={t('dalton.coverage.title')}
      description={t('dalton.coverage.description')}
      layout="padded"
      bodyClassName="space-y-5"
      action={
        <DaltonRefreshButton
          busy={coverageQuery.isFetching}
          onClick={() => void coverageQuery.refetch()}
          testId="dalton-coverage-refresh"
        />
      }
    >
      <div
        className="rounded-md border p-3.5 space-y-2.5"
        style={{
          borderColor: 'var(--color-border-muted)',
          backgroundColor: 'var(--color-bg-elevated)',
        }}
      >
        <div>
          <h4 className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.coverage.registerTitle')}
          </h4>
          <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dalton.coverage.registerHint')}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={slugInput}
            onChange={(e) => setSlugInput(e.target.value)}
            placeholder={t('dalton.coverage.fieldSlug')}
            data-testid="dalton-coverage-slug-input"
            className="h-8 w-52 text-xs"
          />
          <Input
            value={displayNameInput}
            onChange={(e) => setDisplayNameInput(e.target.value)}
            placeholder={t('dalton.coverage.fieldDisplayName')}
            data-testid="dalton-coverage-name-input"
            className="h-8 w-44 text-xs"
          />
          <TimezonePicker
            value={tzInput}
            onChange={setTzInput}
            placeholder={t('dalton.coverage.fieldResearchTz')}
            className="w-48"
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 text-xs"
            disabled={!slugInput.trim() || registerMutation.isPending}
            onClick={() => registerMutation.mutate()}
            data-testid="dalton-coverage-register-btn"
          >
            {registerMutation.isPending
              ? t('dalton.coverage.registering')
              : t('dalton.coverage.register')}
          </Button>
        </div>
      </div>

      {notice && <DaltonInlineNotice tone={notice.tone}>{notice.message}</DaltonInlineNotice>}

      <div className="flex flex-wrap items-center gap-2" role="tablist">
        <DaltonToolbarPill
          label={t('dalton.coverage.tabSectors')}
          count={sectors.length}
          active={activeTab === 'sectors'}
          onClick={() => setActiveTab('sectors')}
          testId="dalton-coverage-tab-sectors"
        />
        <DaltonToolbarPill
          label={t('dalton.coverage.tabCompanies')}
          count={standaloneCompanies.length}
          active={activeTab === 'companies'}
          onClick={() => setActiveTab('companies')}
          testId="dalton-coverage-tab-companies"
        />
        <DaltonToolbarPill
          id="dalton-coverage-unclassified"
          label={t('dalton.coverage.tabUnclassified')}
          count={unclassified.length}
          active={activeTab === 'unclassified'}
          onClick={() => setActiveTab('unclassified')}
          testId="dalton-coverage-tab-unclassified"
        />
      </div>

      {activeTab === 'sectors' && (
        <DaltonKeyValueList isEmpty={sectors.length === 0} empty={t('dalton.coverage.empty')}>
          {sectors.map(renderUnitRow)}
        </DaltonKeyValueList>
      )}

      {activeTab === 'companies' && (
        <DaltonKeyValueList
          isEmpty={standaloneCompanies.length === 0}
          empty={t('dalton.coverage.empty')}
        >
          {standaloneCompanies.map(renderUnitRow)}
        </DaltonKeyValueList>
      )}

      {activeTab === 'unclassified' && (
        <DaltonKeyValueList
          isEmpty={unclassified.length === 0}
          empty={t('dalton.coverage.empty')}
        >
          {unclassified.map((ws) => (
            <DaltonItemRow
              key={ws.workspace_id}
              title={ws.name || ws.workspace_id}
              subtitle={ws.description ?? undefined}
              trailing={
                <div className="flex items-center gap-2">
                  <DaltonStatusBadge
                    tone="neutral"
                    label={t('dalton.coverage.unclassifiedLabel')}
                  />
                  <OpenWorkspaceLink
                    workspaceId={ws.workspace_id}
                    label={t('dalton.coverage.openWorkspace')}
                  />
                </div>
              }
            />
          ))}
        </DaltonKeyValueList>
      )}
    </DaltonSettingsCard>
  );
}
