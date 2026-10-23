import React, { useState } from 'react';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Loader2,
  Play,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import type {
  DaltonProviderProbeResponse,
  DaltonProviderStatusItem,
} from '../../utils/daltonApi';
import { useProviderProbe } from '../../hooks/useProviderStatus';

export function ProviderRow({ item }: { item: DaltonProviderStatusItem }) {
  const probeMutation = useProviderProbe();
  const [errorsExpanded, setErrorsExpanded] = useState(false);
  const [probeResult, setProbeResult] = useState<DaltonProviderProbeResponse | null>(null);

  const usedByRoles = item.used_by_roles ?? [];
  const hasRoles = usedByRoles.length > 0;
  const secondsLeft = item.credential?.seconds_left;
  const refreshFailed = item.credential?.last_refresh?.ok === false;
  const expiringSoon =
    typeof secondsLeft === 'number' && secondsLeft < 1800 && refreshFailed;

  // Status dot color logic per P0N.9 §2
  let dotColor = 'var(--color-text-tertiary)';
  let statusTitle = '已关闭';
  if (!item.enabled) {
    if (hasRoles) {
      dotColor = 'var(--color-loss)';
      statusTitle = '已关闭但仍被角色引用';
    } else {
      dotColor = 'var(--color-text-tertiary)';
      statusTitle = '已关闭';
    }
  } else if (!item.configured) {
    if (hasRoles) {
      dotColor = 'var(--color-loss)';
      statusTitle = '未配置凭据（被角色引用）';
    } else {
      dotColor = 'var(--color-text-tertiary)';
      statusTitle = '未配置凭据';
    }
  } else if (probeResult && !probeResult.ok) {
    dotColor = 'var(--color-loss)';
    statusTitle = '最近一次探测失败';
  } else if (expiringSoon || item.errors_24h > 0 || (item.warnings?.length ?? 0) > 0) {
    dotColor = 'var(--color-warning)';
    statusTitle = '凭据即将到期或最近 24h 有告警/错误';
  } else {
    dotColor = 'var(--color-profit)';
    statusTitle = '已配置且正常';
  }

  const handleProbe = async () => {
    setProbeResult(null);
    try {
      const res = await probeMutation.mutateAsync({
        provider: item.provider,
        model: item.default_probe_model ?? null,
      });
      setProbeResult(res);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setProbeResult({
        ok: false,
        provider: item.provider,
        model: item.default_probe_model,
        latency_ms: 0,
        error_kind: 'network_error',
        message: msg,
      });
    }
  };

  const formatSecondsLeft = (sec: number | null | undefined) => {
    if (sec == null) return null;
    if (sec <= 0) return '已到期（调用时自动刷新）';
    const mins = Math.round(sec / 60);
    if (mins < 60) return `余 ${mins}m`;
    const hours = (mins / 60).toFixed(1);
    return `余 ${hours}h`;
  };

  const formatLocalResetTime = (raw: string | number | null | undefined): string | null => {
    if (raw == null || raw === '') return null;
    let d: Date;
    if (typeof raw === 'number') {
      d = new Date(raw < 1e12 ? raw * 1000 : raw);
    } else if (/^\d+$/.test(String(raw).trim())) {
      const n = Number(String(raw).trim());
      d = new Date(n < 1e12 ? n * 1000 : n);
    } else {
      d = new Date(raw);
    }
    if (Number.isNaN(d.getTime())) return String(raw);
    return d.toLocaleString();
  };

  const isMuse = item.provider === 'muse-oauth';
  const museSubsActive = item.is_subs_active ?? item.credential?.is_subs_active;
  const museTierName = item.subs_tier_name ?? item.credential?.subs_tier_name;
  const museUsage = item.subs_usage ?? item.credential?.subs_usage;
  const museWindow = museUsage?.window;
  const museWeekly = museUsage?.weekly;
  const museWindowReset = formatLocalResetTime(museWindow?.resets_at);
  const museWeeklyReset = formatLocalResetTime(museWeekly?.resets_at);

  const showMuseSubsStatus =
    isMuse && (typeof museSubsActive === 'boolean' || Boolean(museTierName));
  const showMuseWindow =
    isMuse && museWindow?.used_percent != null && museWindowReset != null;
  const showMuseWeekly =
    isMuse && museWeekly?.used_percent != null && museWeeklyReset != null;

  return (
    <div
      className="rounded-md px-3 py-2.5 flex flex-col gap-1.5"
      style={{
        backgroundColor: 'var(--color-bg-subtle)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid={`dalton-provider-row-${item.provider}`}
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className="inline-block h-2.5 w-2.5 rounded-full shrink-0"
            style={{ backgroundColor: dotColor }}
            title={statusTitle}
          />
          <span
            className="text-xs font-medium"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {item.display_name}
          </span>
          <span
            className="px-1.5 py-0.5 rounded text-[10px] font-medium"
            style={
              item.billing === 'subscription'
                ? {
                    backgroundColor: 'var(--color-bg-tag)',
                    color: 'var(--color-text-secondary)',
                  }
                : {
                    backgroundColor: 'var(--color-accent-soft)',
                    color: 'var(--color-accent-primary)',
                  }
            }
          >
            {item.billing === 'subscription' ? '订阅' : '现金'}
          </span>
          {item.tos_risk === 'high' && (
            <span
              className="px-1.5 py-0.5 rounded text-[10px]"
              style={{
                backgroundColor: 'var(--color-warning-soft)',
                color: 'var(--color-warning)',
              }}
              title="订阅直连模仿官方 CLI 请求头，存在被限流或封号风险；可随时切换至现金模型或 OpenClaw broker。"
            >
              条款风险
            </span>
          )}
          {usedByRoles.length > 0 && (
            <span
              className="text-[11px] font-mono"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              [{usedByRoles.map((r) => r.role).join(', ')}]
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {!item.configured && (
            <span className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
              {item.access_type === 'oauth' ? (
                <a
                  href="#connected-accounts"
                  className="underline"
                  style={{ color: 'var(--color-accent-primary)' }}
                >
                  去连接 OAuth
                </a>
              ) : item.provider === 'xai' ? (
                '直连未启用：Grok 目前经 OpenClaw 代理使用（ocb-xai-*）'
              ) : (
                `在 .env 配置 ${item.credential?.env_key ?? 'API_KEY'} 后重启`
              )}
            </span>
          )}
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!item.enabled || !item.configured || probeMutation.isPending}
            onClick={() => void handleProbe()}
            className="h-7 px-2.5 text-xs"
            data-testid={`dalton-provider-probe-${item.provider}`}
          >
            {probeMutation.isPending ? (
              <Loader2 className="h-3 w-3 mr-1 animate-spin" />
            ) : (
              <Play className="h-3 w-3 mr-1" />
            )}
            测试连接
          </Button>
        </div>
      </div>

      {/* Metadata line */}
      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        {item.access_type === 'oauth' ? (
          <span>
            {item.configured
              ? `${item.credential?.email || '已连接'}${
                  item.credential?.plan_type ? ` (${item.credential.plan_type})` : ''
                }`
              : '未连接'}
          </span>
        ) : (
          <span>
            {item.credential?.has_env
              ? `.env (${item.credential.env_key}) 已配置`
              : item.credential?.has_byok
                ? 'BYOK 已配置'
                : '未配置'}
          </span>
        )}
        {formatSecondsLeft(secondsLeft) && <span>{formatSecondsLeft(secondsLeft)}</span>}
        {item.last_success?.at && (
          <span>
            最近成功：{item.last_success.at.slice(0, 19).replace('T', ' ')} (
            {item.last_success.model})
          </span>
        )}
        <span>24h 调用：{item.calls_24h}</span>
        {item.errors_24h > 0 && (
          <span style={{ color: 'var(--color-warning)' }}>
            24h 错误：{item.errors_24h}
          </span>
        )}
      </div>

      {/* Muse subscription & quota usage line */}
      {(showMuseSubsStatus || showMuseWindow || showMuseWeekly) && (
        <div
          className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]"
          style={{ color: 'var(--color-text-secondary)' }}
          data-testid="dalton-muse-subs-usage"
        >
          {showMuseSubsStatus && (
            <span>
              {typeof museSubsActive === 'boolean'
                ? `订阅：${museSubsActive ? '有效' : '未生效'}${
                    museTierName ? ` (${museTierName})` : ''
                  }`
                : `订阅：${museTierName}`}
            </span>
          )}
          {showMuseWindow && (
            <span>
              5 小时窗口已用 {museWindow!.used_percent}%，{museWindowReset} 重置
            </span>
          )}
          {showMuseWeekly && (
            <span>
              本周已用 {museWeekly!.used_percent}%，{museWeeklyReset} 重置
            </span>
          )}
        </div>
      )}

      {/* Warnings */}
      {(item.warnings ?? []).map((w, idx) => (
        <div
          key={`${w.code}-${idx}`}
          className="flex items-center gap-1.5 text-[11px]"
          style={{ color: 'var(--color-warning)' }}
        >
          <AlertTriangle className="h-3 w-3 shrink-0" />
          <span>{w.message}</span>
        </div>
      ))}

      {/* Probe inline feedback */}
      {probeResult && (
        <div
          className="text-xs font-mono px-2 py-1 rounded"
          style={{
            backgroundColor: probeResult.ok
              ? 'var(--color-profit-soft)'
              : 'var(--color-loss-soft)',
            color: probeResult.ok ? 'var(--color-profit)' : 'var(--color-loss)',
          }}
        >
          {probeResult.ok
            ? `✓ ${probeResult.model}: ${probeResult.message}`
            : `✗ ${probeResult.model ?? item.provider} [${probeResult.error_kind ?? 'error'}]: ${probeResult.message}`}
        </div>
      )}

      {/* Collapsible recent errors */}
      {(item.recent_errors?.length ?? 0) > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setErrorsExpanded((v) => !v)}
            className="flex items-center gap-1 text-[11px]"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {errorsExpanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            <span>最近错误 ({item.recent_errors.length})</span>
          </button>
          {errorsExpanded && (
            <div className="mt-1 space-y-1 text-[11px] font-mono">
              {item.recent_errors.map((err, i) => (
                <div
                  key={i}
                  className="px-2 py-1 rounded"
                  style={{
                    backgroundColor: 'var(--color-bg-card)',
                    color: 'var(--color-loss)',
                  }}
                >
                  [{err.at?.slice(0, 19).replace('T', ' ')}] {err.kind}{' '}
                  {err.model ? `(${err.model})` : ''}: {err.message}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
