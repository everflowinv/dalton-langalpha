import React, { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RefreshCw, Save } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonFeishuReadScopes,
  fetchDaltonFeishuSpaces,
  updateDaltonFeishuReadScopes,
  type DaltonFeishuSpace,
} from '../../utils/daltonApi';

function extractApiError(err: unknown, fallback: string): string {
  if (err && typeof err === 'object') {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
    const msg = (err as { message?: unknown }).message;
    if (typeof msg === 'string' && msg.trim()) {
      return msg;
    }
  }
  return fallback;
}

export function FeishuReadScopesCard(): React.ReactElement | null {
  const { canInvite } = useTeamCredentials();
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const scopesQuery = useQuery({
    queryKey: queryKeys.dalton.feishuReadScopes(),
    queryFn: fetchDaltonFeishuReadScopes,
    enabled: canInvite,
  });

  const spacesQuery = useQuery({
    queryKey: queryKeys.dalton.feishuSpaces(),
    queryFn: fetchDaltonFeishuSpaces,
    enabled: canInvite,
    retry: false,
  });

  // Local selection starts from the persisted whitelist; a refetch (save
  // invalidation) re-syncs it unless the user has unsaved edits in progress.
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (!dirty && scopesQuery.data) {
      setSelected(new Set(scopesQuery.data.scopes.map((s) => s.space_id)));
    }
  }, [scopesQuery.data, dirty]);

  const saveMutation = useMutation({
    mutationFn: () => {
      // Carry the names we already know (from the bridge listing or the
      // persisted whitelist) so saving does not wipe them.
      const nameById = new Map<string, string>();
      for (const s of scopesQuery.data?.scopes ?? []) {
        if (s.name) nameById.set(s.space_id, s.name);
      }
      for (const s of spacesQuery.data?.spaces ?? []) {
        if (s.name) nameById.set(s.space_id, s.name);
      }
      return updateDaltonFeishuReadScopes(
        [...selected].map((space_id) => ({ space_id, name: nameById.get(space_id) ?? '' })),
      );
    },
    onSuccess: () => {
      setError(null);
      setDirty(false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.feishuReadScopes() });
    },
    onError: (err: unknown) => {
      setError(extractApiError(err, t('common.failed')));
    },
  });

  const spaces: DaltonFeishuSpace[] = useMemo(() => {
    const fromBridge = spacesQuery.data?.spaces ?? [];
    // Whitelisted spaces the bot can no longer see still render (checked),
    // so unchecking them can remove them from the whitelist.
    const known = new Set(fromBridge.map((s) => s.space_id));
    const extra = (scopesQuery.data?.scopes ?? []).filter((s) => !known.has(s.space_id));
    return [...fromBridge, ...extra.map((s) => ({ space_id: s.space_id, name: s.name }))];
  }, [spacesQuery.data, scopesQuery.data]);

  if (!canInvite) {
    return null;
  }

  const toggle = (spaceId: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(spaceId)) {
        next.delete(spaceId);
      } else {
        next.add(spaceId);
      }
      return next;
    });
    setDirty(true);
  };

  const spacesError = spacesQuery.isError
    ? extractApiError(spacesQuery.error, t('common.failed'))
    : null;

  return (
    <div
      className="flex flex-col gap-3 rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-feishu-scopes-card"
    >
      <div
        className="flex items-start justify-between gap-4 border-b pb-3"
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        <div className="flex flex-col gap-1">
          <span
            className="text-sm font-semibold"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('auth.feishuScopes.title')}
          </span>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('auth.feishuScopes.subtitle')}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => void spacesQuery.refetch()}
            disabled={spacesQuery.isFetching}
            data-testid="dalton-feishu-scopes-refresh-btn"
            className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-border/60 bg-background px-2.5 py-1 text-xs font-medium hover:bg-muted/60 disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${spacesQuery.isFetching ? 'animate-spin' : ''}`} />
            {t('auth.feishuScopes.refresh', '刷新')}
          </button>
          <Button
            type="button"
            size="sm"
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending || !dirty}
            data-testid="dalton-feishu-scopes-save-btn"
            className="shrink-0 whitespace-nowrap"
          >
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {saveMutation.isPending ? t('auth.feishuScopes.saving') : t('auth.feishuScopes.save')}
          </Button>
        </div>
      </div>

      {error && (
        <p role="alert" className="text-xs" style={{ color: 'var(--color-loss)' }}>
          {error}
        </p>
      )}

      {scopesQuery.isLoading ? (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('common.loading')}
        </p>
      ) : spaces.length === 0 ? (
        spacesError ? (
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {spacesError}
          </p>
        ) : (
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('auth.feishuScopes.empty')}
          </p>
        )
      ) : (
        <div className="flex flex-col gap-1.5" data-testid="dalton-feishu-scopes-list">
          {spaces.map((space) => {
            const id = `dalton-feishu-scope-${space.space_id}`;
            return (
              <label
                key={space.space_id}
                htmlFor={id}
                className="flex items-center gap-2 text-xs"
                style={{ color: 'var(--color-text-primary)' }}
              >
                <input
                  id={id}
                  type="checkbox"
                  checked={selected.has(space.space_id)}
                  onChange={() => toggle(space.space_id)}
                  data-testid={id}
                />
                <span className="font-medium">{space.name || space.space_id}</span>
                <span
                  className="font-mono text-[10px]"
                  style={{ color: 'var(--color-text-tertiary)' }}
                >
                  {space.space_id}
                </span>
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}
