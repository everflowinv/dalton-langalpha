import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { AlertTriangle, RefreshCw, Search } from 'lucide-react';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { Button } from '@/components/ui/button';
import { StatusPill } from '@/components/mcp/McpPrimitives';
import {
  useNativeModelsCheck,
  useNativeModelsStatus,
  useNativeModelsSync,
} from '../../hooks/useOpenClaw';
import type {
  NativeModelsCheckResponse,
  NativeModelsRunRecord,
  NativeProviderListingRow,
} from '../../utils/daltonApi';
import { NativeSyncDiffDialog } from './NativeSyncDiffDialog';

function providerPillMeta(
  row: NativeProviderListingRow,
  t: TFunction,
): { label: string; color: string; bg: string } {
  switch (row.status) {
    case 'ok':
      return {
        label: t('dalton.nativeModels.providerOk', {
          defaultValue: '{{count}} 个模型',
          count: row.model_count,
        }),
        color: 'var(--color-profit)',
        bg: 'var(--color-profit-soft)',
      };
    case 'unsupported':
      return {
        label: t('dalton.nativeModels.providerUnsupported', '不提供列表'),
        color: 'var(--color-text-tertiary)',
        bg: 'var(--color-bg-tag)',
      };
    case 'error':
      return {
        label: row.message || t('dalton.nativeModels.providerError', '检查失败'),
        color: 'var(--color-loss)',
        bg: 'var(--color-loss-soft)',
      };
    default:
      return {
        label: t('dalton.nativeModels.providerNever', '尚未检查'),
        color: 'var(--color-text-tertiary)',
        bg: 'var(--color-bg-tag)',
      };
  }
}

const RUN_STATUS_ZH: Record<NativeModelsRunRecord['status'], string> = {
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  blocked: '被阻断',
  interrupted: '已中断',
};

function runStatusZh(status: NativeModelsRunRecord['status']): string {
  return RUN_STATUS_ZH[status] ?? status;
}

