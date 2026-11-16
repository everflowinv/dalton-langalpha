import React, { useMemo } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ListEmpty, ListError, ListSkeleton, MetaText, SectionHeader, TagBadge } from '@/components/mcp/McpPrimitives';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  listMonitors,
  MONITOR_CATEGORIES,
  type MonitorCategory,
  type MonitorSummary,
} from './api';

export function MonitorsList(): React.ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const { data, isLoading, isError, isFetching, refetch } = useQuery<MonitorSummary[]>({
    queryKey: queryKeys.daltonMonitors.list(),
    queryFn: listMonitors,
    staleTime: 30_000,
  });

  const grouped = useMemo(() => {
    const map = new Map<MonitorCategory, MonitorSummary[]>();
    for (const cat of MONITOR_CATEGORIES) map.set(cat, []);
    for (const item of data ?? []) {
      const cat = MONITOR_CATEGORIES.includes(item.category) ? item.category : 'other';
      map.get(cat)!.push(item);
    }
    return MONITOR_CATEGORIES
      .map((cat) => ({ category: cat, items: map.get(cat) ?? [] }))
      .filter((group) => group.items.length > 0);
  }, [data]);

  return (
    <div className="monitors-page">
      <div className="monitors-scroll">
        <div className="monitors-container">
          <div className="monitors-header">
            <div className="min-w-0">
              <h1 className="title-font text-lg font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                {t('monitors.title')}
              </h1>
              <p className="mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('monitors.subtitle')}
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs shrink-0"
              onClick={() => void refetch()}
              disabled={isFetching}
            >
              <RefreshCw className={`h-3.5 w-3.5${isFetching ? ' animate-spin' : ''}`} />
              {t('common.retry', { defaultValue: 'Refresh' })}
            </Button>
          </div>

          {isLoading ? (
            <ListSkeleton rows={4} />
          ) : isError ? (
            <ListError onRetry={() => void refetch()}>
              {t('monitors.loadFailed')}
            </ListError>
          ) : grouped.length === 0 ? (
            <ListEmpty>
              <p className="font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                {t('monitors.emptyTitle')}
              </p>
              <p className="mt-1 text-xs">{t('monitors.emptyDesc')}</p>
            </ListEmpty>
          ) : (
            <div className="flex flex-col gap-7">
              {grouped.map((group) => (
                <section key={group.category} aria-label={t(`monitors.categories.${group.category}`)}>
                  <SectionHeader>
                    {t(`monitors.categories.${group.category}`)} ({group.items.length})
                  </SectionHeader>
                  <div className="mt-2.5 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
                    {group.items.map((item) => {
                      const ownerLabel = item.owner_name || item.owner_email || item.owner_user_id;
                      const pageTime = formatTimestamp(item.updated_at, locale);
                      const dataTime = item.data_as_of ? formatTimestamp(item.data_as_of, locale) : null;
                      return (
                        <Link
                          key={item.monitor_id}
                          to={`/monitors/${encodeURIComponent(item.slug)}`}
                          className="shell-box flex flex-col justify-between gap-3 rounded-lg p-4 transition-colors hover:bg-(--color-bg-hover)"
                          style={{
                            backgroundColor: 'var(--color-shell-fill, var(--color-bg-card))',
                            borderColor: 'var(--color-shell-edge, var(--color-border-muted))',
                          }}
                        >
                          <div className="flex items-start justify-between gap-2 min-w-0">
                            <span
                              className="text-sm font-medium leading-snug line-clamp-2"
                              style={{ color: 'var(--color-text-primary)' }}
                            >
                              {item.title}
                            </span>
                            <TagBadge soft>
                              {t(`monitors.visibility.${item.visibility}`)}
                            </TagBadge>
                          </div>

                          <div className="flex flex-col gap-1">
                            <MetaText>{t('monitors.ownerLabel', { owner: ownerLabel })}</MetaText>
                            <MetaText>{t('monitors.pageUpdatedAt', { time: pageTime })}</MetaText>
                            {dataTime && (
                              <MetaText>{t('monitors.dataUpdatedAt', { time: dataTime })}</MetaText>
                            )}
                            {item.last_publish_error && item.last_publish_failed_at && (
                              <span
                                className="inline-flex items-center gap-1 text-[0.6875rem]"
                                style={{ color: 'var(--color-warning)' }}
                              >
                                <AlertTriangle className="h-3 w-3 shrink-0" />
                                {t('monitors.lastPublishFailed', {
                                  time: formatTimestamp(item.last_publish_failed_at, locale),
                                })}
                              </span>
                            )}
                          </div>
                        </Link>
                      );
                    })}
                  </div>
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
