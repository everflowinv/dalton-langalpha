import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, Link2, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/hooks/useLocale';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  createDaltonInvite,
  fetchDaltonInvites,
  revokeDaltonInvite,
  type DaltonInviteCreateResponse,
  type DaltonInviteItem,
  type DaltonInviteStatus,
} from '../../utils/daltonApi';
import type { DaltonSettingVisibilityContext } from './DaltonCosSharingCard';

export function canShowDaltonInviteAdminCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  return Boolean(ctx?.canInvite);
}

function extractApiError(err: unknown, fallback: string): string {
  if (err && typeof err === 'object') {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
    const msg = (err as { message?: unknown }).message;
    if (typeof msg === 'string' && msg.trim()) {
      return msg;
    }
  }
  return fallback;
}

export function DaltonInvitesCard(): React.ReactElement | null {
  const { canInvite } = useTeamCredentials();
  const locale = useLocale();
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [latestInvite, setLatestInvite] = useState<DaltonInviteCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: queryKeys.dalton.invites(),
    queryFn: fetchDaltonInvites,
    enabled: canInvite,
  });

  const createMutation = useMutation({
    mutationFn: createDaltonInvite,
    onSuccess: (created) => {
      setError(null);
      setLatestInvite(created);
      setCopied(false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.invites() });
    },
    onError: (err: unknown) => {
      setError(extractApiError(err, t('common.failed')));
    },
  });

  const revokeMutation = useMutation({
    mutationFn: async (inviteId: string) => {
      setRevokingId(inviteId);
      return revokeDaltonInvite(inviteId);
    },
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.invites() });
    },
    onError: (err: unknown) => {
      setError(extractApiError(err, t('common.failed')));
    },
    onSettled: () => {
      setRevokingId(null);
    },
  });

  if (!canShowDaltonInviteAdminCard({ canInvite })) {
    return null;
  }

  const invites: DaltonInviteItem[] = data?.invites ?? [];

  const renderStatusLabel = (status: DaltonInviteStatus): string => {
    switch (status) {
      case 'used':
        return t('auth.invites.statusUsed');
      case 'expired':
        return t('auth.invites.statusExpired');
      case 'revoked':
        return t('auth.invites.statusRevoked');
      case 'pending':
      default:
        return t('auth.invites.statusPending');
    }
  };

  const handleCopy = async (url: string) => {
    try {
      await navigator.clipboard?.writeText(url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const latestUrl = latestInvite?.url || latestInvite?.invite_url || '';

  return (
    <div
      id="dalton-invites"
      className="flex flex-col gap-3 rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-invites-card"
    >
      <div
        className="flex items-start justify-between gap-4 border-b pb-3"
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        <div className="flex flex-col gap-1">
          <span
            className="text-sm font-semibold"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('auth.invites.title')}
          </span>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('auth.invites.subtitle')}
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          onClick={() => createMutation.mutate()}
          disabled={createMutation.isPending}
          data-testid="dalton-invite-generate-btn"
        >
          <Link2 className="mr-1.5 h-3.5 w-3.5" />
          {createMutation.isPending ? t('auth.invites.generating') : t('auth.invites.generate')}
        </Button>
      </div>

      {latestInvite && latestUrl && (
        <div
          className="flex flex-col gap-2 rounded p-3"
          style={{
            backgroundColor: 'var(--color-bg-tag)',
            border: '1px solid var(--color-border-muted)',
          }}
          data-testid="dalton-invite-generated-box"
        >
          <div className="flex items-center justify-between gap-2">
            <code
              className="truncate text-xs font-mono"
              style={{ color: 'var(--color-text-primary)' }}
              data-testid="dalton-invite-generated-url"
            >
              {latestUrl}
            </code>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void handleCopy(latestUrl)}
              data-testid="dalton-invite-copy-btn"
            >
              {copied ? (
                <>
                  <Check className="mr-1 h-3.5 w-3.5" />
                  {t('auth.invites.copied')}
                </>
              ) : (
                <>
                  <Copy className="mr-1 h-3.5 w-3.5" />
                  {t('auth.invites.copyLink')}
                </>
              )}
            </Button>
          </div>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('auth.invites.newLinkHint', {
              expiresAt: formatTimestamp(latestInvite.expires_at, locale) || '—',
            })}
          </p>
        </div>
      )}

      {error && (
        <p role="alert" className="text-xs" style={{ color: 'var(--color-loss)' }}>
          {error}
        </p>
      )}

      {isLoading ? (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('common.loading')}
        </p>
      ) : invites.length === 0 ? (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('auth.invites.empty')}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs" data-testid="dalton-invites-table">
            <thead>
              <tr
                className="border-b"
                style={{
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-tertiary)',
                }}
              >
                <th className="py-1.5 pr-3 font-medium">{t('auth.invites.colStatus')}</th>
                <th className="py-1.5 pr-3 font-medium">{t('auth.invites.colCreatedAt')}</th>
                <th className="py-1.5 pr-3 font-medium">{t('auth.invites.colExpiresAt')}</th>
                <th className="py-1.5 pr-3 font-medium">{t('auth.invites.colUsedBy')}</th>
                <th className="py-1.5 text-right font-medium">{t('auth.invites.colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {invites.map((item) => {
                const usedBy = item.used_by_username || item.username || '—';
                const isRevoking = revokingId === item.invite_id && revokeMutation.isPending;
                return (
                  <tr
                    key={item.invite_id}
                    className="border-b last:border-b-0"
                    style={{ borderColor: 'var(--color-border-muted)' }}
                    data-testid={`dalton-invite-row-${item.invite_id}`}
                  >
                    <td className="py-2 pr-3 font-medium" style={{ color: 'var(--color-text-primary)' }}>
                      {renderStatusLabel(item.status)}
                    </td>
                    <td className="py-2 pr-3" style={{ color: 'var(--color-text-secondary)' }}>
                      {formatTimestamp(item.created_at, locale) || '—'}
                    </td>
                    <td className="py-2 pr-3" style={{ color: 'var(--color-text-secondary)' }}>
                      {formatTimestamp(item.expires_at, locale) || '—'}
                    </td>
                    <td className="py-2 pr-3 font-mono" style={{ color: 'var(--color-text-primary)' }}>
                      {usedBy}
                    </td>
                    <td className="py-2 text-right">
                      {item.status === 'pending' && (
                        <button
                          type="button"
                          onClick={() => revokeMutation.mutate(item.invite_id)}
                          disabled={isRevoking}
                          className="inline-flex items-center gap-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                          style={{ color: 'var(--color-loss)' }}
                          data-testid={`dalton-invite-revoke-${item.invite_id}`}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {isRevoking ? t('auth.invites.revoking') : t('auth.invites.revoke')}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
