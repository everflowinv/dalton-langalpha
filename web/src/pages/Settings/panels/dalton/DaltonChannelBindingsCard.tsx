import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
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
  type DaltonChannelBindCodeCreateResponse,
  type DaltonChannelBindingState,
  type DaltonChannelBindingsResponse,
  type DaltonChannelPlatform,
} from '@/pages/Settings/utils/daltonApi';
import { SettingRow, SettingsCardBody, SettingsGroup } from '../../components/SettingsLayout';
import { useSettingCopy } from '../../components/useSettingCopy';
import { SETTINGS } from '../../sections';

const BIND_PLATFORMS = ['feishu', 'discord'] as const;
type PersonalBindPlatform = (typeof BIND_PLATFORMS)[number];

/** The server keeps a code for 10 minutes. Polling stops at that cap, on expiry, on link, or on unmount. */
export const CHANNEL_BINDINGS_POLL_MS = 4_000;
export const CHANNEL_BINDINGS_MAX_WAIT_MS = 10 * 60 * 1000;
const EXPIRY_CLOCK_MS = 1_000;

interface IssuedBindCode {
  platform: PersonalBindPlatform;
  command: string;
  expiresAtMs: number;
}

function isPersonalPlatform(platform: DaltonChannelPlatform): platform is PersonalBindPlatform {
  return platform === 'feishu' || platform === 'discord';
}

/** Keep the server command. Cap the wait so a far-future expiry cannot poll indefinitely. */
export function issuedBindCode(
  res: DaltonChannelBindCodeCreateResponse,
  requested: PersonalBindPlatform,
  nowMs: number,
): IssuedBindCode | null {
  if (!isPersonalPlatform(res.platform) || res.platform !== requested) return null;
  const command = res.command.trim();
  if (!command || !res.code.trim()) return null;
  const parsed = Date.parse(res.expires_at);
  const fromSeconds = Number.isFinite(res.expires_in_seconds)
    ? nowMs + res.expires_in_seconds * 1000
    : nowMs + CHANNEL_BINDINGS_MAX_WAIT_MS;
  const serverExpiry = Number.isFinite(parsed) ? parsed : fromSeconds;
  return {
    platform: requested,
    command,
    expiresAtMs: Math.min(serverExpiry, nowMs + CHANNEL_BINDINGS_MAX_WAIT_MS),
  };
}

function bindingRow(
  data: DaltonChannelBindingsResponse,
  platform: PersonalBindPlatform,
): DaltonChannelBindingState | null {
  const row = data.bindings?.[platform];
  if (!row || typeof row.linked !== 'boolean') return null;
  return row;
}

function isLive(code: IssuedBindCode | undefined, nowMs: number): code is IssuedBindCode {
  return code !== undefined && nowMs < code.expiresAtMs;
}

function readableGenerateError(err: unknown, fallback: string): string {
  const detail = formatApiErrorDetail(err);
  if (!detail || detail === 'Request failed' || detail.startsWith('Request failed with status code')) {
    return fallback;
  }
  return `${fallback} ${detail}`;
}

