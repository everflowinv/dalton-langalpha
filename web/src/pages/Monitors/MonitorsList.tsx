import React, { useMemo } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, ExternalLink, FileText, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ListEmpty, ListError, ListSkeleton, MetaText, SectionHeader, TagBadge } from '@/components/mcp/McpPrimitives';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import { isHtmlFile } from '@/pages/ChatAgent/utils/filePaths';
import {
  listMonitors,
  listSharedDocuments,
  MONITOR_CATEGORIES,
  type MonitorCategory,
  type MonitorSummary,
  type SharedDocumentSummary,
} from './api';

export function MonitorsList(): React.ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const { hasCapability, isLoading: capsLoading } = useCapabilities();
  const dataJobsEnabled = hasCapability('data_jobs');
  const { data, isLoading, isError, isFetching, refetch } = useQuery<MonitorSummary[]>({
    queryKey: queryKeys.daltonMonitors.list(),
    queryFn: listMonitors,
    staleTime: 30_000,
  });
  const sharedDocsQuery = useQuery<SharedDocumentSummary[]>({
    queryKey: queryKeys.daltonMonitors.sharedDocuments(),
    queryFn: listSharedDocuments,
    staleTime: 30_000,
  });

  const grouped = useMemo(() => {
    const map = new Map<MonitorCategory, MonitorSummary[]>();
    for (const cat of MONITOR_CATEGORIES) map.set(cat, []);
    for (const item of data ?? []) {
      if (!dataJobsEnabled && item.source_kind === 'data_job') continue;
      if (item.source_kind === 'workspace' && item.entry_path && !isHtmlFile(item.entry_path)) continue;
      const cat = MONITOR_CATEGORIES.includes(item.category) ? item.category : 'other';
      map.get(cat)!.push(item);
    }
    return MONITOR_CATEGORIES
      .map((cat) => ({ category: cat, items: map.get(cat) ?? [] }))
      .filter((group) => group.items.length > 0);
  }, [data, dataJobsEnabled]);

  const sharedDocs = useMemo(
    () =>
      (sharedDocsQuery.data ?? []).filter(
        (doc) => doc.visibility !== 'own' && (!doc.entry_path || !isHtmlFile(doc.entry_path)) && Boolean(doc.url),
      ),
    [sharedDocsQuery.data],
  );

  const refreshing = isFetching || sharedDocsQuery.isFetching;
  const handleRefresh = () => {
    void refetch();
    void sharedDocsQuery.refetch();
  };

  return (
    <div className="monitors-page">
      <div className="monitors-scroll">
        <div className="monitors-container">
          <div className="monitors-header">
            <div className="min-w-0">
              <h1 className="title-font text-lg font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                {t('monitors:title')}
              </h1>
              <p className="mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('monitors:subtitle')}
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="tap-target h-8 gap-1.5 text-xs shrink-0"
              onClick={handleRefresh}
              disabled={refreshing}
            >
              <RefreshCw className={`h-3.5 w-3.5${refreshing ? ' animate-spin' : ''}`} />
              {t('common.retry', { defaultValue: 'Refresh' })}
            </Button>
          </div>

          {isLoading || capsLoading ? (
            <ListSkeleton rows={4} />
          ) : isError ? (
            <ListError onRetry={handleRefresh}>
              {t('monitors:loadFailed')}
            </ListError>
          ) : grouped.length === 0 ? (
            <ListEmpty>
              <p className="font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                {t('monitors:emptyTitle')}
              </p>
              <p className="mt-1 text-xs">{t('monitors:emptyDesc')}</p>
            </ListEmpty>
          ) : (
            <div className="flex flex-col gap-7">
              {grouped.map((group) => (
                <section key={group.category} aria-label={t(`monitors:categories.${group.category}`)}>
                  <SectionHeader>
                    {t(`monitors:categories.${group.category}`)} ({group.items.length})
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
                              {t(`monitors:visibility.${item.visibility}`)}
                            </TagBadge>
                          </div>

                          <div className="flex flex-col gap-1">
                            <MetaText>{t('monitors:ownerLabel', { owner: ownerLabel })}</MetaText>
                            <MetaText>{t('monitors:pageUpdatedAt', { time: pageTime })}</MetaText>
                            {dataTime && (
                              <MetaText>{t('monitors:dataUpdatedAt', { time: dataTime })}</MetaText>
                            )}
                            {item.last_publish_error && item.last_publish_failed_at && (
                              <span
                                className="inline-flex items-center gap-1 text-[0.6875rem]"
                                style={{ color: 'var(--color-warning)' }}
                              >
                                <AlertTriangle className="h-3 w-3 shrink-0" />
                                {t('monitors:lastPublishFailed', {
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

          {sharedDocs.length > 0 && (
            <section
              aria-label={t('monitors:sharedDocuments.title')}
              data-testid="shared-documents-section"
              className="pt-2"
            >
              <SectionHeader>
                {t('monitors:sharedDocuments.title')} ({sharedDocs.length})
              </SectionHeader>
              <ul
                className="shell-box mt-2.5 divide-y rounded-lg"
                style={{
                  backgroundColor: 'var(--color-shell-fill, var(--color-bg-card))',
                  borderColor: 'var(--color-shell-edge, var(--color-border-muted))',
                }}
              >
                {sharedDocs.map((doc) => {
                  const ownerLabel = doc.owner_name || doc.owner_email || doc.owner_user_id;
                  const updatedTime = formatTimestamp(doc.updated_at, locale);
                  return (
                    <li
                      key={doc.artifact_id}
                      className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
                      style={{ borderColor: 'var(--color-border-subtle)' }}
                    >
                      <div className="flex min-w-0 flex-1 items-start gap-2.5">
                        <FileText
                          className="mt-0.5 h-4 w-4 shrink-0"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className="truncate text-sm font-medium"
                              style={{ color: 'var(--color-text-primary)' }}
                              title={doc.entry_path}
                            >
                              {doc.file_name}
                            </span>
                            <TagBadge soft>
                              {t(`monitors:visibility.${doc.visibility}`)}
                            </TagBadge>
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-0.5">
                            <MetaText>
                              {t('monitors:sharedDocuments.sharedBy', { owner: ownerLabel })}
                            </MetaText>
                            <MetaText>
                              {t('monitors:sharedDocuments.updatedAt', { time: updatedTime })}
                            </MetaText>
                          </div>
                        </div>
                      </div>
                      <a
                        href={doc.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="tap-target inline-flex h-8 shrink-0 items-center gap-1.5 self-start rounded-md border px-2.5 text-xs font-medium transition-colors hover:bg-(--color-bg-hover) sm:self-center"
                        style={{
                          borderColor: 'var(--color-border-muted)',
                          color: 'var(--color-text-primary)',
                        }}
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                        {t('monitors:sharedDocuments.openLink')}
                      </a>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
