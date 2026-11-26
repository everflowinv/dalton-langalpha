import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useInRouterContext } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ArrowRight } from 'lucide-react';
import { isNotFoundError } from '@/api/errors';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ToggleSwitch } from '@/components/ui/switch';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import { SettingRow } from '@/pages/Settings/components/SettingsLayout';
import {
  DALTON_WEIXIN_ADMIN_UNBIND_REASONS,
  adminRevokeDaltonWeixinUser,
  adminUnbindDaltonWeixinAccount,
  fetchDaltonWeixinAdminOverview,
  updateDaltonWeixinAdminConfig,
  type DaltonWeixinAdminMemberItem,
  type DaltonWeixinAdminUnbindReason,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueGrid,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
  type DaltonStatusTone,
} from '@/components/dalton/primitives';

const REASON_LABEL_KEYS: Record<DaltonWeixinAdminUnbindReason, string> = {
  离职交接: 'dalton.weixinBot.reasonOffboarding',
  手机遗失应急: 'dalton.weixinBot.reasonLostPhone',
  误绑微信号: 'dalton.weixinBot.reasonWrongWechat',
  其他原因: 'dalton.weixinBot.reasonOther',
};

const PENDING_STAGE_LABEL_KEYS: Record<string, string> = {
  starting: 'dalton.weixinBot.stageStarting',
  wait: 'dalton.weixinBot.stageWait',
  scaned: 'dalton.weixinBot.stageScaned',
  scaned_but_redirect: 'dalton.weixinBot.stageScaned',
  verify_code: 'dalton.weixinBot.stageVerifyCode',
};

function memberStatusBadge(
  member: DaltonWeixinAdminMemberItem,
  t: (key: string) => string,
): { tone: DaltonStatusTone; label: string } {
  if (member.status === 'active') {
    return { tone: 'success', label: t('dalton.weixinBot.memberStatusActive') };
  }
  if (member.status === 'pending_verification') {
    return { tone: 'accent', label: t('dalton.weixinBot.memberStatusPendingVerification') };
  }
  if (member.status === 'login_pending') {
    return { tone: 'accent', label: t('dalton.weixinBot.memberStatusLoginPending') };
  }
  if (member.status === 'expired') {
    return { tone: 'warning', label: t('dalton.weixinBot.memberStatusExpired') };
  }
  if (member.status === 'disabled') {
    if (member.disabled_reason === 'external_access_disabled') {
      return { tone: 'warning', label: t('dalton.weixinBot.memberStatusExternalPaused') };
    }
    if (member.disabled_reason === 'member_not_in_roster') {
      return { tone: 'neutral', label: t('dalton.weixinBot.memberStatusRemoved') };
    }
    return { tone: 'neutral', label: t('dalton.weixinBot.memberStatusDisabled') };
  }
  return { tone: 'neutral', label: member.status };
}

function tierLabel(tier: string, t: (key: string) => string): string {
  if (tier === 'admin') return t('dalton.weixinBot.tierAdmin');
  if (tier === 'internal') return t('dalton.weixinBot.tierInternal');
  if (tier === 'external') return t('dalton.weixinBot.tierExternal');
  return tier;
}

function pendingStageLabel(stage: string | null | undefined, t: (key: string) => string): string {
  const key = stage ? PENDING_STAGE_LABEL_KEYS[stage] : undefined;
  return key ? t(key) : t('dalton.weixinBot.stageWait');
}

function ManageOwnWeixinLink({ label }: { label: string }) {
  const inRouter = useInRouterContext();
  const className =
    'inline-flex items-center gap-1 text-xs font-medium hover:underline transition-opacity tap-target';
  const style = { color: 'var(--color-accent-primary)' };

  if (inRouter) {
    return (
      <Link
        to="?tab=integrations#dalton-weixin-bind"
        data-testid="dalton-weixin-manage-own-link"
        className={className}
        style={style}
      >
        <span>{label}</span>
        <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
      </Link>
    );
  }
  return (
    <a
      href="/settings?tab=integrations#dalton-weixin-bind"
      data-testid="dalton-weixin-manage-own-link"
      className={className}
      style={style}
    >
      <span>{label}</span>
      <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
    </a>
  );
}