export function DaltonChannelBindingsCard(): React.ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const copy = useSettingCopy();
  const heading = copy(SETTINGS.daltonChannelBindings);
  const queryClient = useQueryClient();
  const [codes, setCodes] = useState<Partial<Record<PersonalBindPlatform, IssuedBindCode>>>({});
  const [pending, setPending] = useState<Partial<Record<PersonalBindPlatform, boolean>>>({});
  const [actionError, setActionError] = useState<Partial<Record<PersonalBindPlatform, string | null>>>({});
  const [copyState, setCopyState] = useState<Partial<Record<PersonalBindPlatform, 'copied' | 'failed' | null>>>({});
  const inflight = useRef<Partial<Record<PersonalBindPlatform, boolean>>>({});

  const now = useNow(EXPIRY_CLOCK_MS, (tick) => (
    BIND_PLATFORMS.some((platform) => isLive(codes[platform], tick))
  ));
  const hasLiveCode = BIND_PLATFORMS.some((platform) => isLive(codes[platform], now));

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    staleTime: 30_000,
    retry: false,
    refetchOnWindowFocus: 'always',
    refetchInterval: hasLiveCode ? CHANNEL_BINDINGS_POLL_MS : false,
    refetchIntervalInBackground: false,
  });

  useEffect(() => {
    const data = bindingsQuery.data;
    if (!data) return;
    setCodes((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const platform of BIND_PLATFORMS) {
        if (next[platform] && bindingRow(data, platform)?.linked === true) {
          delete next[platform];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [bindingsQuery.data]);

  const platformLabel = (platform: PersonalBindPlatform): string => (
    t(platform === 'feishu' ? 'settings.dalton.channelPlatformFeishu' : 'settings.dalton.channelPlatformDiscord')
  );

  const createCode = useMutation({
    mutationFn: (platform: PersonalBindPlatform) => createDaltonChannelBindCode(platform),
    onSuccess: (res, platform) => {
      const issued = issuedBindCode(res, platform, Date.now());
      if (!issued) {
        setActionError((prev) => ({
          ...prev,
          [platform]: t('settings.dalton.channelBindingsGenerateError', { platform: platformLabel(platform) }),
        }));
        return;
      }
      setCodes((prev) => ({ ...prev, [platform]: issued }));
      setActionError((prev) => ({ ...prev, [platform]: null }));
      setCopyState((prev) => ({ ...prev, [platform]: null }));
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
    },
    onError: (err, platform) => {
      setActionError((prev) => ({
        ...prev,
        [platform]: readableGenerateError(
          err,
          t('settings.dalton.channelBindingsGenerateError', { platform: platformLabel(platform) }),
        ),
      }));
    },
    // Each outstanding platform request clears its own pending state.
    onSettled: (_data, _error, platform) => {
      inflight.current[platform] = false;
      setPending((prev) => ({ ...prev, [platform]: false }));
    },
  });

  const requestCode = (platform: PersonalBindPlatform) => {
    if (inflight.current[platform]) return;
    inflight.current[platform] = true;
    setPending((prev) => ({ ...prev, [platform]: true }));
    setActionError((prev) => ({ ...prev, [platform]: null }));
    // The server revokes the previous code when it accepts this request, even if the new response is lost.
    setCodes((prev) => {
      if (!prev[platform]) return prev;
      const next = { ...prev };
      delete next[platform];
      return next;
    });
    setCopyState((prev) => ({ ...prev, [platform]: null }));
    createCode.mutate(platform);
  };

  const copyCommand = (platform: PersonalBindPlatform, command: string) => {
    void copyText(command).then(() => {
      setCopyState((prev) => ({ ...prev, [platform]: 'copied' }));
    }, () => {
      setCopyState((prev) => ({ ...prev, [platform]: 'failed' }));
    });
  };

  const loadStatus = apiErrorStatus(bindingsQuery.error);
  const loadFailed = bindingsQuery.isError && !bindingsQuery.data;
  const refreshFailed = bindingsQuery.isError && Boolean(bindingsQuery.data);
  const loadMessage = loadStatus === 401 || loadStatus === 403
    ? t('settings.dalton.channelBindingsForbidden')
    : t('settings.dalton.channelBindingsLoadError');

  return (
    <SettingsGroup
      id={heading.id}
      title={heading.label}
      description={heading.description}
      action={bindingsQuery.isPending ? undefined : (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="tap-target h-auto min-h-9 max-w-full whitespace-normal"
          disabled={bindingsQuery.isFetching}
          onClick={() => { void bindingsQuery.refetch(); }}
        >
          {t('settings.dalton.channelBindingsRefresh')}
        </Button>
      )}
    >
      {bindingsQuery.isPending && (
        <SettingsCardBody>
          <p className="m-0 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('settings.dalton.channelBindingsLoading')}
          </p>
        </SettingsCardBody>
      )}
      {loadFailed && (
        <SettingsCardBody>
          <p role="alert" className="m-0 text-xs" style={{ color: 'var(--color-loss)' }}>
            {loadMessage}
          </p>
          <Button
            type="button"
            size="sm"
            className="tap-target mt-2 h-auto min-h-9 max-w-full whitespace-normal"
            onClick={() => { void bindingsQuery.refetch(); }}
          >
            {t('settings.dalton.channelBindingsRetry')}
          </Button>
        </SettingsCardBody>
      )}
      {refreshFailed && (
        <SettingsCardBody>
          <p role="alert" className="m-0 text-xs" style={{ color: 'var(--color-loss)' }}>
            {t('settings.dalton.channelBindingsRefreshError')}
          </p>
        </SettingsCardBody>
      )}
      {bindingsQuery.data && BIND_PLATFORMS.map((platform) => {
        const data = bindingsQuery.data;
        if (!data) return null;
        const label = platformLabel(platform);
        const row = bindingRow(data, platform);
        const issued = codes[platform];
        const linked = row?.linked === true;
        const liveCode = !linked && isLive(issued, now) ? issued : null;
        const expired = !linked && issued !== undefined && liveCode === null;
        const busy = pending[platform] === true;
        const name = row?.platform_username?.trim() || '';
        const accountId = row?.platform_user_id?.trim() || '';
        const status = row === null
          ? t('settings.dalton.channelBindingsUnavailable')
          : row.linked
            ? t('settings.dalton.channelBindingsLinked')
            : t('settings.dalton.channelBindingsNotLinked');
        const detail = [
          status,
          row?.linked && name ? t('settings.dalton.channelBindingsAccountName', { name }) : '',
          row?.linked && accountId ? t('settings.dalton.channelBindingsAccountId', { id: accountId }) : '',
        ].filter(Boolean).join(' · ');
        return (
          <div key={platform} data-testid={`channel-bind-${platform}`} className="min-w-0 max-w-full">
            <SettingRow label={label} description={detail}>
              {row && !row.linked && (
                <Button
                  type="button"
                  size="sm"
                  className="tap-target h-auto min-h-9 max-w-full whitespace-normal"
                  disabled={busy}
                  onClick={() => requestCode(platform)}
                >
                  {busy
                    ? t('settings.dalton.channelBindingsGenerating', { platform: label })
                    : t('settings.dalton.channelBindingsGenerate', { platform: label })}
                </Button>
              )}
            </SettingRow>
            {liveCode && (
              <SettingsCardBody>
                <p
                  className="m-0 break-all font-mono text-[0.8125rem] leading-5"
                  style={{ color: 'var(--color-text-primary)' }}
                  data-testid={`channel-bind-command-${platform}`}
                >
                  {liveCode.command}
                </p>
                <p className="mb-0 mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings.dalton.channelBindingsExpires', {
                    time: formatTimestamp(new Date(liveCode.expiresAtMs).toISOString(), locale),
                  })}
                </p>
                <p className="mb-0 mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings.dalton.channelBindingsDmHint')}
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="tap-target mt-2 h-auto min-h-9 max-w-full whitespace-normal"
                  onClick={() => copyCommand(platform, liveCode.command)}
                >
                  {t('settings.dalton.channelBindingsCopy', { platform: label })}
                </Button>
                <p className="mb-0 mt-1 text-xs" aria-live="polite" style={{ color: copyState[platform] === 'failed' ? 'var(--color-loss)' : 'var(--color-text-tertiary)' }}>
                  {copyState[platform] === 'copied' && t('settings.dalton.channelBindingsCopied', { platform: label })}
                  {copyState[platform] === 'failed' && t('settings.dalton.channelBindingsCopyFailed', { platform: label })}
                </p>
              </SettingsCardBody>
            )}
            {expired && (
              <SettingsCardBody>
                <p role="status" className="m-0 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings.dalton.channelBindingsExpired', { platform: label })}
                </p>
              </SettingsCardBody>
            )}
            {actionError[platform] && (
              <SettingsCardBody>
                <p role="alert" className="m-0 text-xs" style={{ color: 'var(--color-loss)' }}>
                  {actionError[platform]}
                </p>
              </SettingsCardBody>
            )}
          </div>
        );
      })}
    </SettingsGroup>
  );
}
