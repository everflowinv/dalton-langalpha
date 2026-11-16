import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Check, Copy, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/hooks/useLocale';
import { useNow } from '@/hooks/useNow';
import { apiErrorStatus, formatApiErrorDetail } from '@/api/errors';
import { copyText } from '@/lib/clipboard';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  createDaltonChannelBindCode,
  fetchDaltonChannelBindings,
  type DaltonChannelBindingState,
  type DaltonChannelBindCodeCreateResponse,
  type DaltonChannelPlatform,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from './primitives';

type BindPlatform = Extract<DaltonChannelPlatform, 'feishu' | 'discord'>;

const PLATFORMS: BindPlatform[] = ['feishu', 'discord'];

export const CHANNEL_BINDINGS_POLL_MS = 5000;
export const CHANNEL_BINDINGS_MAX_WAIT_MS = 5 * 60 * 1000;

export interface IssuedBindCodeState {
  code: string;
  platform: BindPlatform;
  expiresAt: string;
  expiresAtMs: number;
  command: string;
}

export function issuedBindCode(
  platform: BindPlatform,
  res: DaltonChannelBindCodeCreateResponse,
  nowMs = Date.now(),
): IssuedBindCodeState {
  const parsed = res.expires_at ? Date.parse(res.expires_at) : NaN;
  const fallbackMs =
    nowMs +
    Math.min(
      Math.max((res.expires_in_seconds ?? 300) * 1000, 0),
      CHANNEL_BINDINGS_MAX_WAIT_MS,
    );
  const cappedMs = Number.isFinite(parsed)
    ? Math.min(parsed, nowMs + CHANNEL_BINDINGS_MAX_WAIT_MS)
    : fallbackMs;

  return {
    code: res.code,
    platform,
    expiresAt: res.expires_at,
    expiresAtMs: cappedMs,
    command: res.command,
  };
}

