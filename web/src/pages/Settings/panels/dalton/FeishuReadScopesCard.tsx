import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { formatApiErrorDetail } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { hasRequiredCapability, useCapabilities } from '@/hooks/useCapabilities';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { isDaltonAuth } from '@/config/daltonAuth';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonFeishuReadScopes,
  fetchDaltonFeishuSpaces,
  updateDaltonFeishuReadScopes,
  type DaltonFeishuSpace,
} from '@/pages/Settings/utils/daltonApi';
import { SettingRow } from '@/pages/Settings/components/SettingsLayout';
import { ToggleSwitch } from '@/components/ui/switch';
import type { DaltonSettingVisibilityContext } from './DaltonCosSharingCard';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

export function canShowDaltonFeishuScopesCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  return (
    isDaltonAuth &&
    ctx?.callerTier === 'admin' &&
    hasRequiredCapability(ctx?.capabilities, 'feishu_docs')
  );
}

export const isDaltonFeishuScopesSettingVisible = canShowDaltonFeishuScopesCard;

/** Admin-only Feishu wiki read-scope allowlist card using upstream SettingRow + ToggleSwitch. */
export function FeishuReadScopesCard({
  ctx,
}: {
  ctx?: DaltonSettingVisibilityContext | null;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { callerTier } = useTeamCredentials();
  const { hasCapability } = useCapabilities();
  const feishuDocsEnabled = ctx?.capabilities
    ? hasRequiredCapability(ctx.capabilities, 'feishu_docs')
    : hasCapability('feishu_docs');
  const isAdmin =
    feishuDocsEnabled &&
    callerTier === 'admin' &&
    (ctx ? ctx.callerTier === 'admin' : true);

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [dirty, setDirty] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);

  const scopesQuery = useQuery({
    queryKey: queryKeys.dalton.feishuReadScopes(),
    queryFn: fetchDaltonFeishuReadScopes,
    enabled: isAdmin,
  });

  const spacesQuery = useQuery({
    queryKey: queryKeys.dalton.feishuSpaces(),
    queryFn: fetchDaltonFeishuSpaces,
    enabled: isAdmin,
  });

  useEffect(() => {
    if (!scopesQuery.data) return;
    setSelectedIds(new Set(scopesQuery.data.scopes.map((s) => s.space_id)));
    setDirty(false);
  }, [scopesQuery.data]);

  const mergedSpaces = useMemo<DaltonFeishuSpace[]>(() => {
    const byId = new Map<string, DaltonFeishuSpace>();
    for (const s of spacesQuery.data?.spaces ?? []) {
      byId.set(s.space_id, s);
    }
    for (const persisted of scopesQuery.data?.scopes ?? []) {
      if (!byId.has(persisted.space_id)) {
        byId.set(persisted.space_id, {
          space_id: persisted.space_id,
          name: persisted.name || persisted.space_id,
        });
      }
    }
    return Array.from(byId.values());
  }, [spacesQuery.data?.spaces, scopesQuery.data?.scopes]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const payload = Array.from(selectedIds).map((id) => {
        const space = mergedSpaces.find((s) => s.space_id === id);
        return { space_id: id, name: space?.name ?? id };
      });
      return updateDaltonFeishuReadScopes(payload);
    },
    onSuccess: () => {
      setDirty(false);
      setSavedNotice(true);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.feishuReadScopes() });
    },
  });

  const handleToggleSpace = (spaceId: string, checked: boolean) => {
    setSavedNotice(false);
    setDirty(true);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(spaceId);
      else next.delete(spaceId);
      return next;
    });
  };

  if (!isAdmin) return null;

  const isLoading = scopesQuery.isLoading || spacesQuery.isLoading;
  const loadErr = spacesQuery.error ?? scopesQuery.error;
  const loadErrorMsg = loadErr ? formatApiErrorDetail(loadErr) || t('dalton.common.loadFailed') : null;

  return (
    <DaltonSettingsCard
      id="dalton-feishu-scopes"
      testId="dalton-feishu-scopes-card"
      title={t('auth.feishuScopes.title')}
      description={t('auth.feishuScopes.subtitle')}
      layout="rows"
      action={
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <DaltonRefreshButton
            busy={spacesQuery.isFetching || scopesQuery.isFetching}
            label={t('auth.feishuScopes.refresh')}
            onClick={() => {
              void scopesQuery.refetch();
              void spacesQuery.refetch();
            }}
            testId="dalton-feishu-scopes-refresh"
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 text-xs"
            disabled={!dirty || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
            data-testid="dalton-feishu-scopes-save"
          >
            {saveMutation.isPending
              ? t('auth.feishuScopes.saving')
              : t('auth.feishuScopes.save')}
          </Button>
        </div>
      }
      below={
        <div className="space-y-2 mt-2">
          {savedNotice && (
            <DaltonInlineNotice tone="success">
              {t('auth.feishuScopes.saved')}
            </DaltonInlineNotice>
          )}
          {saveMutation.isError && (
            <DaltonInlineNotice tone="error">
              {formatApiErrorDetail(saveMutation.error) || t('dalton.common.saveFailed')}
            </DaltonInlineNotice>
          )}
        </div>
      }
    >
      {isLoading ? (
        <div className="p-4">
          <DaltonLoadingRow label={t('dalton.common.loading')} />
        </div>
      ) : loadErrorMsg ? (
        <div className="p-4">
          <DaltonInlineNotice tone="error">{loadErrorMsg}</DaltonInlineNotice>
        </div>
      ) : mergedSpaces.length === 0 ? (
        <div className="p-4">
          <DaltonInlineNotice tone="info">{t('auth.feishuScopes.empty')}</DaltonInlineNotice>
        </div>
      ) : (
        mergedSpaces.map((space) => (
          <SettingRow
            key={space.space_id}
            id={`feishu-scope-${space.space_id}`}
            label={
              <span className="inline-flex flex-wrap items-center gap-2 min-w-0 max-w-full">
                <span className="max-md:break-words min-w-0">{space.name}</span>
                {selectedIds.has(space.space_id) && (
                  <DaltonStatusBadge tone="accent" label={space.space_id} className="max-w-full max-md:break-all" />
                )}
              </span>
            }
          >
            <ToggleSwitch
              id={`feishu-scope-switch-${space.space_id}`}
              ariaLabel={space.name}
              checked={selectedIds.has(space.space_id)}
              disabled={saveMutation.isPending}
              onChange={() =>
                handleToggleSpace(space.space_id, !selectedIds.has(space.space_id))
              }
            />
          </SettingRow>
        ))
      )}
    </DaltonSettingsCard>
  );
}
