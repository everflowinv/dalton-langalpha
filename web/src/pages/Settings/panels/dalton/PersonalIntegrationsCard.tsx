import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ExternalLink, RefreshCw, ShieldCheck } from 'lucide-react';
import { ApiError } from '@/api/client';
import { Button } from '@/components/ui/button';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';
import {
  PERSONAL_INTEGRATION_CAPABILITIES,
  useCapabilities,
  type CapabilityId,
} from '@/hooks/useCapabilities';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  authorizePersonalIntegration,
  confirmPersonalIntegration,
  disconnectPersonalIntegrationProvider,
  disconnectPersonalIntegrationService,
  retryPersonalIntegrationRevoke,
  usePersonalIntegrationsQuery,
  type PersonalIntegrationConnection,
  type PersonalIntegrationConnectorInfo,
  type PersonalIntegrationProvider,
  type PersonalIntegrationPurgeLedger,
  type PersonalIntegrationService,
  type PersonalIntegrationsListResponse,
} from '@/lib/dalton/personalIntegrationsApi';

interface BannerPurgeTracking {
  kind: 'service' | 'provider_all' | 'switch' | 'retry_revoke';
  provider: PersonalIntegrationProvider;
  service?: PersonalIntegrationService;
  purgeIds: string[];
  baselineData: PersonalIntegrationsListResponse | undefined;
  feishuSuffix?: string;
  remoteMode?: 'remote_if_sole' | 'local_only';
}

interface BannerState {
  tone: 'info' | 'success' | 'warning' | 'error';
  text: string;
  tracking?: BannerPurgeTracking;
}

interface PendingAccountSwitchState {
  confirmationToken: string;
  provider: PersonalIntegrationProvider;
  service: PersonalIntegrationService;
  previousEmail: string | null;
  newEmail: string | null;
}

interface DisconnectDialogState {
  connection: PersonalIntegrationConnection;
  hasGoogleSibling: boolean;
}

const SERVICE_META: Record<
  `${PersonalIntegrationProvider}:${PersonalIntegrationService}`,
  { title: string; subtitle: string; capability: CapabilityId }
> = {
  'google:calendar': {
    title: 'Google 日历',
    subtitle: '在私密对话中查询日程、查看空闲时段，并在确认后创建、修改或删除日程。',
    capability: 'google_calendar',
  },
  'feishu:calendar': {
    title: '飞书日历',
    subtitle: '连接本人飞书日历，在私密对话中查询和管理个人日程与提醒。',
    capability: 'feishu_calendar',
  },
  'feishu:mail': {
    title: '飞书邮箱',
    subtitle: '在私密对话中检索和阅读本人飞书邮件，按需开启二次确认发信。',
    capability: 'feishu_mail',
  },
  'google:mail': {
    title: 'Gmail',
    subtitle: '在私密对话中检索和阅读本人邮件，并可按需保存邮件草稿。',
    capability: 'google_gmail',
  },
};

function serviceKey(
  provider: PersonalIntegrationProvider,
  service: PersonalIntegrationService,
): `${PersonalIntegrationProvider}:${PersonalIntegrationService}` {
  return `${provider}:${service}`;
}

function statusBadgeMeta(
  conn: PersonalIntegrationConnection | undefined,
  connector: PersonalIntegrationConnectorInfo | undefined,
): { label: string; tone: 'success' | 'warning' | 'accent' | 'neutral' } {
  if (conn) {
    switch (conn.status) {
      case 'connected':
        return { label: '已连接', tone: 'success' };
      case 'needs_reauth':
        return { label: '需重新授权', tone: 'warning' };
      case 'refresh_ambiguous':
        return { label: '状态待确认', tone: 'warning' };
      case 'disconnect_pending':
      case 'revoked_purging':
        return { label: '正在清理数据', tone: 'accent' };
    }
  }
  if (connector && !connector.credentials_ready) {
    return { label: '待配置凭据', tone: 'neutral' };
  }
  if (connector && !connector.ready) {
    return { label: '待完成环境配置', tone: 'warning' };
  }
  return { label: '可连接', tone: 'neutral' };
}

function isGmailPartialGrant(conn: PersonalIntegrationConnection): boolean {
  if (conn.provider !== 'google' || conn.service !== 'mail') return false;
  const scopes = new Set(conn.granted_scopes);
  const hasDraft =
    scopes.has('https://www.googleapis.com/auth/gmail.drafts.create') ||
    scopes.has('https://www.googleapis.com/auth/gmail.compose');
  return scopes.has('https://www.googleapis.com/auth/gmail.readonly') && !hasDraft;
}

function extractApiErrorData(err: unknown): Record<string, unknown> | null {
  if (err instanceof ApiError && err.response?.data && typeof err.response.data === 'object') {
    const data = err.response.data as Record<string, unknown>;
    if (data.detail && typeof data.detail === 'object') {
      return data.detail as Record<string, unknown>;
    }
    return data;
  }
  return null;
}

function resolvePrivacyPolicyUrl(profile: string): string | null {
  if (profile === 'everflow') {
    return '/help/everflow/privacy/';
  }
  const custom = import.meta.env.VITE_PRIVACY_URL?.trim();
  return custom || null;
}