/** Personal Feishu and Discord account bindings built with SettingsGroup + Dalton primitives. */
export function DaltonChannelBindingsCard() {
  const { t } = useTranslation();
  const locale = useLocale();
  const queryClient = useQueryClient();

  const [codes, setCodes] = useState<Partial<Record<BindPlatform, IssuedBindCodeState>>>({});
  const [pendingPlatforms, setPendingPlatforms] = useState<Partial<Record<BindPlatform, boolean>>>({});
  const [generateError, setGenerateError] = useState<Partial<Record<BindPlatform, string>>>({});
  const [copiedPlatform, setCopiedPlatform] = useState<BindPlatform | null>(null);
  const [copyError, setCopyError] = useState<Partial<Record<BindPlatform, string>>>({});
  const requestSeqRef = useRef<Record<BindPlatform, number>>({ feishu: 0, discord: 0 });
  const inFlightRef = useRef<Record<BindPlatform, boolean>>({ feishu: false, discord: false });

  const hasActiveCode = Object.values(codes).some((c) => Boolean(c));
  const now = useNow(CHANNEL_BINDINGS_POLL_MS, hasActiveCode);

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    retry: false,
  });

  // Poll while an issued code is active and unlinked; refetch on window focus
  useEffect(() => {
    const activeEntries = (
      Object.entries(codes) as Array<[BindPlatform, IssuedBindCodeState | undefined]>
    ).filter(([platform, code]) => {
      if (!code) return false;
      const linked = bindingsQuery.data?.bindings?.[platform]?.linked === true;
      return !linked && code.expiresAtMs > Date.now();
    });
    if (activeEntries.length === 0) return;

    const timer = window.setInterval(() => {
      void bindingsQuery.refetch();
    }, CHANNEL_BINDINGS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [codes, bindingsQuery]);

  // Clear any issued code once its platform reports linked
  useEffect(() => {
    const b = bindingsQuery.data?.bindings;
    if (!b) return;
    setCodes((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const platform of PLATFORMS) {
        if (next[platform] && b[platform]?.linked === true) {
          delete next[platform];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [bindingsQuery.data?.bindings]);

  useEffect(() => {
    const onFocus = () => {
      void bindingsQuery.refetch();
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [bindingsQuery]);

  useEffect(() => {
    if (!copiedPlatform) return;
    const timer = window.setTimeout(() => setCopiedPlatform(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copiedPlatform]);

  const handleGenerate = async (platform: BindPlatform) => {
    if (inFlightRef.current[platform]) return;
    inFlightRef.current[platform] = true;
    const seq = ++requestSeqRef.current[platform];

    setPendingPlatforms((prev) => ({ ...prev, [platform]: true }));
    setCodes((prev) => {
      const next = { ...prev };
      delete next[platform];
      return next;
    });
    setGenerateError((prev) => {
      const next = { ...prev };
      delete next[platform];
      return next;
    });
    setCopyError((prev) => {
      const next = { ...prev };
      delete next[platform];
      return next;
    });

    const label =
      platform === 'feishu'
        ? t('settings.dalton.channelPlatformFeishu')
        : t('settings.dalton.channelPlatformDiscord');

    try {
      const created = await createDaltonChannelBindCode(platform);
      if (requestSeqRef.current[platform] !== seq) return;
      if (created.platform && created.platform !== platform) {
        setGenerateError((prev) => ({
          ...prev,
          [platform]: t('settings.dalton.channelBindingsGenerateError', { platform: label }),
        }));
        return;
      }
      setCodes((prev) => ({ ...prev, [platform]: issuedBindCode(platform, created) }));
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
    } catch (err) {
      if (requestSeqRef.current[platform] !== seq) return;
      const detail = formatApiErrorDetail(err);
      setGenerateError((prev) => ({
        ...prev,
        [platform]:
          detail || t('settings.dalton.channelBindingsGenerateError', { platform: label }),
      }));
    } finally {
      inFlightRef.current[platform] = false;
      if (requestSeqRef.current[platform] === seq) {
        setPendingPlatforms((prev) => ({ ...prev, [platform]: false }));
      }
    }
  };

  const handleCopy = async (platform: BindPlatform, command: string) => {
    const label =
      platform === 'feishu'
        ? t('settings.dalton.channelPlatformFeishu')
        : t('settings.dalton.channelPlatformDiscord');
    try {
      await copyText(command);
      setCopiedPlatform(platform);
      setCopyError((prev) => {
        const next = { ...prev };
        delete next[platform];
        return next;
      });
    } catch {
      setCopyError((prev) => ({
        ...prev,
        [platform]: t('settings.dalton.channelBindingsCopyFailed', { platform: label }),
      }));
    }
  };

  const bindings = bindingsQuery.data?.bindings;
  const loadStatus = apiErrorStatus(bindingsQuery.error);
  const loadForbidden = loadStatus === 401 || loadStatus === 403;

  return (
    <DaltonSettingsCard
      id="dalton-channel-bindings"
      testId="dalton-channel-bindings-card"
      title={t('settings.dalton.channelBindingsLabel')}
      description={t('settings.dalton.channelBindingsDesc')}
      layout="rows"
      action={
        <DaltonRefreshButton
          busy={bindingsQuery.isFetching}
          label={t('settings.dalton.channelBindingsRefresh')}
          onClick={() => void bindingsQuery.refetch()}
          testId="dalton-channel-bindings-refresh"
        />
      }
      below={
        bindingsQuery.isError && bindings ? (
          <DaltonInlineNotice tone="warning" className="mt-2">
            {t('settings.dalton.channelBindingsRefreshError')}
          </DaltonInlineNotice>
        ) : undefined
      }
    >
      {bindingsQuery.isLoading ? (
        <div className="p-4">
          <DaltonLoadingRow label={t('settings.dalton.channelBindingsLoading')} />
        </div>
      ) : bindingsQuery.isError && !bindings ? (
        <div className="p-4">
          <DaltonInlineNotice
            tone={loadForbidden ? 'warning' : 'error'}
            action={
              !loadForbidden ? (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 gap-1.5 text-xs"
                  onClick={() => void bindingsQuery.refetch()}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  <span>{t('settings.dalton.channelBindingsRetry')}</span>
                </Button>
              ) : undefined
            }
          >
            {loadForbidden
              ? t('settings.dalton.channelBindingsForbidden')
              : t('settings.dalton.channelBindingsLoadError')}
          </DaltonInlineNotice>
        </div>
      ) : (
        PLATFORMS.map((platform) => {
          const label =
            platform === 'feishu'
              ? t('settings.dalton.channelPlatformFeishu')
              : t('settings.dalton.channelPlatformDiscord');
          const state: DaltonChannelBindingState | undefined = bindings?.[platform];
          const knownLinked = typeof state?.linked === 'boolean' ? state.linked : null;
          const unavailable = knownLinked === null;
          const linked = knownLinked === true;
          const generated = codes[platform];
          const isExpired = Boolean(generated && generated.expiresAtMs <= now);
          const isGenerating = pendingPlatforms[platform] === true;
          const isCopied = copiedPlatform === platform;
          const accountName = state?.platform_username?.trim() || null;
          const accountId = state?.platform_user_id?.trim() || null;

          return (
            <div key={platform} className="p-4 space-y-3" data-testid={`dalton-channel-row-${platform}`}>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0 space-y-1">
                  <div className="flex items-center gap-2">
                    <span
                      className="text-sm font-medium"
                      style={{ color: 'var(--color-text-primary)' }}
                    >
                      {label}
                    </span>
                    <DaltonStatusBadge
                      dot
                      tone={unavailable ? 'neutral' : linked ? 'success' : 'neutral'}
                      label={
                        unavailable
                          ? t('settings.dalton.channelBindingsUnavailable')
                          : linked
                            ? t('settings.dalton.channelBindingsLinked')
                            : t('settings.dalton.channelBindingsNotLinked')
                      }
                    />
                  </div>
                  {linked && (accountName || accountId) && (
                    <div
                      className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {accountName && (
                        <span>
                          {t('settings.dalton.channelBindingsAccountName', {
                            name: accountName,
                          })}
                        </span>
                      )}
                      {accountId && (
                        <span className="font-mono text-[11px]">
                          {t('settings.dalton.channelBindingsAccountId', {
                            id: accountId,
                          })}
                        </span>
                      )}
                    </div>
                  )}
                </div>

                {!unavailable && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8 text-xs"
                    disabled={isGenerating}
                    onClick={() => void handleGenerate(platform)}
                    data-testid={`dalton-channel-generate-${platform}`}
                  >
                    {isGenerating
                      ? t('settings.dalton.channelBindingsGenerating', { platform: label })
                      : t('settings.dalton.channelBindingsGenerate', { platform: label })}
                  </Button>
                )}
              </div>

              {generateError[platform] && (
                <DaltonInlineNotice tone="error">{generateError[platform]}</DaltonInlineNotice>
              )}

              {generated && (
                <div
                  className="rounded-md border px-3 py-2.5 space-y-2"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    backgroundColor: 'var(--color-bg-elevated)',
                  }}
                >
                  {isExpired ? (
                    <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {t('settings.dalton.channelBindingsExpired', { platform: label })}
                    </div>
                  ) : (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <code
                          className="text-xs font-mono select-all break-all"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          {generated.command}
                        </code>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-8 gap-1.5 text-xs shrink-0"
                          onClick={() => void handleCopy(platform, generated.command)}
                          data-testid={`dalton-channel-copy-${platform}`}
                        >
                          {isCopied ? (
                            <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} />
                          ) : (
                            <Copy className="h-3.5 w-3.5" />
                          )}
                          <span>
                            {isCopied
                              ? t('settings.dalton.channelBindingsCopied', { platform: label })
                              : t('settings.dalton.channelBindingsCopy', { platform: label })}
                          </span>
                        </Button>
                      </div>
                      {copyError[platform] && (
                        <DaltonInlineNotice tone="warning">{copyError[platform]}</DaltonInlineNotice>
                      )}
                      <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                        {t('settings.dalton.channelBindingsExpires', {
                          time: formatTimestamp(generated.expiresAt, locale),
                        })}
                      </div>
                      <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings.dalton.channelBindingsDmHint')}
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })
      )}
    </DaltonSettingsCard>
  );
}