export function NativeModelsCard({ embedded = false }: { embedded?: boolean } = {}) {
  const { t } = useTranslation();
  const { isPm } = useTeamCredentials();
  const statusQuery = useNativeModelsStatus(true);
  const checkMutation = useNativeModelsCheck();
  const syncMutation = useNativeModelsSync();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [checkResult, setCheckResult] = useState<NativeModelsCheckResponse | null>(null);
  const [viewingStoredDiff, setViewingStoredDiff] = useState(false);
  const [banner, setBanner] = useState<{ tone: 'profit' | 'loss'; text: string } | null>(null);

  const statusData = statusQuery.data;
  const lastCheck = statusData?.last_check;
  const added = statusData?.diff.added ?? [];
  const deprecatedCount = statusData?.diff.deprecated.length ?? 0;

  const openStoredDiff = () => {
    setBanner(null);
    setCheckResult(null);
    setViewingStoredDiff(true);
    setDialogOpen(true);
  };

  const handleCheck = async () => {
    setBanner(null);
    try {
      const res = await checkMutation.mutateAsync();
      setCheckResult(res);
      setViewingStoredDiff(false);
      setDialogOpen(true);
      if (res.diff.added.length === 0 && res.diff.deprecated.length === 0) {
        setBanner({ tone: 'profit', text: t('dalton.nativeModels.noChanges', '检查完成：没有新增或下线的原生模型。') });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setBanner({ tone: 'loss', text: msg });
    }
  };

  const handleConfirmSync = async (selectedKeys: string[]) => {
    try {
      const res = await syncMutation.mutateAsync({ models: selectedKeys });
      setDialogOpen(false);
      setBanner({
        tone: 'profit',
        text: t('dalton.nativeModels.syncDone', {
          defaultValue: '已加入 {{count}} 个模型，已热加载。',
          count: res.added_keys.length,
        }),
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setBanner({ tone: 'loss', text: msg });
    }
  };

  // The dialog shows the fresh check result when there is one, otherwise the
  // diff stored by the last check (the 查看差异 entry).
  const dialogAdded = viewingStoredDiff || !checkResult ? added : checkResult.diff.added;
  const dialogDeprecated =
    viewingStoredDiff || !checkResult ? statusData?.diff.deprecated ?? [] : checkResult.diff.deprecated;
  const dialogUnchanged =
    viewingStoredDiff || !checkResult ? statusData?.diff.unchanged ?? 0 : checkResult.diff.unchanged;

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
      data-testid="dalton-native-models-card"
    >
      {/* Header row */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2.5">
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('dalton.nativeModels.title', '原生模型同步')}
          </span>
          {added.length > 0 && (
            <StatusPill
              icon={AlertTriangle}
              label={t('dalton.nativeModels.foundNew', {
                defaultValue: '发现 {{count}} 个新模型',
                count: added.length,
              })}
              color="var(--color-profit)"
              bg="var(--color-profit-soft)"
              testid="dalton-native-models-new-pill"
            />
          )}
        </div>

        <div className="flex items-center gap-2">
          {(added.length > 0 || deprecatedCount > 0) && (
            <Button size="sm" variant="outline" onClick={openStoredDiff} data-testid="dalton-native-models-view-diff-btn">
              {t('dalton.nativeModels.viewDiff', '查看差异')}
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={() => void handleCheck()}
            disabled={!isPm || checkMutation.isPending}
            title={isPm ? undefined : t('dalton.nativeModels.pmOnly', '仅 PM 可执行检查与同步')}
            data-testid="dalton-native-models-check-btn"
          >
            <Search className={`h-3.5 w-3.5 mr-1.5 ${checkMutation.isPending ? 'animate-pulse' : ''}`} />
            {checkMutation.isPending
              ? t('dalton.nativeModels.checking', '检查中...')
              : t('dalton.nativeModels.check', '检查')}
          </Button>
        </div>
      </div>

      {/* Status metadata lines */}
      <div
        className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        <span>
          {t('dalton.nativeModels.lastCheck', '上次检查')}：
          {lastCheck
            ? `${lastCheck.started_at}（${runStatusZh(lastCheck.status)}）`
            : t('dalton.nativeModels.neverChecked', '尚未检查')}
        </span>
        <span>
          {t('dalton.nativeModels.runtimeOverlay', '运行时 overlay')}：
          {statusData?.overlay.exists
            ? `${statusData.overlay.model_count} 个模型 · ${statusData.overlay.generated_at ?? ''}`
            : t('dalton.nativeModels.noOverlay', '未生成')}
        </span>
        {!isPm && (
          <span>{t('dalton.nativeModels.pmOnlyHint', '仅查看模式（检查与同步需 PM 权限）')}</span>
        )}
      </div>

      {banner && (
        <div
          className="px-3 py-2 rounded text-xs font-medium"
          style={{
            backgroundColor:
              banner.tone === 'profit' ? 'var(--color-profit-soft)' : 'var(--color-loss-soft)',
            color: banner.tone === 'profit' ? 'var(--color-profit)' : 'var(--color-loss)',
          }}
        >
          {banner.text}
        </div>
      )}

      {/* Per-provider rows */}
      <div className="flex flex-col gap-1.5">
        {(statusData?.providers ?? []).map((row) => {
          const pill = providerPillMeta(row, t);
          return (
            <div key={row.provider} className="flex items-center justify-between gap-2 text-xs">
              <span style={{ color: 'var(--color-text-secondary)' }}>{row.display_name}</span>
              <span
                className="px-2 py-0.5 rounded"
                style={{ backgroundColor: pill.bg, color: pill.color }}
                title={row.message ?? undefined}
              >
                {pill.label}
              </span>
            </div>
          );
        })}
        {statusData === undefined && statusQuery.isPending && (
          <span className="text-xs flex items-center gap-1.5" style={{ color: 'var(--color-text-tertiary)' }}>
            <RefreshCw className="h-3 w-3 animate-spin" />
            {t('dalton.nativeModels.loading', '加载上次检查结果...')}
          </span>
        )}
      </div>

      <p className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
        {t(
          'dalton.nativeModels.footnote',
          '打开设置页不会自动拉取；检查使用团队凭证调用各供应商的模型列表接口，确认后只把新增模型写入运行时 overlay。',
        )}
      </p>

      <NativeSyncDiffDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        added={dialogAdded}
        deprecated={dialogDeprecated}
        unchanged={dialogUnchanged}
        applying={syncMutation.isPending}
        canSync={isPm}
        onConfirmSync={(keys) => void handleConfirmSync(keys)}
      />
    </div>
  );
}
