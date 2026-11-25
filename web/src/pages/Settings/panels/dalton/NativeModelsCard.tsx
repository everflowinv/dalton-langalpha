import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import {
  useNativeModelsCheck,
  useNativeModelsRestore,
  useNativeModelsRetire,
  useNativeModelsStatus,
  useNativeModelsSync,
} from '../../hooks/useOpenClaw';
import { NativeSyncDiffDialog } from './NativeSyncDiffDialog';
import {
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

/** Team model catalog sync status card inside Team Management -> Models & Providers. */
export function NativeModelsCard() {
  const { t } = useTranslation();
  const locale = useLocale();
  const { isAdmin } = useCallerIdentity();

  const statusQuery = useNativeModelsStatus(true);
  const checkMutation = useNativeModelsCheck();
  const syncMutation = useNativeModelsSync();
  const retireMutation = useNativeModelsRetire();
  const restoreMutation = useNativeModelsRestore();

  const [diffOpen, setDiffOpen] = useState(false);
  const [retiredOpen, setRetiredOpen] = useState(false);
  const [notice, setNotice] = useState<{
    tone: 'success' | 'error' | 'info' | 'warning';
    message: string;
  } | null>(null);

  const data = statusQuery.data;
  const added = data?.diff?.added ?? [];
  const deprecated = data?.diff?.deprecated ?? [];
  const unchanged = data?.diff?.unchanged ?? 0;
  const retired = data?.retired ?? [];

  const handleCheck = async () => {
    setNotice(null);
    try {
      const res = await checkMutation.mutateAsync();
      const count = res.diff?.added?.length ?? 0;
      if (count > 0 || (res.diff?.deprecated?.length ?? 0) > 0) {
        setDiffOpen(true);
      } else {
        setNotice({ tone: 'info', message: t('dalton.nativeModels.noChanges') });
      }
    } catch {
      setNotice({ tone: 'error', message: t('dalton.common.loadFailed') });
    }
  };

  const handleSync = async (selectedModels: string[]) => {
    setNotice(null);
    try {
      await syncMutation.mutateAsync({ models: selectedModels });
      setDiffOpen(false);
      setNotice({ tone: 'success', message: t('dalton.nativeModels.syncDone') });
    } catch {
      setNotice({ tone: 'error', message: t('dalton.common.saveFailed') });
    }
  };

  const handleRetire = async (selectedKeys: string[]) => {
    setNotice(null);
    try {
      const res = await retireMutation.mutateAsync({ keys: selectedKeys });
      setDiffOpen(false);
      const retiredCount = res.retired_keys?.length ?? 0;
      const inUseItems = (res.results ?? []).filter((r) => r.status === 'in_use');
      const notDeprecatedItems = (res.results ?? []).filter(
        (r) => r.status === 'not_deprecated',
      );
      const otherSkippedItems = (res.results ?? []).filter(
        (r) => r.status !== 'retired' && r.status !== 'in_use' && r.status !== 'not_deprecated',
      );
      const skippedCount =
        inUseItems.length + notDeprecatedItems.length + otherSkippedItems.length;

      const parts: string[] = [];
      if (retiredCount > 0) {
        parts.push(t('dalton.nativeModels.retireDone', { count: retiredCount }));
      }
      for (const item of inUseItems) {
        parts.push(
          item.in_use_by?.length
            ? t('dalton.nativeModels.retireInUseItem', {
                model: item.key,
                roles: item.in_use_by.join(', '),
              })
            : t('dalton.nativeModels.retireInUseSkipped', {
                count: 1,
                keys: item.key,
              }),
        );
      }
      for (const item of notDeprecatedItems) {
        parts.push(
          t('dalton.nativeModels.retireNotDeprecatedItem', { model: item.key }),
        );
      }
      for (const item of otherSkippedItems) {
        parts.push(`${item.key} (${item.status})`);
      }
      if (retiredCount === 0 && skippedCount === 0) {
        parts.push(t('dalton.nativeModels.retireNoChange'));
      }

      setNotice({
        tone: skippedCount > 0 || retiredCount === 0 ? 'warning' : 'success',
        message: parts.join(' '),
      });
    } catch {
      setNotice({ tone: 'error', message: t('dalton.common.saveFailed') });
    }
  };

  const handleRestore = async (key: string) => {
    setNotice(null);
    try {
      await restoreMutation.mutateAsync({ keys: [key] });
      setNotice({
        tone: 'success',
        message: t('dalton.nativeModels.restoreDone', { model: key }),
      });
    } catch {
      setNotice({ tone: 'error', message: t('dalton.common.saveFailed') });
    }
  };

  return (
    <>
      <DaltonSettingsCard
        id="dalton-native-models"
        testId="dalton-native-models-card"
        title={t('dalton.nativeModels.title')}
        description={t('dalton.nativeModels.footnote')}
        layout="padded"
        bodyClassName="space-y-3"
        action={
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            {(added.length > 0 || deprecated.length > 0) && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 text-xs"
                onClick={() => setDiffOpen(true)}
                data-testid="native-models-view-diff"
              >
                {t('dalton.nativeModels.viewDiff')}
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 text-xs"
              disabled={!isAdmin || checkMutation.isPending}
              title={!isAdmin ? t('dalton.nativeModels.pmOnly') : undefined}
              onClick={() => void handleCheck()}
              data-testid="native-models-check"
            >
              {checkMutation.isPending
                ? t('dalton.nativeModels.checking')
                : t('dalton.nativeModels.check')}
            </Button>
          </div>
        }
      >
        {statusQuery.isLoading ? (
          <DaltonLoadingRow label={t('dalton.nativeModels.loading')} />
        ) : statusQuery.isError ? (
          <DaltonInlineNotice tone="error">{t('dalton.common.loadFailed')}</DaltonInlineNotice>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <DaltonStatusBadge
                dot
                tone={data?.overlay?.exists ? 'accent' : 'neutral'}
                label={
                  data?.overlay?.exists
                    ? t('dalton.nativeModels.runtimeOverlay')
                    : t('dalton.nativeModels.noOverlay')
                }
              />
              {added.length > 0 && (
                <DaltonStatusBadge
                  tone="success"
                  label={t('dalton.nativeModels.foundNew', { count: added.length })}
                />
              )}
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {data?.last_check?.started_at
                  ? t('dalton.nativeModels.lastCheck', {
                      time: formatTimestamp(data.last_check.started_at, locale),
                    })
                  : t('dalton.nativeModels.neverChecked')}
              </span>
            </div>

            {!isAdmin && (
              <DaltonInlineNotice tone="info">{t('dalton.nativeModels.pmOnlyHint')}</DaltonInlineNotice>
            )}

            {notice && <DaltonInlineNotice tone={notice.tone}>{notice.message}</DaltonInlineNotice>}

            {(data?.providers?.length ?? 0) > 0 && (
              <DaltonKeyValueList>
                {data!.providers.map((p) => (
                  <DaltonItemRow
                    key={p.provider}
                    title={p.display_name || p.provider}
                    subtitle={p.message}
                    trailing={
                      <div className="flex items-center gap-2">
                        <span
                          className="text-xs tabular-nums"
                          style={{ color: 'var(--color-text-secondary)' }}
                        >
                          {p.model_count}
                        </span>
                        <DaltonStatusBadge
                          dot
                          tone={
                            p.status === 'ok'
                              ? 'success'
                              : p.status === 'error'
                                ? 'danger'
                                : 'neutral'
                          }
                          label={
                            p.status === 'ok'
                              ? t('dalton.nativeModels.providerOk', { count: p.model_count })
                              : p.status === 'unsupported'
                                ? t('dalton.nativeModels.providerUnsupported')
                                : p.status === 'error'
                                  ? t('dalton.nativeModels.providerError')
                                  : t('dalton.nativeModels.providerNever')
                          }
                        />
                      </div>
                    }
                  />
                ))}
              </DaltonKeyValueList>
            )}

            {retired.length > 0 && (
              <div className="pt-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-1.5 text-xs gap-1"
                  onClick={() => setRetiredOpen((v) => !v)}
                >
                  {retiredOpen ? (
                    <ChevronDown className="h-3.5 w-3.5" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5" />
                  )}
                  <span>
                    {t('dalton.nativeModels.previouslyRetired')} ({retired.length})
                  </span>
                </Button>
                <Disclosure open={retiredOpen}>
                  <div className="mt-2 space-y-2">
                    <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                      {t('dalton.nativeModels.previouslyRetiredHint')}
                    </p>
                    <DaltonKeyValueList>
                      {retired.map((r) => (
                        <DaltonItemRow
                          key={r.key}
                          title={<span className="font-mono text-xs">{r.key}</span>}
                          meta={
                            r.retired_at
                              ? formatTimestamp(r.retired_at, locale)
                              : undefined
                          }
                          trailing={
                            isAdmin ? (
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                className="h-8 text-xs"
                                disabled={restoreMutation.isPending}
                                onClick={() => void handleRestore(r.key)}
                              >
                                {t('dalton.nativeModels.restore')}
                              </Button>
                            ) : undefined
                          }
                        />
                      ))}
                    </DaltonKeyValueList>
                  </div>
                </Disclosure>
              </div>
            )}
          </>
        )}
      </DaltonSettingsCard>

      <NativeSyncDiffDialog
        open={diffOpen}
        onClose={() => setDiffOpen(false)}
        added={added}
        deprecated={deprecated}
        unchangedCount={unchanged}
        onSync={handleSync}
        onRetire={isAdmin ? handleRetire : undefined}
        syncing={syncMutation.isPending}
        retiring={retireMutation.isPending}
      />
    </>
  );
}
