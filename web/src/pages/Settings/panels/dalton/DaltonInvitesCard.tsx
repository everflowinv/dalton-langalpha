import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Check, Copy, UserPlus } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/hooks/useLocale';
import { isDaltonAuth, isDaltonLocalAuth } from '@/config/daltonAuth';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { copyText } from '@/lib/clipboard';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  createDaltonInvite,
  fetchDaltonInvites,
  revokeDaltonInvite,
  type DaltonInviteItem,
  type DaltonInviteStatus,
  type DaltonInviteTier,
} from '@/pages/Settings/utils/daltonApi';
import type { DaltonSettingVisibilityContext } from './DaltonCosSharingCard';
import {
  DaltonActionConfirmButton,
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonSettingsCard,
  DaltonStatusBadge,
  type DaltonStatusTone,
} from '@/components/dalton/primitives';

export function canShowDaltonInviteAdminCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  if (!isDaltonAuth) return false;
  if (!ctx) return false;
  if (ctx.canInvite === false) return false;
  return (
    ctx.canInvite === true ||
    ctx.callerTier === 'admin' ||
    ctx.callerTier === 'internal'
  );
}

export const isDaltonInvitesSettingVisible = canShowDaltonInviteAdminCard;

const STATUS_TONE: Record<DaltonInviteStatus, DaltonStatusTone> = {
  pending: 'accent',
  used: 'success',
  expired: 'neutral',
  revoked: 'danger',
};

