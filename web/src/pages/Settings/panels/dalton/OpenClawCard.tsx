import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  RefreshCw,
} from 'lucide-react';
import { StatusPill } from '@/components/mcp/McpPrimitives';
import { Button } from '@/components/ui/button';
import {
  useOpenClawHealth,
  useOpenClawStatus,
  useOpenClawSync,
} from '../../hooks/useOpenClaw';
import type { OpenClawHealthResponse, OpenClawSyncResponse } from '../../utils/daltonApi';
import { SyncDiffDialog } from './SyncDiffDialog';

function healthPillMeta(health: OpenClawHealthResponse | undefined, t: (k: string, d: string) => string) {
  if (!health) {
    return {
      icon: RefreshCw,
      label: t('dalton.openclaw.healthChecking', '检查中...'),
      color: 'var(--color-text-tertiary)',
      bg: 'var(--color-bg-tag)',
    };
  }
  switch (health.status) {
    case 'ok':
      return {
        icon: CheckCircle2,
        label: `broker 正常 · ${health.latency_ms}ms`,
        color: 'var(--color-profit)',
        bg: 'var(--color-profit-soft)',
      };
    case 'degraded': {
      const pending = health.broker?.journal?.pending ?? 0;
      return {
        icon: AlertTriangle,
        label: `有 ${pending} 个未决调用`,
        color: 'var(--color-warning)',
        bg: 'var(--color-warning-soft)',
      };
    }
    case 'not_installed':
      return {
        icon: AlertTriangle,
        label: t('dalton.openclaw.healthNotInstalled', 'broker 未安装'),
        color: 'var(--color-warning)',
        bg: 'var(--color-warning-soft)',
      };
    case 'gateway_restarting':
      return {
        icon: AlertTriangle,
        label: t('dalton.openclaw.healthRestarting', '网关重启中'),
        color: 'var(--color-warning)',
        bg: 'var(--color-warning-soft)',
      };
    case 'auth_error':
      return {
        icon: AlertTriangle,
        label: t('dalton.openclaw.healthAuthError', '令牌无效'),
        color: 'var(--color-warning)',
        bg: 'var(--color-warning-soft)',
      };
    case 'token_missing':
      return {
        icon: AlertTriangle,
        label: t('dalton.openclaw.healthTokenMissing', '未配置令牌'),
        color: 'var(--color-warning)',
        bg: 'var(--color-warning-soft)',
      };
    case 'unreachable':
    default:
      return {
        icon: AlertCircle,
        label: t('dalton.openclaw.healthUnreachable', '不可达'),
        color: 'var(--color-loss)',
        bg: 'var(--color-loss-soft)',
      };
  }
}