export function PersonalIntegrationsCard() {
  const queryClient = useQueryClient();
  const locale = useLocale();
  const { hasCapability, profile } = useCapabilities();
  const anyCapEnabled = hasCapability(PERSONAL_INTEGRATION_CAPABILITIES);
  const { data, isLoading, isFetching, error, refetch } = usePersonalIntegrationsQuery(true);

  const [banner, setBanner] = useState<BannerState | null>(null);
  const [preSwitchTarget, setPreSwitchTarget] = useState<{
    provider: PersonalIntegrationProvider;
    service: PersonalIntegrationService;
  } | null>(null);
  const [pendingSwitch, setPendingSwitch] = useState<PendingAccountSwitchState | null>(null);
  const [disconnectDialog, setDisconnectDialog] = useState<DisconnectDialogState | null>(null);
  const callbackHandledRef = useRef(false);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.personalIntegrations() });
  };

  // Handle OAuth callback query params + #confirmation_token fragment once on mount
  useEffect(() => {
    if (callbackHandledRef.current || typeof window === 'undefined') return;
    const url = new URL(window.location.href);
    const status = url.searchParams.get('integration_status');
    const cbProvider = url.searchParams.get('provider') as PersonalIntegrationProvider | null;
    const cbService = url.searchParams.get('service') as PersonalIntegrationService | null;
    const errorCode = url.searchParams.get('error_code');
    const partialGrant = url.searchParams.get('partial_grant') === '1';

    const hashParams = new URLSearchParams(url.hash.replace(/^#/, ''));
    const confirmToken = hashParams.get('confirmation_token');

    if (!status && !confirmToken) return;
    callbackHandledRef.current = true;

    // Scrub sensitive fragment token and transient callback params from address bar immediately
    for (const key of [
      'integration_status',
      'provider',
      'service',
      'error_code',
      'partial_grant',
    ]) {
      url.searchParams.delete(key);
    }
    hashParams.delete('confirmation_token');
    const nextHash = hashParams.toString();
    url.hash = nextHash ? `#${nextHash}` : '#dalton-personal-integrations';
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);

    if (status === 'connected') {
      setBanner({
        tone: 'success',
        text: partialGrant
          ? '已连接账号（当前为只读授权，如需额外写入权限可重新授权并勾选）。'
          : '已成功连接个人账号。',
      });
      invalidate();
      return;
    }

    if (status === 'error') {
      if (errorCode === 'missing_required_scopes') {
        setBanner({
          tone: 'warning',
          text: '授权时请勾选「日历事件」等基础权限，重新点击连接即可完成配置。',
        });
      } else if (errorCode === 'missing_refresh_token_on_account_switch') {
        setBanner({
          tone: 'warning',
          text: '换账号时需重新授予离线访问权限，已保持原账号连接，请重试换账号。',
        });
      } else {
        setBanner({
          tone: 'warning',
          text: '本次授权尚未完成，请重新点击连接后再试。',
        });
      }
      invalidate();
      return;
    }

    if (
      confirmToken &&
      (status === 'confirmation_required' || status === 'account_switch_confirmation_required')
    ) {
      void (async () => {
        try {
          const res = await confirmPersonalIntegration({
            confirmation_token: confirmToken,
            confirm_account_switch: false,
          });
          if (res.status === 'connected') {
            setBanner({ tone: 'success', text: '已确认登录身份并启用个人连接。' });
            invalidate();
          } else if (res.status === 'account_switch_confirmation_required') {
            setPendingSwitch({
              confirmationToken: confirmToken,
              provider: res.provider,
              service: res.service,
              previousEmail: res.previous_account_email ?? null,
              newEmail: res.new_account_email ?? null,
            });
          }
        } catch (err) {
          const payload = extractApiErrorData(err);
          if (payload?.code === 'account_switch_confirmation_required') {
            setPendingSwitch({
              confirmationToken: confirmToken,
              provider:
                (payload.provider as PersonalIntegrationProvider) || cbProvider || 'google',
              service:
                (payload.service as PersonalIntegrationService) || cbService || 'calendar',
              previousEmail:
                typeof payload.previous_account_email === 'string'
                  ? payload.previous_account_email
                  : null,
              newEmail:
                typeof payload.new_account_email === 'string'
                  ? payload.new_account_email
                  : null,
            });
            return;
          }
          setBanner({
            tone: 'error',
            text:
              typeof payload?.message === 'string'
                ? payload.message
                : '确认授权时出现问题，请重新发起连接。',
          });
        }
      })();
    }
  }, []);

  const authorizeMutation = useMutation({
    mutationFn: async (vars: {
      provider: PersonalIntegrationProvider;
      service: PersonalIntegrationService;
      switchAccount?: boolean;
    }) => {
      const res = await authorizePersonalIntegration(vars.provider, vars.service, {
        switch_account: Boolean(vars.switchAccount),
        return_to: '/settings?tab=integrations',
      });
      return res;
    },
    onSuccess: (res) => {
      setPreSwitchTarget(null);
      if (res.authorize_url) {
        window.location.assign(res.authorize_url);
      }
    },
    onError: (err) => {
      setPreSwitchTarget(null);
      const payload = extractApiErrorData(err);
      setBanner({
        tone: 'warning',
        text:
          typeof payload?.message === 'string'
            ? payload.message
            : '发起授权失败，请确认管理员已完成凭据配置后重试。',
      });
    },
  });

  const confirmSwitchMutation = useMutation({
    mutationFn: async (vars: {
      token: string;
      confirmAccountSwitch: boolean;
      cancel: boolean;
    }) =>
      confirmPersonalIntegration({
        confirmation_token: vars.token,
        confirm_account_switch: vars.confirmAccountSwitch,
        cancel: vars.cancel,
      }),
    onSuccess: (res) => {
      setPendingSwitch(null);
      if (res.status === 'connected') {
        setBanner({
          tone: 'success',
          text: '已切换为新账号，原账号留下的对话个人数据正在后台清理。',
          tracking: {
            kind: 'switch',
            provider: res.provider,
            service: res.service,
            purgeIds: res.purge_id ? [res.purge_id] : [],
            baselineData: data,
          },
        });
      } else if (res.status === 'cancelled') {
        setBanner({ tone: 'info', text: '已取消换账号，继续保留原账号连接。' });
      }
      invalidate();
    },
    onError: (err) => {
      const payload = extractApiErrorData(err);
      setBanner({
        tone: 'error',
        text:
          typeof payload?.message === 'string'
            ? payload.message
            : '换账号确认失败，请重新发起换账号。',
      });
    },
  });

  const disconnectServiceMutation = useMutation({
    mutationFn: async (vars: {
      connection: PersonalIntegrationConnection;
      mode: 'remote_if_sole' | 'local_only';
    }) => {
      const res = await disconnectPersonalIntegrationService(
        vars.connection.provider,
        vars.connection.service,
        vars.mode,
      );
      return {
        res,
        prevStatus: vars.connection.status,
        provider: vars.connection.provider,
        service: vars.connection.service,
        mode: vars.mode,
      };
    },
    onSuccess: ({ res, prevStatus, provider, service, mode }) => {
      setDisconnectDialog(null);
      const localPurgeStatus = res.local_purge_status;
      const remoteRevokeStatus = res.remote_revoke_status;
      const purgeIds = res.purge_id ? [res.purge_id] : [];
      if (provider === 'feishu') {
        const ambiguousNote =
          prevStatus === 'refresh_ambiguous'
            ? ' 此前连接处于待确认刷新状态，飞书侧可能留存最长 30 天刷新令牌，建议前往飞书客户端移除应用授权。'
            : '';
        const feishuSuffix =
          '短期访问令牌最长在 7,200 秒内自然失效，可随时在飞书客户端移除应用授权。' +
          ambiguousNote;
        const purgeSentence =
          localPurgeStatus === 'running'
            ? '已在本地断开飞书连接，正在后台清理相关对话个人数据。'
            : localPurgeStatus === 'completed' || localPurgeStatus === 'not_required'
              ? '已在本地断开飞书连接，相关对话个人数据已清理完成。'
              : '已在本地断开飞书连接并启动关联对话个人数据清理。';
        setBanner({
          tone: 'info',
          text: purgeSentence + feishuSuffix,
          tracking:
            localPurgeStatus === 'running' || !localPurgeStatus
              ? {
                  kind: 'service',
                  provider,
                  service,
                  purgeIds,
                  baselineData: data,
                  feishuSuffix,
                  remoteMode: mode,
                }
              : undefined,
        });
      } else {
        const googleText =
          localPurgeStatus === 'running'
            ? '已断开连接，正在后台清理关联对话个人数据。'
            : remoteRevokeStatus === 'failed_retryable'
              ? '已断开连接，关联个人数据已清理完成，Google 远端撤销待重试。'
              : remoteRevokeStatus === 'pending'
                ? '已断开连接，关联个人数据已清理完成，正在后台撤销 Google 授权。'
                : localPurgeStatus === 'completed' || localPurgeStatus === 'not_required'
                  ? '已断开连接，关联个人数据已清理完成。'
                  : '已断开连接并启动关联个人数据清理。';
        const needsTracking =
          localPurgeStatus === 'running' ||
          remoteRevokeStatus === 'pending' ||
          remoteRevokeStatus === 'failed_retryable' ||
          !localPurgeStatus;
        setBanner({
          tone:
            remoteRevokeStatus === 'failed_retryable'
              ? 'warning'
              : localPurgeStatus === 'running' || remoteRevokeStatus === 'pending'
                ? 'info'
                : 'success',
          text: googleText,
          tracking: needsTracking
            ? {
                kind: 'service',
                provider,
                service,
                purgeIds,
                baselineData: data,
                remoteMode: mode,
              }
            : undefined,
        });
      }
      invalidate();
    },
    onError: (err) => {
      const payload = extractApiErrorData(err);
      setBanner({
        tone: 'error',
        text:
          typeof payload?.message === 'string'
            ? payload.message
            : '断开连接失败，请稍后重试。',
      });
    },
  });

  const disconnectAllGoogleMutation = useMutation({
    mutationFn: () => disconnectPersonalIntegrationProvider('google', 'remote_if_sole'),
    onSuccess: (res) => {
      setDisconnectDialog(null);
      const purges = Array.isArray(res?.purges) ? res.purges : [];
      const purgeIds = purges
        .map((p) => p.purge_id)
        .filter((id): id is string => typeof id === 'string' && id.length > 0);
      const hasRunningLocal = purges.some((p) => p.local_purge_status === 'running');
      const hasRetryableRemote = purges.some((p) => p.remote_revoke_status === 'failed_retryable');
      const hasPendingRemote = purges.some((p) => p.remote_revoke_status === 'pending');
      const tracking: BannerPurgeTracking | undefined =
        hasRunningLocal || hasRetryableRemote || hasPendingRemote
          ? {
              kind: 'provider_all',
              provider: 'google',
              purgeIds,
              baselineData: data,
              remoteMode: 'remote_if_sole',
            }
          : undefined;
      if (hasRunningLocal) {
        setBanner({
          tone: 'info',
          text: '已断开全部 Google 服务，正在后台清理关联本地个人数据，清理完成后将自动撤销 Google 授权。',
          tracking,
        });
      } else if (hasRetryableRemote) {
        setBanner({
          tone: 'warning',
          text: '已断开全部 Google 服务，关联本地个人数据已清理完成，Google 远端撤销待重试。',
          tracking,
        });
      } else if (hasPendingRemote) {
        setBanner({
          tone: 'info',
          text: '已断开全部 Google 服务，关联本地个人数据已清理完成，正在后台撤销 Google 授权。',
          tracking,
        });
      } else {
        setBanner({
          tone: 'success',
          text: '已断开全部 Google 服务，关联本地个人数据已清理完成。',
        });
      }
      invalidate();
    },
    onError: (err) => {
      const payload = extractApiErrorData(err);
      setBanner({
        tone: 'error',
        text:
          typeof payload?.message === 'string'
            ? payload.message
            : '断开全部 Google 服务失败，请稍后重试。',
      });
    },
  });

  const retryRevokeMutation = useMutation({
    mutationFn: (purgeId: string) => retryPersonalIntegrationRevoke(purgeId),
    onSuccess: () => {
      setBanner({ tone: 'success', text: '已触发 Google 远端撤销重试。' });
      invalidate();
    },
    onError: (err) => {
      const payload = extractApiErrorData(err);
      setBanner({
        tone: 'warning',
        text:
          typeof payload?.message === 'string'
            ? payload.message
            : '远端撤销重试暂未完成，系统会继续自动重试。',
      });
    },
  });

  const connectorMap = useMemo(() => {
    const map = new Map<string, PersonalIntegrationConnectorInfo>();
    for (const c of data?.connectors ?? []) {
      map.set(serviceKey(c.provider, c.service), c);
    }
    return map;
  }, [data?.connectors]);

  const connectionMap = useMemo(() => {
    const map = new Map<string, PersonalIntegrationConnection>();
    for (const c of data?.connections ?? []) {
      map.set(serviceKey(c.provider, c.service), c);
    }
    return map;
  }, [data?.connections]);

  const visibleRows = useMemo(() => {
    const keys: Array<`${PersonalIntegrationProvider}:${PersonalIntegrationService}`> = [
      'google:calendar',
      'feishu:calendar',
      'feishu:mail',
      'google:mail',
    ];
    return keys
      .map((k) => {
        const meta = SERVICE_META[k];
        const connector = connectorMap.get(k);
        const connection = connectionMap.get(k);
        const capOn = hasCapability(meta.capability) || Boolean(connector?.enabled);
        // Show service row when its capability is enabled OR a historical connection exists (§6 item 3)
        if (!capOn && !connection) return null;
        return {
          key: k,
          provider: k.split(':')[0] as PersonalIntegrationProvider,
          service: k.split(':')[1] as PersonalIntegrationService,
          title: meta.title,
          subtitle: meta.subtitle,
          capOn,
          connector,
          connection,
        };
      })
      .filter((item): item is NonNullable<typeof item> => item !== null);
  }, [connectorMap, connectionMap, hasCapability]);

  const retryablePurges: PersonalIntegrationPurgeLedger[] = useMemo(
    () =>
      (data?.active_purges ?? []).filter(
        (p) =>
          p.remote_revoke_status === 'failed_retryable' ||
          p.remote_revoke_status === 'pending' ||
          p.local_purge_status === 'running',
      ),
    [data?.active_purges],
  );

  // Keep the top banner in sync with polled purge records once a post-mutation query response lands.
  // Never infer "revoked" from a missing ledger row or a `superseded` revoke status.
  const displayBanner = useMemo<{
    tone: 'info' | 'success' | 'warning' | 'error';
    text: string;
  } | null>(() => {
    if (!banner) return null;
    const tracking = banner.tracking;
    if (!tracking || !data || data === tracking.baselineData) {
      return banner;
    }

    const activePurges = data.active_purges ?? [];
    const purgeById = new Map<string, PersonalIntegrationPurgeLedger>();
    for (const p of data.recent_purges ?? []) {
      purgeById.set(p.purge_id, p);
    }
    for (const p of activePurges) {
      purgeById.set(p.purge_id, p);
    }
    const allKnownPurges = Array.from(purgeById.values());

    const trackedPurges =
      tracking.purgeIds.length > 0
        ? tracking.purgeIds
            .map((id) => purgeById.get(id))
            .filter((p): p is PersonalIntegrationPurgeLedger => p !== undefined)
        : allKnownPurges.filter(
            (p) =>
              p.provider === tracking.provider &&
              (!tracking.service || !p.service || p.service === tracking.service),
          );
    const allTrackedFound =
      tracking.purgeIds.length > 0 && trackedPurges.length === tracking.purgeIds.length;

    const hasPurgingConn = (data.connections ?? []).some(
      (c) =>
        c.provider === tracking.provider &&
        (!tracking.service || c.service === tracking.service) &&
        (c.status === 'disconnect_pending' || c.status === 'revoked_purging'),
    );
    const hasRunningLocal =
      hasPurgingConn || trackedPurges.some((p) => p.local_purge_status === 'running');
    const hasRetryableRemote = trackedPurges.some(
      (p) => p.remote_revoke_status === 'failed_retryable',
    );
    const hasPendingRemote = trackedPurges.some((p) => p.remote_revoke_status === 'pending');
    const anyRemoteSuperseded = trackedPurges.some((p) => p.remote_revoke_status === 'superseded');
    const allLocalCompleted =
      allTrackedFound &&
      trackedPurges.every(
        (p) => p.local_purge_status === 'completed' || p.local_purge_status === 'not_required',
      );
    const allRemoteSucceeded =
      allTrackedFound && trackedPurges.every((p) => p.remote_revoke_status === 'succeeded');
    const allRemoteNotApplicable =
      allTrackedFound && trackedPurges.every((p) => p.remote_revoke_status === 'not_applicable');

    if (tracking.kind === 'provider_all') {
      if (hasRunningLocal) {
        return {
          tone: 'info',
          text: '已断开全部 Google 服务，正在后台清理关联本地个人数据，清理完成后将自动撤销 Google 授权。',
        };
      }
      if (hasRetryableRemote) {
        return {
          tone: 'warning',
          text: '已断开全部 Google 服务，关联本地个人数据已清理完成，Google 远端撤销待重试。',
        };
      }
      if (hasPendingRemote) {
        return {
          tone: 'info',
          text: '已断开全部 Google 服务，关联本地个人数据已清理完成，正在后台撤销 Google 授权。',
        };
      }
      if (anyRemoteSuperseded) {
        return {
          tone: 'info',
          text: '已断开原 Google 服务，因检测到新的授权，旧的 Google 远端撤销任务已终止。',
        };
      }
      if (allLocalCompleted && allRemoteSucceeded) {
        return {
          tone: 'success',
          text: '已断开全部 Google 服务，关联本地个人数据已清理完成，Google 授权已撤销。',
        };
      }
      if (allLocalCompleted && allRemoteNotApplicable) {
        return {
          tone: 'success',
          text: '已断开全部 Google 服务，关联本地个人数据已清理完成。',
        };
      }
      return {
        tone: 'info',
        text: '已断开全部 Google 服务，暂未收到 Google 远端撤销的明确确认结果，请稍后刷新查看。',
      };
    }

    if (tracking.kind === 'switch') {
      if (hasRunningLocal) {
        return {
          tone: 'info',
          text: '已切换为新账号，原账号留下的对话个人数据正在后台清理。',
        };
      }
      if (allLocalCompleted) {
        return {
          tone: 'success',
          text: '已切换为新账号，原账号留下的对话个人数据已清理完成。',
        };
      }
      return {
        tone: 'info',
        text: '已切换为新账号，请稍后刷新确认原账号数据清理状态。',
      };
    }

    if (tracking.provider === 'feishu') {
      const suffix = tracking.feishuSuffix ?? '';
      if (hasRunningLocal) {
        return {
          tone: 'info',
          text: '已在本地断开飞书连接，正在后台清理相关对话个人数据。' + suffix,
        };
      }
      if (allLocalCompleted) {
        return {
          tone: 'info',
          text: '已在本地断开飞书连接，相关对话个人数据已清理完成。' + suffix,
        };
      }
      return {
        tone: 'info',
        text: '已在本地断开飞书连接，请稍后刷新确认相关对话个人数据清理状态。' + suffix,
      };
    }

    if (hasRunningLocal) {
      return {
        tone: 'info',
        text: '已断开连接，正在后台清理关联对话个人数据。',
      };
    }
    if (hasRetryableRemote) {
      return {
        tone: 'warning',
        text: '已断开连接，关联个人数据已清理完成，Google 远端撤销待重试。',
      };
    }
    if (hasPendingRemote) {
      return {
        tone: 'info',
        text: '已断开连接，关联个人数据已清理完成，正在后台撤销 Google 授权。',
      };
    }
    if (anyRemoteSuperseded) {
      return {
        tone: 'info',
        text: '已断开原连接，因检测到新的授权，旧的 Google 远端撤销任务已终止。',
      };
    }
    if (allLocalCompleted && allRemoteSucceeded) {
      return {
        tone: 'success',
        text: '已断开连接，关联个人数据已清理完成，Google 授权已撤销。',
      };
    }
    if (
      allLocalCompleted &&
      (tracking.remoteMode !== 'remote_if_sole' || allRemoteNotApplicable)
    ) {
      return {
        tone: 'success',
        text: '已断开连接，关联个人数据已清理完成。',
      };
    }
    return {
      tone: 'info',
      text:
        tracking.remoteMode === 'remote_if_sole'
          ? '已断开连接，暂未收到 Google 远端撤销的明确确认结果，请稍后刷新查看。'
          : '已断开连接，请稍后刷新确认关联个人数据清理状态。',
    };
  }, [banner, data]);

  const privacyUrl = resolvePrivacyPolicyUrl(profile);
  const hasExistingHistory = Boolean(
    data?.has_personal_integrations ||
      (data?.connections?.length ?? 0) > 0 ||
      (data?.active_purges?.length ?? 0) > 0,
  );

  if (!anyCapEnabled && !hasExistingHistory && !displayBanner && !isLoading) {
    return null;
  }

  return (
    <DaltonSettingsCard
      id="dalton-personal-integrations"
      title="个人日历与邮箱连接"
      description="授权连接你的个人 Google 日历或飞书日历，在私密对话中查询日程、查看空闲时段，并在确认后管理个人日程。"
      action={
        <DaltonRefreshButton
          onClick={() => void refetch()}
          busy={isFetching}
          label="刷新连接状态"
        />
      }
    >
      <div className="space-y-4">
        {displayBanner && (
          <DaltonInlineNotice tone={displayBanner.tone} testId="personal-integrations-banner">
            {displayBanner.text}
          </DaltonInlineNotice>
        )}

        {isLoading && <DaltonLoadingRow label="正在加载个人连接状态…" />}

        {error && !isLoading && (
          <DaltonInlineNotice tone="warning" testId="personal-integrations-error">
            读取个人连接状态时遇到问题，可点击右上角刷新重试。
          </DaltonInlineNotice>
        )}

        {!isLoading && visibleRows.length > 0 && (
          <div className="divide-y divide-(--color-border-muted)">
            {visibleRows.map((row) => {
              const badge = statusBadgeMeta(row.connection, row.connector);
              const conn = row.connection;
              const isBusy =
                authorizeMutation.isPending ||
                disconnectServiceMutation.isPending ||
                disconnectAllGoogleMutation.isPending;
              const credsReady = row.connector ? row.connector.credentials_ready : true;
              const isReady = row.connector ? row.connector.ready : true;
              const canConnect = row.capOn && isReady;
              const googleSiblings =
                conn?.provider === 'google'
                  ? (data?.connections ?? []).filter(
                      (c) =>
                        c.provider === 'google' &&
                        c.provider_account_id === conn.provider_account_id &&
                        c.service !== conn.service,
                    )
                  : [];

              return (
                <div
                  key={row.key}
                  data-testid={`personal-integration-row-${row.provider}-${row.service}`}
                  className="py-3.5 first:pt-0 last:pb-0 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between"
                >
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-(--color-text-primary)">
                        {row.title}
                      </span>
                      <DaltonStatusBadge tone={badge.tone} label={badge.label} />
                      {!row.capOn && conn && (
                        <DaltonStatusBadge tone="neutral" label="历史连接" />
                      )}
                      {conn && isGmailPartialGrant(conn) && (
                        <DaltonStatusBadge tone="accent" label="只读授权" />
                      )}
                    </div>
                    <p className="text-xs text-(--color-text-secondary)">{row.subtitle}</p>

                    {conn && (
                      <div className="text-xs text-(--color-text-tertiary) space-y-0.5">
                        {conn.provider_email && <div>已连接账号：{conn.provider_email}</div>}
                        {conn.last_refresh_at && (
                          <div>最近同步：{formatTimestamp(conn.last_refresh_at, locale)}</div>
                        )}
                        {conn.client_rotated && (
                          <div className="text-(--color-warning)">
                            管理员已更新 OAuth 客户端，点击「重新授权」即可原地续接并保留历史对话。
                          </div>
                        )}
                        {conn.status === 'needs_reauth' && !conn.client_rotated && (
                          <div className="text-(--color-warning)">
                            {conn.last_error_code === 'missing_required_scopes'
                              ? '请重新授权并勾选「日历事件」权限。'
                              : '当前授权已失效，点击「重新授权」即可恢复使用。'}
                          </div>
                        )}
                        {conn.status === 'refresh_ambiguous' && (
                          <div className="text-(--color-warning)">
                            上次刷新结果待确认，点击「重新授权」可立即恢复稳定连接。
                          </div>
                        )}
                        {isGmailPartialGrant(conn) && (
                          <div>当前为只读授权，如需创建邮件草稿可重新授权并勾选草稿权限。</div>
                        )}
                      </div>
                    )}

                    {!conn && !isReady && row.capOn && (
                      <p className="text-xs text-(--color-text-tertiary)">
                        {!credsReady
                          ? '管理员在「部署设置 → 集成凭据」完成配置后即可连接。'
                          : '管理员完成公开回调地址与服务端加密配置后即可连接。'}
                      </p>
                    )}
                  </div>

                  <div className="flex flex-wrap items-center gap-2 shrink-0">
                    {!conn && row.capOn && (
                      <Button
                        type="button"
                        size="sm"
                        disabled={!canConnect || isBusy}
                        onClick={() =>
                          authorizeMutation.mutate({
                            provider: row.provider,
                            service: row.service,
                          })
                        }
                      >
                        连接
                      </Button>
                    )}

                    {conn &&
                      row.capOn &&
                      (conn.status === 'needs_reauth' ||
                        conn.status === 'refresh_ambiguous' ||
                        conn.client_rotated ||
                        isGmailPartialGrant(conn)) && (
                        <Button
                          type="button"
                          size="sm"
                          disabled={!canConnect || isBusy}
                          onClick={() =>
                            authorizeMutation.mutate({
                              provider: row.provider,
                              service: row.service,
                            })
                          }
                        >
                          重新授权
                        </Button>
                      )}

                    {conn &&
                      row.capOn &&
                      row.provider === 'google' &&
                      conn.status === 'connected' && (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={!canConnect || isBusy}
                          onClick={() =>
                            setPreSwitchTarget({
                              provider: row.provider,
                              service: row.service,
                            })
                          }
                        >
                          换账号
                        </Button>
                      )}

                    {conn &&
                      conn.status !== 'disconnect_pending' &&
                      conn.status !== 'revoked_purging' && (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={isBusy}
                          onClick={() =>
                            setDisconnectDialog({
                              connection: conn,
                              hasGoogleSibling: googleSiblings.length > 0,
                            })
                          }
                        >
                          断开连接
                        </Button>
                      )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {retryablePurges.length > 0 && (
          <div className="rounded-lg border border-(--color-border-muted) p-3 space-y-2">
            <div className="text-xs font-medium text-(--color-text-primary)">
              断开与远端撤销进度
            </div>
            {retryablePurges.map((purge) => {
              const purgeProviderLabel = purge.provider === 'google' ? 'Google' : '飞书';
              const statusText =
                purge.local_purge_status === 'running'
                  ? ` — 正在清理本地${purgeProviderLabel}对话个人数据`
                  : purge.remote_revoke_status === 'failed_retryable'
                    ? ` — 本地已清理，${purgeProviderLabel} 远端撤销待重试`
                    : purge.remote_revoke_status === 'pending'
                      ? ` — 本地已清理，正在后台撤销 ${purgeProviderLabel} 授权`
                      : ` — ${purgeProviderLabel} 本地个人数据已清理`;
              return (
                <div
                  key={purge.purge_id}
                  data-testid={`personal-integration-purge-${purge.purge_id}`}
                  className="flex flex-wrap items-center justify-between gap-2 text-xs text-(--color-text-secondary)"
                >
                  <span>
                    {purgeProviderLabel}
                    {purge.service ? ` · ${purge.service === 'calendar' ? '日历' : '邮箱'}` : ''}
                    {statusText}
                  </span>
                  {purge.remote_revoke_status === 'failed_retryable' && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={retryRevokeMutation.isPending}
                      onClick={() => retryRevokeMutation.mutate(purge.purge_id)}
                    >
                      <RefreshCw className="h-3.5 w-3.5 mr-1" />
                      重试远端撤销
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-xs text-(--color-text-tertiary)">
          <span className="inline-flex items-center gap-1.5">
            <ShieldCheck className="h-3.5 w-3.5 shrink-0" />
            个人连接数据仅在本人私密会话中使用，断开后会自动清除关联对话内容。
          </span>
          {privacyUrl && (
            <a
              href={privacyUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 underline hover:text-(--color-text-primary)"
            >
              隐私权政策
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      </div>

      {/* Pre-redirect Google account switch confirmation modal */}
      {preSwitchTarget && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="确认更换 Google 账号"
          data-testid="pre-account-switch-dialog"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
        >
          <div className="w-full max-w-md rounded-xl border border-(--color-border-muted) bg-(--color-bg-card) p-5 shadow-lg space-y-4">
            <h3 className="text-base font-medium text-(--color-text-primary)">
              确认更换 Google 账号
            </h3>
            <p className="text-sm text-(--color-text-secondary)">
              换账号会清除旧账号在对话中留下的个人数据，随后跳转到 Google 选择并授权新账号。
            </p>
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setPreSwitchTarget(null)}
              >
                取消
              </Button>
              <Button
                type="button"
                size="sm"
                disabled={authorizeMutation.isPending}
                onClick={() =>
                  authorizeMutation.mutate({
                    provider: preSwitchTarget.provider,
                    service: preSwitchTarget.service,
                    switchAccount: true,
                  })
                }
              >
                继续选择新账号
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Post-callback account switch confirmation modal (Step 2 409 state) */}
      {pendingSwitch && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="确认切换账号"
          data-testid="confirm-account-switch-dialog"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
        >
          <div className="w-full max-w-md rounded-xl border border-(--color-border-muted) bg-(--color-bg-card) p-5 shadow-lg space-y-4">
            <h3 className="text-base font-medium text-(--color-text-primary)">
              确认切换为新的 Google 账号
            </h3>
            <div className="rounded-lg border border-(--color-border-muted) p-3 text-xs space-y-1 text-(--color-text-secondary)">
              {pendingSwitch.previousEmail && (
                <div>原账号：{pendingSwitch.previousEmail}</div>
              )}
              {pendingSwitch.newEmail && <div>新账号：{pendingSwitch.newEmail}</div>}
            </div>
            <p className="text-sm text-(--color-text-secondary)">
              确认后将断开原账号、清除原账号在对话中留下的个人数据，并立即启用新账号连接。
            </p>
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={confirmSwitchMutation.isPending}
                onClick={() =>
                  confirmSwitchMutation.mutate({
                    token: pendingSwitch.confirmationToken,
                    confirmAccountSwitch: false,
                    cancel: true,
                  })
                }
              >
                取消
              </Button>
              <Button
                type="button"
                size="sm"
                disabled={confirmSwitchMutation.isPending}
                onClick={() =>
                  confirmSwitchMutation.mutate({
                    token: pendingSwitch.confirmationToken,
                    confirmAccountSwitch: true,
                    cancel: false,
                  })
                }
              >
                确认换账号
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Disconnect modal (Google sole vs sibling vs Feishu) */}
      {disconnectDialog && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="断开个人连接"
          data-testid="disconnect-integration-dialog"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
        >
          <div className="w-full max-w-md rounded-xl border border-(--color-border-muted) bg-(--color-bg-card) p-5 shadow-lg space-y-4">
            <h3 className="text-base font-medium text-(--color-text-primary)">
              断开{' '}
              {
                SERVICE_META[
                  serviceKey(
                    disconnectDialog.connection.provider,
                    disconnectDialog.connection.service,
                  )
                ].title
              }
            </h3>
            <p className="text-sm text-(--color-text-secondary)">
              断开后会立即清除该连接在私密对话中留下的个人数据，清理完成后原对话可继续使用。
            </p>

            {disconnectDialog.connection.provider === 'google' &&
              disconnectDialog.hasGoogleSibling && (
                <div className="space-y-2 text-xs text-(--color-text-secondary)">
                  <p>
                    同一 Google 账号同时连接了另一项服务。仅断开当前服务时，保留服务的刷新令牌仍带原并集权限（服务端会按当前连接行隔离工具访问），也可对保留服务重新发起单服务授权以收窄权限。
                  </p>
                  <div className="flex flex-col gap-2 pt-1">
                    <Button
                      type="button"
                      size="sm"
                      disabled={disconnectServiceMutation.isPending}
                      onClick={() =>
                        disconnectServiceMutation.mutate({
                          connection: disconnectDialog.connection,
                          mode: 'local_only',
                        })
                      }
                    >
                      仅断开此项服务（保留另一服务可用）
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={disconnectAllGoogleMutation.isPending}
                      onClick={() => disconnectAllGoogleMutation.mutate()}
                    >
                      断开全部 Google 服务并在 Google 撤销授权
                    </Button>
                  </div>
                </div>
              )}

            {disconnectDialog.connection.provider === 'google' &&
              !disconnectDialog.hasGoogleSibling && (
                <div className="space-y-2">
                  <div className="flex flex-col gap-2">
                    <Button
                      type="button"
                      size="sm"
                      disabled={disconnectServiceMutation.isPending}
                      onClick={() =>
                        disconnectServiceMutation.mutate({
                          connection: disconnectDialog.connection,
                          mode: 'remote_if_sole',
                        })
                      }
                    >
                      断开并在 Google 撤销授权（推荐）
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={disconnectServiceMutation.isPending}
                      onClick={() =>
                        disconnectServiceMutation.mutate({
                          connection: disconnectDialog.connection,
                          mode: 'local_only',
                        })
                      }
                    >
                      仅在本地断开
                    </Button>
                  </div>
                  <a
                    href="https://myaccount.google.com/permissions"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs underline text-(--color-text-tertiary)"
                  >
                    前往 Google 账号权限页管理授权
                    <ExternalLink className="h-3 w-3" />
                  </a>
                </div>
              )}

            {disconnectDialog.connection.provider === 'feishu' && (
              <div className="space-y-3">
                <p className="text-xs text-(--color-text-tertiary)">
                  确认后将立即销毁本地令牌并清理关联对话内容；已签发的短期访问令牌最长在 7,200 秒内自然失效，可按需在飞书客户端移除应用授权。
                </p>
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    size="sm"
                    disabled={disconnectServiceMutation.isPending}
                    onClick={() =>
                      disconnectServiceMutation.mutate({
                        connection: disconnectDialog.connection,
                        mode: 'local_only',
                      })
                    }
                  >
                    确认断开
                  </Button>
                </div>
              </div>
            )}

            <div className="flex justify-end">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setDisconnectDialog(null)}
              >
                取消
              </Button>
            </div>
          </div>
        </div>
      )}
    </DaltonSettingsCard>
  );
}