/** Admin-only team WeChat channel management card ("团队微信渠道管理"). */
export function DaltonWeixinBotCard() {
  const { t } = useTranslation();
  const locale = useLocale();
  const queryClient = useQueryClient();
  const { isAdmin } = useCallerIdentity();
  const { hasCapability } = useCapabilities();
  const canWeixin = hasCapability('channel_weixin');

  const [actionNotice, setActionNotice] = useState<{
    tone: 'success' | 'error';
    message: string;
  } | null>(null);
  const [unbindTarget, setUnbindTarget] = useState<DaltonWeixinAdminMemberItem | null>(null);
  const [selectedReason, setSelectedReason] = useState<DaltonWeixinAdminUnbindReason | ''>('');
  const [dialogError, setDialogError] = useState<string | null>(null);

  const overviewQuery = useQuery({
    queryKey: queryKeys.dalton.weixinAdminOverview(),
    queryFn: fetchDaltonWeixinAdminOverview,
    enabled: isAdmin && canWeixin,
    retry: false,
  });

  const configMutation = useMutation({
    mutationFn: updateDaltonWeixinAdminConfig,
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.weixinAdminOverview() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      if (res.disabled_count > 0) {
        setActionNotice({
          tone: 'success',
          message: t('dalton.weixinBot.configSavedPaused', { count: res.disabled_count }),
        });
      } else if (res.restored_count > 0) {
        setActionNotice({
          tone: 'success',
          message: t('dalton.weixinBot.configSavedRestored', { count: res.restored_count }),
        });
      } else {
        setActionNotice({
          tone: 'success',
          message: t('dalton.weixinBot.configSaved'),
        });
      }
    },
    onError: () => {
      setActionNotice({
        tone: 'error',
        message: t('dalton.weixinBot.configSaveFailed'),
      });
    },
  });

  const unbindMutation = useMutation({
    mutationFn: async (params: {
      member: DaltonWeixinAdminMemberItem;
      reason: DaltonWeixinAdminUnbindReason;
    }) => {
      if (params.member.account_id) {
        return adminUnbindDaltonWeixinAccount(params.member.account_id, params.reason);
      }
      return adminRevokeDaltonWeixinUser(params.member.user_id, params.reason);
    },
    onSuccess: (_res, vars) => {
      setUnbindTarget(null);
      setSelectedReason('');
      setDialogError(null);
      setActionNotice({
        tone: 'success',
        message: vars.member.account_id
          ? t('dalton.weixinBot.unbindSuccess', {
              member: vars.member.user_name || vars.member.user_email || vars.member.user_id,
            })
          : t('dalton.weixinBot.revokeSuccess', {
              member: vars.member.user_name || vars.member.user_email || vars.member.user_id,
            }),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.weixinAdminOverview() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
    },
    onError: () => {
      setDialogError(t('dalton.weixinBot.unbindFailed'));
    },
  });

  // Admin-only card: return null for non-admin callers or when capability is disabled.
  if (!isAdmin || !canWeixin || isNotFoundError(overviewQuery.error)) {
    return null;
  }

  const overview = overviewQuery.data;
  const totalConnected = overview
    ? overview.connected_count + overview.pending_verification_count
    : 0;
  const openUnbindDialog = (member: DaltonWeixinAdminMemberItem) => {
    setUnbindTarget(member);
    setSelectedReason('');
    setDialogError(null);
  };

  const handleConfirmUnbind = () => {
    if (!unbindTarget || !selectedReason || unbindMutation.isPending) return;
    setDialogError(null);
    unbindMutation.mutate({ member: unbindTarget, reason: selectedReason });
  };

  return (
    <DaltonSettingsCard
      id="dalton-weixin-bot"
      testId="dalton-weixin-bot-card"
      title={t('dalton.weixinBot.title')}
      description={t('dalton.weixinBot.description')}
      layout="padded"
      bodyClassName="space-y-4"
      action={
        <div className="flex items-center gap-2">
          {overview && (
            <DaltonStatusBadge
              dot
              testId="dalton-weixin-admin-status-badge"
              tone={overview.enabled ? 'success' : 'warning'}
              label={
                overview.enabled
                  ? t('dalton.weixinBot.channelOnBadge')
                  : t('dalton.weixinBot.channelPausedBadge')
              }
            />
          )}
          <DaltonRefreshButton
            busy={overviewQuery.isFetching}
            onClick={() => void overviewQuery.refetch()}
          />
        </div>
      }
    >
      {overviewQuery.isLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : overviewQuery.isError || !overview ? (
        <DaltonInlineNotice tone="error">{t('dalton.weixinBot.loadFailed')}</DaltonInlineNotice>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <ManageOwnWeixinLink label={t('dalton.weixinBot.manageOwnLink')} />
            {overview.gateway_lease && (
              <DaltonStatusBadge
                dot
                testId="dalton-weixin-gateway-lease-badge"
                tone={overview.gateway_lease.active ? 'success' : 'warning'}
                label={
                  overview.gateway_lease.active
                    ? t('dalton.weixinBot.gatewayLeaseActive')
                    : t('dalton.weixinBot.gatewayLeaseInactive')
                }
              />
            )}
          </div>

          <DaltonKeyValueGrid
            columns={3}
            items={[
              {
                label: t('dalton.weixinBot.statConnected'),
                value: `${overview.connected_count} / ${totalConnected}`,
                hint: overview.enabled
                  ? t('dalton.weixinBot.statConnectedHintActive')
                  : t('dalton.weixinBot.statConnectedHintPaused'),
                testId: 'dalton-weixin-stat-connected',
              },
              {
                label: t('dalton.weixinBot.statPendingExpired'),
                value: `${overview.pending_verification_count} / ${overview.expired_count}`,
                hint: t('dalton.weixinBot.statAttentionHint'),
                testId: 'dalton-weixin-stat-attention',
              },
              {
                label: t('dalton.weixinBot.statSlots'),
                value: `${overview.active_poller_count} / ${overview.max_concurrent_accounts}`,
                hint: overview.enabled
                  ? t('dalton.weixinBot.statSlotsHint')
                  : t('dalton.weixinBot.statSlotsPausedHint'),
                testId: 'dalton-weixin-stat-slots',
              },
            ]}
          />

          <div
            className="rounded-md border divide-y"
            style={{
              borderColor: 'var(--color-border-muted)',
              backgroundColor: 'var(--color-bg-elevated)',
            }}
          >
            <SettingRow
              id="dalton-weixin-admin-enabled"
              label={t('dalton.weixinBot.channelSwitchLabel')}
              description={t('dalton.weixinBot.channelSwitchDesc')}
            >
              <ToggleSwitch
                id="dalton-weixin-admin-enabled-switch"
                ariaLabel={t('dalton.weixinBot.channelSwitchLabel')}
                checked={overview.enabled}
                disabled={configMutation.isPending}
                onChange={() => configMutation.mutate({ enabled: !overview.enabled })}
              />
            </SettingRow>

            <SettingRow
              id="dalton-weixin-admin-allow-external"
              label={t('dalton.weixinBot.externalSwitchLabel')}
              description={t('dalton.weixinBot.externalSwitchDesc')}
            >
              <ToggleSwitch
                id="dalton-weixin-admin-allow-external-switch"
                ariaLabel={t('dalton.weixinBot.externalSwitchLabel')}
                checked={overview.allow_external}
                disabled={configMutation.isPending}
                onChange={() =>
                  configMutation.mutate({ allow_external: !overview.allow_external })
                }
              />
            </SettingRow>
          </div>

          {actionNotice && (
            <DaltonInlineNotice
              tone={actionNotice.tone}
              testId="dalton-weixin-admin-notice"
            >
              {actionNotice.message}
            </DaltonInlineNotice>
          )}

          <div className="space-y-2">
            <h4
              className="text-xs font-medium"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {t('dalton.weixinBot.membersHeading', { count: overview.accounts.length })}
            </h4>

            <DaltonKeyValueList
              isEmpty={overview.accounts.length === 0}
              empty={t('dalton.weixinBot.emptyMembers')}
            >
              {overview.accounts.map((member) => {
                const statusBadge = memberStatusBadge(member, t);
                const displayName =
                  member.user_name || member.user_email || member.user_id;
                const showSecondaryEmail = Boolean(
                  member.user_name && member.user_email && member.user_email !== member.user_name,
                );
                const isPendingOnly = !member.account_id;

                return (
                  <DaltonItemRow
                    key={member.account_id ?? `pending-${member.user_id}`}
                    testId={`dalton-weixin-member-row-${member.user_id}`}
                    title={
                      <>
                        <span className="font-medium break-words">{displayName}</span>
                        {showSecondaryEmail && (
                          <span
                            className="text-xs font-normal break-all"
                            style={{ color: 'var(--color-text-tertiary)' }}
                          >
                            {member.user_email}
                          </span>
                        )}
                        <DaltonStatusBadge
                          tone="neutral"
                          label={tierLabel(member.user_tier, t)}
                        />
                        <DaltonStatusBadge
                          dot
                          testId={`dalton-weixin-member-status-${member.user_id}`}
                          tone={statusBadge.tone}
                          label={statusBadge.label}
                        />
                        {member.account_id && member.has_pending_login && (
                          <DaltonStatusBadge
                            tone="accent"
                            label={t('dalton.weixinBot.rescanningBadge')}
                          />
                        )}
                      </>
                    }
                    subtitle={
                      isPendingOnly ? (
                        <span>
                          {t('dalton.weixinBot.pendingLoginDetail', {
                            stage: pendingStageLabel(member.pending_login_status, t),
                          })}
                        </span>
                      ) : (
                        <span className="inline-flex flex-wrap items-center gap-2">
                          <span>
                            {member.ilink_username || t('dalton.weixinBind.unnamedWechat')}
                          </span>
                          {member.masked_ilink_user_id && (
                            <code
                              className="text-[11px] font-mono px-1.5 py-0.5 rounded"
                              style={{
                                color: 'var(--color-text-secondary)',
                                backgroundColor: 'var(--color-bg-elevated)',
                              }}
                            >
                              {member.masked_ilink_user_id}
                            </code>
                          )}
                        </span>
                      )
                    }
                    meta={
                      <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-0.5">
                        {member.last_inbound_at && (
                          <span>
                            {t('dalton.weixinBot.lastInboundMeta', {
                              time: formatTimestamp(member.last_inbound_at, locale),
                            })}
                          </span>
                        )}
                        {member.updated_at && (
                          <span>
                            {t('dalton.weixinBot.updatedMeta', {
                              time: formatTimestamp(member.updated_at, locale),
                            })}
                          </span>
                        )}
                      </span>
                    }
                    trailing={
                      <Button
                        type="button"
                        variant="outline"
                        tone="danger"
                        size="sm"
                        className="h-8 text-xs tap-target"
                        disabled={unbindMutation.isPending}
                        onClick={() => openUnbindDialog(member)}
                        data-testid={`dalton-weixin-admin-unbind-${member.user_id}`}
                      >
                        {isPendingOnly
                          ? t('dalton.weixinBot.revokePendingAction')
                          : t('dalton.weixinBot.unbindAction')}
                      </Button>
                    }
                  />
                );
              })}
            </DaltonKeyValueList>
          </div>

          {overview.audit_logs.length > 0 && (
            <details
              data-testid="dalton-weixin-audit-logs"
              className="rounded-md border px-3 py-2 text-xs"
              style={{
                borderColor: 'var(--color-border-muted)',
                backgroundColor: 'var(--color-bg-elevated)',
              }}
            >
              <summary
                className="cursor-pointer font-medium select-none"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                {t('dalton.weixinBot.auditLogsSummary', {
                  count: overview.audit_logs.length,
                })}
              </summary>
              <div className="mt-2 space-y-1.5 max-h-48 overflow-y-auto">
                {overview.audit_logs.map((entry) => (
                  <div
                    key={entry.audit_id}
                    className="flex flex-wrap items-baseline justify-between gap-2 border-t pt-1.5 text-[11px]"
                    style={{ borderColor: 'var(--color-border-muted)' }}
                  >
                    <div className="min-w-0 space-y-0.5">
                      <div style={{ color: 'var(--color-text-primary)' }}>
                        {entry.reason}
                        {entry.masked_ilink_user_id ? ` (${entry.masked_ilink_user_id})` : ''}
                      </div>
                      <div style={{ color: 'var(--color-text-tertiary)' }}>
                        {entry.target_user_id
                          ? t('dalton.weixinBot.auditActorTarget', {
                              operator: entry.operator_user_id,
                              target: entry.target_user_id,
                            })
                          : t('dalton.weixinBot.auditActorOnly', {
                              operator: entry.operator_user_id,
                            })}
                      </div>
                    </div>
                    {entry.created_at && (
                      <span className="shrink-0" style={{ color: 'var(--color-text-tertiary)' }}>
                        {formatTimestamp(entry.created_at, locale)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </details>
          )}
        </>
      )}

      <Dialog
        open={Boolean(unbindTarget)}
        onOpenChange={(open) => {
          if (!open && !unbindMutation.isPending) {
            setUnbindTarget(null);
            setSelectedReason('');
            setDialogError(null);
          }
        }}
      >
        <DialogContent
          className="sm:max-w-md border"
          style={{
            backgroundColor: 'var(--color-shell-float, var(--color-bg-elevated))',
            borderColor: 'var(--color-shell-edge, var(--color-border-elevated))',
          }}
        >
          <DialogHeader>
            <DialogTitle style={{ color: 'var(--color-text-primary)' }}>
              {unbindTarget?.account_id
                ? t('dalton.weixinBot.unbindDialogTitle')
                : t('dalton.weixinBot.revokeDialogTitle')}
            </DialogTitle>
            <p className="text-xs leading-5" style={{ color: 'var(--color-text-secondary)' }}>
              {unbindTarget?.account_id
                ? t('dalton.weixinBot.unbindDialogDesc', {
                    member:
                      unbindTarget.user_name ||
                      unbindTarget.user_email ||
                      unbindTarget.user_id,
                    masked: unbindTarget.masked_ilink_user_id || '-',
                  })
                : t('dalton.weixinBot.revokeDialogDesc', {
                    member:
                      unbindTarget?.user_name ||
                      unbindTarget?.user_email ||
                      unbindTarget?.user_id ||
                      '',
                  })}
            </p>
          </DialogHeader>

          <div className="space-y-2 py-2">
            <label
              htmlFor="dalton-weixin-admin-unbind-reason"
              className="block text-xs font-medium"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {t('dalton.weixinBot.reasonLabel')}
            </label>
            <select
              id="dalton-weixin-admin-unbind-reason"
              data-testid="dalton-weixin-admin-unbind-reason"
              value={selectedReason}
              disabled={unbindMutation.isPending}
              onChange={(e) =>
                setSelectedReason(e.target.value as DaltonWeixinAdminUnbindReason | '')
              }
              className="w-full h-9 rounded-md border px-2.5 text-xs"
              style={{
                borderColor: 'var(--color-border-default)',
                backgroundColor: 'var(--color-bg-card)',
                color: 'var(--color-text-primary)',
              }}
            >
              <option value="">{t('dalton.weixinBot.reasonPlaceholder')}</option>
              {DALTON_WEIXIN_ADMIN_UNBIND_REASONS.map((reason) => (
                <option key={reason} value={reason}>
                  {t(REASON_LABEL_KEYS[reason])}
                </option>
              ))}
            </select>
            {dialogError && <DaltonInlineNotice tone="error">{dialogError}</DaltonInlineNotice>}
          </div>

          <DialogFooter className="gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 text-xs tap-target"
              disabled={unbindMutation.isPending}
              onClick={() => {
                setUnbindTarget(null);
                setSelectedReason('');
                setDialogError(null);
              }}
            >
              {t('dalton.common.cancel')}
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              className="h-8 text-xs tap-target"
              disabled={!selectedReason || unbindMutation.isPending}
              onClick={handleConfirmUnbind}
              data-testid="dalton-weixin-admin-unbind-confirm"
            >
              {unbindMutation.isPending
                ? t('dalton.weixinBot.unbinding')
                : t('dalton.weixinBot.confirmUnbind')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </DaltonSettingsCard>
  );
}
