import { useState } from 'react';
import { Link } from 'react-router';
import { ChevronDown, ChevronRight, Wifi } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useLocale } from '@/hooks/useLocale';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { formatTimestamp } from '@/lib/format';
import type { DaltonProviderStatusItem } from '@/pages/Settings/utils/daltonApi';
import { useProviderProbe } from '../../hooks/useProviderStatus';
import {
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonStatusBadge,
  type DaltonStatusTone,
} from '@/components/dalton/primitives';

const ROLE_LABELS: Record<string, string> = {
  lead: 'dalton.providers.roles.lead',
  background: 'dalton.providers.roles.background',
  assist: 'dalton.providers.roles.assist',
  audit: 'dalton.providers.roles.audit',
  reviewer: 'dalton.providers.roles.reviewer',
};

function billingLabel(
  billing: DaltonProviderStatusItem['billing'],
  t: (key: string) => string,
): string {
  return billing === 'subscription'
    ? t('dalton.providers.subscriptionBadge')
    : t('dalton.providers.cashBadge');
}

function deriveProviderState(item: DaltonProviderStatusItem): {
  tone: DaltonStatusTone;
  labelKey: string;
} {
  if (!item.enabled || !item.configured || item.credential.token_expired) {
    return { tone: 'neutral', labelKey: 'dalton.providers.unconfiguredBadge' };
  }
  if (item.errors_24h > 0 || item.warnings.length > 0 || item.credential.reconnect_required) {
    return { tone: 'warning', labelKey: 'dalton.providers.degradedBadge' };
  }
  return { tone: 'success', labelKey: 'dalton.providers.configuredBadge' };
}

function formatResetTimestamp(
  raw: string | number | null | undefined,
  locale: string,
): string | null {
  if (raw == null) return null;
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    const ms = raw < 1e12 ? raw * 1000 : raw;
    return formatTimestamp(new Date(ms).toISOString(), locale);
  }
  const str = String(raw).trim();
  if (!str) return null;
  if (/^\d+(\.\d+)?$/.test(str)) {
    const n = Number(str);
    const ms = n < 1e12 ? n * 1000 : n;
    return formatTimestamp(new Date(ms).toISOString(), locale);
  }
  return formatTimestamp(str, locale);
}

