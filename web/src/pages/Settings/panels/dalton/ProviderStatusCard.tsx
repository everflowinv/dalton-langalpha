import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  RefreshCw,
} from 'lucide-react';
import { useProviderStatus } from '../../hooks/useProviderStatus';
import { ProviderRow } from './ProviderRow';
import { OpenClawCard } from './OpenClawCard';

export function ProviderStatusCard() {
  const { t } = useTranslation();
  const statusQuery = useProviderStatus(true);
  const [brokerExpanded, setBrokerExpanded] = useState(false);

  const data = statusQuery.data;
  const providers = data?.providers ?? [];
  const roleWarnings = data?.role_warnings ?? [];
  const brokerEnabled = data?.broker?.enabled ?? true;
  const brokerHealth = data?.broker?.health;

  // Summarize active role models from used_by_roles
  const roleSummary: string[] = [];
  for (const p of providers) {
    for (const r of p.used_by_roles ?? []) {
      if (r.role === 'lead' || r.role === 'background' || r.role === 'assist') {
        roleSummary.push(`${r.role}: ${r.model}`);
      }
    }
  }

  const brokerDotColor =
    brokerHealth?.status === 'ok'
      ? 'var(--color-profit)'
      : brokerHealth?.status === 'degraded'
        ? 'var(--color-warning)'
        : 'var(--color-loss)';

  return (
    <div
      className="flex flex-col gap-3 p-4 rounded-lg"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-default)',
      }}
      data-testid="dalton-provider-status-card"
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          <span
            className="text-sm font-medium"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('dalton.providers.title', '模型供应商状态')}
          </span>
          {roleSummary.length > 0 && (
            <p
              className="text-xs font-mono mt-0.5"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {roleSummary.join(' · ')}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => void statusQuery.refetch()}
          className="flex items-center gap-1 text-xs underline underline-offset-2"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          <RefreshCw
            className={`h-3 w-3 ${statusQuery.isFetching ? 'animate-spin' : ''}`}
          />
          {t('dalton.providers.refresh', '刷新状态')}
        </button>
      </div>

      {/* Role warnings at top */}
      {roleWarnings.length > 0 && (
        <div
          className="rounded-md p-2.5 space-y-1 text-xs"
          style={{
            backgroundColor: 'var(--color-warning-soft)',
            color: 'var(--color-warning)',
          }}
          data-testid="dalton-provider-role-warnings"
        >
          {roleWarnings.map((w, idx) => (
            <div key={`${w.code}-${idx}`} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                <span>
                  [{w.role}] {w.message}
                </span>
              </span>
              {w.link && (
                <a href={w.link} className="underline shrink-0">
                  去设置
                </a>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Provider rows */}
      <div className="flex flex-col gap-2">
        {providers.map((item) => (
          <ProviderRow key={item.provider} item={item} />
        ))}
      </div>

      {/* Collapsible OpenClaw broker parallel path section */}
      {brokerEnabled && (
        <div
          className="pt-2.5 mt-1"
          style={{ borderTop: '1px solid var(--color-border-muted)' }}
        >
          <button
            type="button"
            onClick={() => setBrokerExpanded((v) => !v)}
            className="flex items-center gap-2 text-xs font-medium w-full text-left"
            style={{ color: 'var(--color-text-secondary)' }}
            data-testid="dalton-broker-section-toggle"
          >
            {brokerExpanded ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: brokerDotColor }}
            />
            <span>
              {t('dalton.providers.brokerSectionTitle', 'OpenClaw broker（并行路径）')}
            </span>
          </button>
          {brokerExpanded && (
            <div className="mt-2.5">
              <OpenClawCard embedded />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