export function OpenClawCard({ embedded = false }: { embedded?: boolean } = {}) {
  const { t } = useTranslation();
  const statusQuery = useOpenClawStatus(true);
  const healthQuery = useOpenClawHealth(true);
  const syncMutation = useOpenClawSync();

  const [previewData, setPreviewData] = useState<OpenClawSyncResponse | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [modelsExpanded, setModelsExpanded] = useState(false);
  const [errorDetailOpen, setErrorDetailOpen] = useState(false);
  const [syncBanner, setSyncBanner] = useState<{ tone: 'profit' | 'loss'; text: string } | null>(
    null,
  );

  const statusData = statusQuery.data;
  const healthData = healthQuery.data;
  const pill = healthPillMeta(healthData, t);

  const handlePreviewSync = async () => {
    setSyncBanner(null);
    try {
      const res = await syncMutation.mutateAsync({ dry_run: true });
      setPreviewData(res);
      setDialogOpen(true);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setSyncBanner({ tone: 'loss', text: msg });
    }
  };

  const handleConfirmApply = async (force: boolean) => {
    try {
      const res = await syncMutation.mutateAsync({ dry_run: false, force });
      setDialogOpen(false);
      setSyncBanner({
        tone: 'profit',
        text: `已同步：+${res.added.length} −${res.removed.length} ~${res.changed.length}`,
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setSyncBanner({ tone: 'loss', text: msg });
    }
  };

  const counts = statusData?.meta.counts ?? { catalog: 0, visible: 0, hidden: 0, text_only: 0 };
  const lastRun = statusData?.last_run;
  const smoke = healthData?.smoke;

  return (
    <div
      className={embedded ? 'flex flex-col gap-3' : 'flex flex-col gap-3 p-4 rounded-lg'}
      style={
        embedded
          ? undefined
          : {
              backgroundColor: 'var(--color-bg-card)',
              border: '1px solid var(--color-border-default)',
            }
      }
      data-testid="dalton-openclaw-card"
    >
      {/* Header row */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2.5">
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.openclaw.title', 'OpenClaw 模型网关')}
          </span>
          <StatusPill
            icon={pill.icon}
            label={pill.label}
            color={pill.color}
            bg={pill.bg}
            title={
              healthData
                ? `${healthData.base_url} · ${healthData.checked_at}`
                : undefined
            }
            testid="dalton-openclaw-health-pill"
          />
          <button
            type="button"
            onClick={() => void healthQuery.refetch()}
            className="text-xs underline underline-offset-2"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {t('dalton.openclaw.checkNow', '立即检查')}
          </button>
        </div>

        <Button
          size="sm"
          onClick={() => void handlePreviewSync()}
          disabled={syncMutation.isPending}
          data-testid="dalton-openclaw-sync-btn"
        >
          <RefreshCw
            className={`h-3.5 w-3.5 mr-1.5 ${syncMutation.isPending ? 'animate-spin' : ''}`}
          />
          {t('dalton.openclaw.resyncButton', '从 OpenClaw 重新同步')}
        </Button>
      </div>

      {/* Status metadata lines */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        <span>
          {t('dalton.openclaw.lastSync', '上次同步')}：
          {lastRun
            ? `${lastRun.started_at}（${lastRun.status}）`
            : statusData?.meta.generated_at ?? t('dalton.openclaw.neverSynced', '尚未同步')}
        </span>
        <span>
          可见 {counts.visible} · 隐藏 {counts.hidden} · 仅文本 {counts.text_only}
        </span>
        <span>
          {smoke?.generated_at
            ? `最近冒烟：${smoke.generated_at} · 阻断失败 ${smoke.blocking_failures?.length ?? 0}`
            : '尚未运行冒烟测试（uv run python scripts/dalton/openclaw_smoke.py --target broker）'}
        </span>
      </div>

      {/* Sync result banner */}
      {syncBanner && (
        <div
          className="px-3 py-2 rounded text-xs font-medium"
          style={{
            backgroundColor:
              syncBanner.tone === 'profit'
                ? 'var(--color-profit-soft)'
                : 'var(--color-loss-soft)',
            color:
              syncBanner.tone === 'profit'
                ? 'var(--color-profit)'
                : 'var(--color-loss)',
          }}
        >
          {syncBanner.text}
        </div>
      )}

      {/* Last run error / blocked alert */}
      {lastRun && (lastRun.status === 'failed' || lastRun.status === 'blocked') && (
        <div
          className="flex flex-col gap-1.5 px-3 py-2 rounded text-xs"
          style={{
            backgroundColor:
              lastRun.status === 'blocked'
                ? 'var(--color-warning-soft)'
                : 'var(--color-loss-soft)',
            color:
              lastRun.status === 'blocked'
                ? 'var(--color-warning)'
                : 'var(--color-loss)',
          }}
        >
          <div className="flex items-center justify-between gap-2">
            <span>
              最近一次同步{lastRun.status === 'blocked' ? '被引用保护阻断' : '失败'}：
              {lastRun.error ?? '未知原因'}
            </span>
            <button
              type="button"
              onClick={() => setErrorDetailOpen((v) => !v)}
              className="underline"
            >
              {errorDetailOpen ? '收起详情' : '查看详情'}
            </button>
          </div>
          {errorDetailOpen && (
            <pre className="text-[11px] whitespace-pre-wrap overflow-x-auto pt-1">
              {JSON.stringify(
                { error: lastRun.error, warnings: lastRun.warnings, diff: lastRun.diff },
                null,
                2,
              )}
            </pre>
          )}
        </div>
      )}

      {/* Role policy warnings */}
      {(statusData?.role_warnings?.length ?? 0) > 0 && (
        <div
          className="flex flex-col gap-1 px-3 py-2 rounded text-xs"
          style={{
            backgroundColor: 'var(--color-warning-soft)',
            color: 'var(--color-warning)',
          }}
        >
          {statusData!.role_warnings.map((w, idx) => (
            <div key={`${w.code}-${idx}`} className="flex items-center justify-between gap-2">
              <span>{w.message}</span>
              <a href={w.link} className="underline shrink-0">
                去检查
              </a>
            </div>
          ))}
        </div>
      )}

      {/* Collapsible model table */}
      <div className="pt-1">
        <button
          type="button"
          onClick={() => setModelsExpanded((v) => !v)}
          className="flex items-center gap-1 text-xs font-medium"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {modelsExpanded ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          <span>
            {t('dalton.openclaw.modelTableToggle', '模型清单')} ({statusData?.models.length ?? 0})
          </span>
        </button>

        {modelsExpanded && (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr
                  className="border-b"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-tertiary)',
                  }}
                >
                  <th className="py-1.5 pr-3">显示名</th>
                  <th className="py-1.5 pr-3">家族</th>
                  <th className="py-1.5 pr-3">上下文 / 输出</th>
                  <th className="py-1.5 pr-3">价格 ($/1M)</th>
                  <th className="py-1.5 pr-3">可见性</th>
                  <th className="py-1.5">工具能力</th>
                </tr>
              </thead>
              <tbody>
                {(statusData?.models ?? []).map((m) => (
                  <tr
                    key={m.key}
                    className="border-b"
                    style={{ borderColor: 'var(--color-border-muted)' }}
                  >
                    <td className="py-1.5 pr-3 font-medium" style={{ color: 'var(--color-text-primary)' }}>
                      <div>{m.display_name}</div>
                      <div className="font-mono text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                        {m.key}
                      </div>
                    </td>
                    <td className="py-1.5 pr-3 font-mono" style={{ color: 'var(--color-text-secondary)' }}>
                      {m.family}
                    </td>
                    <td className="py-1.5 pr-3 font-mono" style={{ color: 'var(--color-text-tertiary)' }}>
                      {m.context ? `${Math.round(m.context / 1000)}k` : '-'} /{' '}
                      {m.max_output ? `${Math.round(m.max_output / 1000)}k` : '-'}
                    </td>
                    <td className="py-1.5 pr-3 font-mono" style={{ color: 'var(--color-text-secondary)' }}>
                      {m.subscription
                        ? m.notional_summary
                          ? `订阅（名义 $${m.notional_summary.input ?? 0} / $${m.notional_summary.output ?? 0}）`
                          : '订阅'
                        : m.pricing_summary
                          ? `$${m.pricing_summary.input ?? 0} / $${m.pricing_summary.output ?? 0}`
                          : '未定价'}
                    </td>
                    <td className="py-1.5 pr-3" style={{ color: 'var(--color-text-secondary)' }}>
                      {m.visible ? '可见' : `隐藏 (${m.hide_reason ?? 'override'})`}
                    </td>
                    <td className="py-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                      {m.tool_calling ? '支持工具' : '仅文本'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
              如需调整可见性或参数档位，请编辑 <code>src/dalton/llm/openclaw_overrides.yaml</code> 后点重新同步。
            </p>
          </div>
        )}
      </div>

      <SyncDiffDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        preview={previewData}
        applying={syncMutation.isPending}
        onConfirmApply={(force) => void handleConfirmApply(force)}
      />
    </div>
  );
}