export function ProviderRow({
  item,
  isAdmin = false,
}: {
  item: DaltonProviderStatusItem;
  isAdmin?: boolean;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const { isShared } = useTeamCredentials();
  const probe = useProviderProbe();
  const [expanded, setExpanded] = useState(false);

  const state = deriveProviderState(item);
  const subsUsage = item.subs_usage ?? item.credential.subs_usage;
  const quota = item.quota ?? item.credential.quota;
  const isProbing = probe.isPending && probe.variables?.provider === item.provider;
  const probeResult =
    probe.data?.provider === item.provider ? probe.data : null;
  const probeErrorMessage =
    probe.isError && probe.variables?.provider === item.provider
      ? probe.error instanceof Error && probe.error.message
        ? probe.error.message
        : t('dalton.common.loadFailed')
      : null;
  // Team-shared mode manages team subscriptions on Team Management -> Models & Providers (#team-model-access).
  const bindAccountPath =
    isDaltonAuth && isShared
      ? '/settings?tab=teamModels#team-model-access'
      : '/settings?tab=model#model-access';

  const hasDetail =
    item.recent_errors.length > 0 ||
    item.warnings.length > 0 ||
    Boolean(item.credential.status_message) ||
    Boolean(item.credential.hint);

  return (
    <DaltonItemRow
      testId={`dalton-provider-row-${item.provider}`}
      title={
        <span className="flex flex-wrap items-center gap-2">
          <span>{item.display_name || item.provider}</span>
          <DaltonStatusBadge tone={state.tone} dot label={t(state.labelKey)} />
          <DaltonStatusBadge
            tone={item.billing === 'subscription' ? 'accent' : 'neutral'}
            label={billingLabel(item.billing, t)}
          />
          {item.tos_risk === 'high' && (
            <DaltonStatusBadge
              tone="warning"
              label={t('dalton.providers.tosRiskBadge')}
            />
          )}
        </span>
      }
      subtitle={
        <div className="space-y-1">
          {item.credential.status_text && <div>{item.credential.status_text}</div>}
          {(subsUsage?.window?.used_percent != null || subsUsage?.weekly?.used_percent != null) && (
            <div className="tabular-nums" data-testid="dalton-muse-subs-usage">
              {subsUsage?.window?.used_percent != null && (
                <span>
                  5h: {Math.round(subsUsage.window.used_percent)}%
                  {formatResetTimestamp(subsUsage.window.resets_at, locale)
                    ? ` (${formatResetTimestamp(subsUsage.window.resets_at, locale)})`
                    : ''}
                </span>
              )}
              {subsUsage?.window?.used_percent != null && subsUsage?.weekly?.used_percent != null && ' · '}
              {subsUsage?.weekly?.used_percent != null && (
                <span>
                  {t('dalton.providers.modelColQuota')}: {Math.round(subsUsage.weekly.used_percent)}%
                  {formatResetTimestamp(subsUsage.weekly.resets_at, locale)
                    ? ` (${formatResetTimestamp(subsUsage.weekly.resets_at, locale)})`
                    : ''}
                </span>
              )}
            </div>
          )}
          {quota?.credit_usage_percent != null && (
            <div className="tabular-nums">
              {t('dalton.providers.modelColQuota')}: {Math.round(quota.credit_usage_percent)}%
            </div>
          )}
          {item.used_by_roles.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
              {item.used_by_roles.map((r) => (
                <DaltonStatusBadge
                  key={`${r.role}:${r.model}`}
                  tone="neutral"
                  label={`${ROLE_LABELS[r.role] ? t(ROLE_LABELS[r.role]) : r.role}: ${r.model}`}
                />
              ))}
            </div>
          )}
        </div>
      }
      meta={
        <div className="flex flex-wrap items-center gap-3 tabular-nums">
          <span>24h: {item.calls_24h}</span>
          {item.errors_24h > 0 && (
            <span style={{ color: 'var(--color-loss)' }}>
              24h err: {item.errors_24h}
            </span>
          )}
          {item.last_success?.at && (
            <span>{formatTimestamp(item.last_success.at, locale)}</span>
          )}
        </div>
      }
      trailing={
        <div className="flex flex-wrap items-center gap-1.5">
          {!item.configured && item.billing === 'subscription' && (
            <Link
              to={bindAccountPath}
              className="text-xs underline underline-offset-2 px-1.5"
              style={{ color: 'var(--color-accent-primary)' }}
              data-testid={`dalton-provider-connect-${item.provider}`}
            >
              {t('dalton.providers.goBindAccount')}
            </Link>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 gap-1 text-xs"
            disabled={!item.configured || isProbing}
            onClick={() => probe.mutate({ provider: item.provider })}
            data-testid={`dalton-provider-probe-${item.provider}`}
          >
            <Wifi className="h-3.5 w-3.5" />
            <span>
              {isProbing
                ? t('dalton.providers.probing')
                : t('dalton.providers.probeButton')}
            </span>
          </Button>
          {hasDetail && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 px-2 text-xs"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
            >
              {expanded ? (
                <ChevronDown className="h-3.5 w-3.5" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" />
              )}
            </Button>
          )}
        </div>
      }
    >
      {probeResult && (
        <DaltonInlineNotice tone={probeResult.ok ? 'success' : 'error'}>
          {probeResult.message} ({probeResult.latency_ms}ms)
        </DaltonInlineNotice>
      )}
      {probeErrorMessage && (
        <DaltonInlineNotice tone="error" testId={`dalton-provider-probe-error-${item.provider}`}>
          {t('dalton.providers.probeFailed', { error: probeErrorMessage })}
        </DaltonInlineNotice>
      )}

      {hasDetail && (
        <Disclosure open={expanded}>
          <div className="space-y-2 pt-1 text-xs">
            {item.credential.status_message && (
              <DaltonInlineNotice tone="info">
                {item.credential.status_message}
              </DaltonInlineNotice>
            )}
            {isAdmin && item.credential.hint && (
              <DaltonInlineNotice tone="warning">
                {item.credential.hint}
              </DaltonInlineNotice>
            )}
            {item.warnings.map((w, idx) => (
              <DaltonInlineNotice key={idx} tone="warning">
                {w.message}
              </DaltonInlineNotice>
            ))}
            {item.recent_errors.map((err, idx) => (
              <DaltonInlineNotice key={idx} tone="error">
                {err.at ? `${formatTimestamp(err.at, locale)} · ` : ''}
                {err.model ? `${err.model} · ` : ''}
                {err.message}
              </DaltonInlineNotice>
            ))}
          </div>
        </Disclosure>
      )}
    </DaltonItemRow>
  );
}