/** Single-use member invite links management card (internal & admin callers). */
export function DaltonInvitesCard({
  ctx,
}: {
  ctx?: DaltonSettingVisibilityContext | null;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const { canInvite, callerTier: hookCallerTier } = useTeamCredentials();
  const queryClient = useQueryClient();
  const effectiveTier = ctx?.callerTier ?? hookCallerTier;
  const canPickTier = isDaltonLocalAuth && effectiveTier === 'admin';
  const [selectedTier, setSelectedTier] = useState<DaltonInviteTier>('external');

  const [createdUrl, setCreatedUrl] = useState<{ url: string; expiresAt: string | null } | null>(
    null,
  );
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const allowed = ctx ? isDaltonInvitesSettingVisible(ctx) : canInvite;

  const invitesQuery = useQuery({
    queryKey: queryKeys.dalton.invites(),
    queryFn: fetchDaltonInvites,
    enabled: allowed,
  });

  const createMutation = useMutation({
    mutationFn: (tier?: DaltonInviteTier) => createDaltonInvite(tier),
    onSuccess: (created) => {
      const origin = typeof window !== 'undefined' ? window.location.origin : '';
      const rawUrl = created.url || created.invite_url || '';
      const fullUrl = rawUrl.startsWith('http') ? rawUrl : `${origin}${rawUrl}`;
      setCreatedUrl({ url: fullUrl, expiresAt: created.expires_at ?? null });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.invites() });
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (inviteId: string) => revokeDaltonInvite(inviteId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.invites() });
    },
  });

  const handleCopy = async (url: string) => {
    try {
      await copyText(url);
      setCopied(true);
    } catch {
      /* clipboard blocked */
    }
  };

  if (!allowed) {
    return null;
  }

  const invites = invitesQuery.data?.invites ?? [];
  const activeCount =
    invitesQuery.data?.active_count ??
    invites.filter((i: DaltonInviteItem) => i.status === 'pending').length;
  const maxActive = invitesQuery.data?.max_active;

  const statusLabel = (status: DaltonInviteStatus) => {
    switch (status) {
      case 'pending':
        return t('auth.invites.statusPending');
      case 'used':
        return t('auth.invites.statusUsed');
      case 'expired':
        return t('auth.invites.statusExpired');
      case 'revoked':
        return t('auth.invites.statusRevoked');
    }
  };

  const tierLabel = (tier?: DaltonInviteTier) => {
    switch (tier) {
      case 'admin':
        return t('auth.invites.tierAdmin');
      case 'internal':
        return t('auth.invites.tierInternal');
      case 'external':
        return t('auth.invites.tierExternal');
      default:
        return null;
    }
  };

  return (
    <DaltonSettingsCard
      id="dalton-invites"
      testId="dalton-invites-card"
      title={t('auth.invites.title')}
      description={t('auth.invites.subtitle')}
      layout="padded"
      bodyClassName="space-y-3"
      action={
        <div className="flex items-center gap-2">
          {typeof maxActive === 'number' && (
            <DaltonStatusBadge
              tone="neutral"
              label={t('auth.invites.activeQuota', {
                count: activeCount,
                max: maxActive,
              })}
            />
          )}
          {canPickTier && (
            <select
              aria-label={t('auth.invites.tierLabel')}
              data-testid="dalton-invites-tier-select"
              value={selectedTier}
              onChange={(e) => setSelectedTier(e.target.value as DaltonInviteTier)}
              disabled={createMutation.isPending}
              className="h-8 rounded-md border px-2 text-xs"
              style={{
                backgroundColor: 'var(--color-bg-card)',
                borderColor: 'var(--color-border-muted)',
                color: 'var(--color-text-primary)',
              }}
            >
              <option value="external">{t('auth.invites.tierExternal')}</option>
              <option value="internal">{t('auth.invites.tierInternal')}</option>
              <option value="admin">{t('auth.invites.tierAdmin')}</option>
            </select>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            disabled={createMutation.isPending}
            onClick={() => createMutation.mutate(canPickTier ? selectedTier : undefined)}
            data-testid="dalton-invites-generate"
          >
            <UserPlus className="h-3.5 w-3.5" />
            <span>
              {createMutation.isPending
                ? t('auth.invites.generating')
                : t('auth.invites.generate')}
            </span>
          </Button>
        </div>
      }
    >
      {createMutation.isError && (
        <DaltonInlineNotice tone="error">{t('dalton.common.saveFailed')}</DaltonInlineNotice>
      )}
      {revokeMutation.isError && (
        <DaltonInlineNotice tone="error">{t('dalton.common.saveFailed')}</DaltonInlineNotice>
      )}

      {createdUrl && (
        <DaltonInlineNotice
          tone="success"
          testId="dalton-invites-new-link"
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs shrink-0"
              onClick={() => void handleCopy(createdUrl.url)}
              data-testid="dalton-invites-copy"
            >
              {copied ? (
                <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )}
              <span>{copied ? t('auth.invites.copied') : t('auth.invites.copyLink')}</span>
            </Button>
          }
        >
          <div className="space-y-1">
            <div>
              {t('auth.invites.newLinkHint', {
                expiresAt: createdUrl.expiresAt
                  ? formatTimestamp(createdUrl.expiresAt, locale)
                  : '',
              })}
            </div>
            <code className="block font-mono text-xs select-all break-all">{createdUrl.url}</code>
          </div>
        </DaltonInlineNotice>
      )}

      {invitesQuery.isLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : invitesQuery.isError ? (
        <DaltonInlineNotice tone="error">{t('dalton.common.loadFailed')}</DaltonInlineNotice>
      ) : (
        <DaltonKeyValueList isEmpty={invites.length === 0} empty={t('auth.invites.empty')}>
          {invites.map((item) => {
            const usedBy = item.used_by_username || item.username;
            const isRevoking =
              revokeMutation.isPending && revokeMutation.variables === item.invite_id;
            return (
              <DaltonItemRow
                key={item.invite_id}
                testId={`dalton-invite-row-${item.invite_id}`}
                leading={
                  <DaltonStatusBadge
                    tone={STATUS_TONE[item.status] ?? 'neutral'}
                    label={statusLabel(item.status)}
                  />
                }
                title={
                  <span className="inline-flex items-center gap-2">
                    <span className="font-mono text-xs">{item.invite_id.slice(0, 12)}</span>
                    {isDaltonLocalAuth && tierLabel(item.tier) && (
                      <DaltonStatusBadge tone="neutral" label={tierLabel(item.tier)!} />
                    )}
                  </span>
                }
                meta={
                  <span className="flex flex-wrap gap-x-3 gap-y-0.5">
                    {item.created_at && (
                      <span>
                        {t('auth.invites.colCreatedAt')}:{' '}
                        {formatTimestamp(item.created_at, locale)}
                      </span>
                    )}
                    {item.expires_at && (
                      <span>
                        {t('auth.invites.colExpiresAt')}:{' '}
                        {formatTimestamp(item.expires_at, locale)}
                      </span>
                    )}
                    {usedBy && (
                      <span>
                        {t('auth.invites.colUsedBy')}: {usedBy}
                      </span>
                    )}
                  </span>
                }
                trailing={
                  item.status === 'pending' ? (
                    <DaltonActionConfirmButton
                      label={
                        isRevoking ? t('auth.invites.revoking') : t('auth.invites.revoke')
                      }
                      confirmTitle={t('auth.invites.revokeTitle')}
                      confirmMessage={t('auth.invites.revokeConfirm')}
                      confirmLabel={t('auth.invites.revoke')}
                      busy={isRevoking}
                      onConfirm={() =>
                        revokeMutation.mutateAsync(item.invite_id).then(() => undefined)
                      }
                      testId={`dalton-invite-revoke-${item.invite_id}`}
                    />
                  ) : undefined
                }
              />
            );
          })}
        </DaltonKeyValueList>
      )}
    </DaltonSettingsCard>
  );
}
