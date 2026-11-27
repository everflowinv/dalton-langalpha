import React, { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, ArrowLeft, Check, Code, Copy, Download, ExternalLink, Eye, FileCode2 } from 'lucide-react';
import { withCopy } from '@/i18n';
import { isMarkdownFile } from '@/lib/dalton/sharedMarkdown';
import { downloadServedFile } from '@/pages/SharedChat/api';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import { Select } from '@/components/ui/select';
import { toast } from '@/components/ui/use-toast';
import { ListError, TagBadge } from '@/components/mcp/McpPrimitives';
import { apiErrorStatus } from '@/api/errors';
// DALTON-HOOK(monitors): normalize entry paths and track unified HtmlFrameDocState in monitor frames
import { artifactPathKey } from '@/hooks/useArtifact';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useLocale } from '@/hooks/useLocale';
import { GRANT_RENEW_MARGIN_MS, MIN_RENEW_INTERVAL_MS, msUntilRenewal } from '@/lib/expiry';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import { BridgedFrame, type FrameSource, type HtmlFrameDocState } from '@/pages/ChatAgent/components/viewers/html/BridgedFrame';
import { docStatesEqual } from '@/pages/ChatAgent/components/viewers/html/pageProtocol';
import { SERVED_HTML_SANDBOX } from '@/pages/ChatAgent/components/viewers/html/sandbox';
import { buildServeUrl, versionPrefix } from '@/pages/ChatAgent/components/viewers/html/wsfilesUrl';
import type { PageBridge } from '@/pages/ChatAgent/components/viewers/html/usePageBridge';
import { useBridgeMessage } from '@/pages/ChatAgent/components/viewers/html/usePageBridge';
// The public route (/m/:slug) reaches this viewer without the Monitors page, so
// the viewer brings its own styles; otherwise its frame host has no height.
import './Monitors.css';
import {
  getMonitorDetail,
  getPublicMonitorDetail,
  mintMonitorGrant,
  monitorPermalink,
  monitorShareUrl,
  type MonitorDetail,
} from './api';

interface MonitorViewerProps {
  slug: string;
  publicMode?: boolean;
}

const SharedFileBody = React.lazy(withCopy(
  () => import('@/pages/SharedChat/SharedFileView').then((m) => ({ default: m.FileBody })),
  ['filePanel', 'pageViewer', 'toolArtifact'],
));

export function MonitorViewer({ slug, publicMode = false }: MonitorViewerProps): React.ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const navigate = useNavigate();
  // Anonymous public viewers must not hit authenticated /api/v1/dalton/capabilities
  // (which 401s under Supabase auth); GET /api/v1/dalton/monitors/public/{slug} already
  // enforces data_jobs server-side and returns 404 when disabled.
  const { hasCapability, isLoading: capsLoading } = useCapabilities({ enabled: !publicMode });
  const dataJobsEnabled = hasCapability('data_jobs');

  const detailQuery = useQuery<MonitorDetail>({
    queryKey: publicMode ? ['dalton', 'monitors', 'public', slug] : queryKeys.daltonMonitors.detail(slug),
    queryFn: () => (publicMode ? getPublicMonitorDetail(slug) : getMonitorDetail(slug)),
    staleTime: 30_000,
    retry: (count, err) => {
      const status = apiErrorStatus(err);
      if (status === 403 || status === 404) return false;
      return count < 1;
    },
  });

  const detail = detailQuery.data;
  const monitor = detail?.monitor;
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null);
  const [mountTime] = useState(() => Date.now());
  const [renewedGrant, setRenewedGrant] = useState<{
    slug: string;
    forDetail: MonitorDetail;
    grant: string;
    frameBase: string;
    dataBase: string;
    expiresIn: number;
    receivedAt: number;
  } | null>(null);
  const [copied, setCopied] = useState(false);
  const [rawSource, setRawSource] = useState(false);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    setSelectedSeq(null);
    setRawSource(false);
  }, [slug]);

  // Periodically renew monitor_grant before its 12h expiry and push to iframe via postMessage.
  // Derive activeGrant synchronously from detail so version refreshes update frame key, src, and docState together.
  const detailReceivedAt = detailQuery.dataUpdatedAt || mountTime;
  const activeGrant = useMemo(() => {
    if (!detail || detail.monitor.slug !== slug) return null;
    if (renewedGrant && renewedGrant.slug === slug && renewedGrant.forDetail === detail) {
      return renewedGrant;
    }
    return {
      slug: detail.monitor.slug,
      grant: detail.monitor_grant,
      frameBase: detail.frame_base,
      dataBase: detail.data_base,
      expiresIn: detail.expires_in,
      receivedAt: detailReceivedAt,
    };
  }, [detail, slug, renewedGrant, detailReceivedAt]);

  useEffect(() => {
    if (publicMode || !activeGrant || !detail || activeGrant.expiresIn <= 0) return;
    const waitMs = Math.max(
      msUntilRenewal(activeGrant.expiresIn, activeGrant.receivedAt, GRANT_RENEW_MARGIN_MS),
      MIN_RENEW_INTERVAL_MS,
    );
    const timer = window.setTimeout(() => {
      void mintMonitorGrant(slug)
        .then((next) => {
          setRenewedGrant({
            slug,
            forDetail: detail,
            grant: next.monitor_grant,
            frameBase: next.frame_base,
            dataBase: next.data_base,
            expiresIn: next.expires_in,
            receivedAt: Date.now(),
          });
        })
        .catch(() => {
          void detailQuery.refetch();
        });
    }, waitMs);
    return () => window.clearTimeout(timer);
  }, [publicMode, activeGrant, detail, slug, detailQuery]);

  const effectiveSeq = publicMode
    ? (monitor?.published_seq || 1)
    : (selectedSeq ?? (monitor?.published_seq || detail?.versions[0]?.seq || 1));
  const selectedVersion = !publicMode
    ? detail?.versions.find((v) => v.seq === effectiveSeq)
    : undefined;
  const effectiveEntryPath = selectedVersion?.entry_path || monitor?.entry_path;
  const isMarkdownEntry = Boolean(effectiveEntryPath && isMarkdownFile(effectiveEntryPath));
  const activeVersionPrefix = activeGrant ? versionPrefix(activeGrant.frameBase, effectiveSeq) : '';
  const frameSource = useMemo<FrameSource | null>(() => {
    if (!monitor || !activeGrant || isMarkdownEntry) return null;
    const prefix = versionPrefix(activeGrant.frameBase, effectiveSeq);
    if (!effectiveEntryPath) {
      if (!publicMode) return null;
      return {
        key: `${monitor.monitor_id}:v${effectiveSeq}`,
        src: `${prefix.replace(/\/+$/, '')}/`,
        sandbox: SERVED_HTML_SANDBOX,
      };
    }
    return {
      key: `${monitor.monitor_id}:v${effectiveSeq}`,
      src: buildServeUrl(prefix, effectiveEntryPath),
      sandbox: SERVED_HTML_SANDBOX,
    };
  }, [monitor, activeGrant, effectiveSeq, effectiveEntryPath, publicMode, isMarkdownEntry]);

  // DALTON-HOOK(monitors): guard bridge & unified HtmlFrameDocState by frameKey so monitor/version switches never bleed stale paths.
  const currentFrameKey = frameSource?.key ?? null;
  const [bridgeEntry, setBridgeEntry] = useState<{ key: string | null; bridge: PageBridge | null }>({
    key: null,
    bridge: null,
  });
  const [docEntry, setDocEntry] = useState<{ key: string | null; state: HtmlFrameDocState }>({
    key: null,
    state: { kind: 'pending' },
  });
  const handleBridge = useCallback(
    (next: PageBridge, frameKey?: string | null) => {
      const resolvedKey = frameKey !== undefined ? frameKey : (next.frameKey !== undefined ? next.frameKey : currentFrameKey);
      if (resolvedKey !== currentFrameKey) return;
      setBridgeEntry({ key: resolvedKey, bridge: next });
    },
    [currentFrameKey],
  );
  const handleDocStateChange = useCallback(
    (next: HtmlFrameDocState, frameKey: string | null) => {
      if (frameKey !== currentFrameKey) return;
      setDocEntry((prev) =>
        prev.key === frameKey && docStatesEqual(prev.state, next)
          ? prev
          : { key: frameKey, state: next },
      );
    },
    [currentFrameKey],
  );
  const bridge = currentFrameKey !== null && bridgeEntry.key === currentFrameKey ? bridgeEntry.bridge : null;
  const docState: HtmlFrameDocState =
    currentFrameKey !== null && docEntry.key === currentFrameKey ? docEntry.state : { kind: 'pending' };

  // Push current monitor_grant into the framed document whenever bridge is ready or grant renews.
  useEffect(() => {
    if (!bridge?.ready || !activeGrant) return;
    bridge.post({
      type: 'page:monitor-grant',
      grant: activeGrant.grant,
      dataBase: activeGrant.dataBase,
      expiresIn: activeGrant.expiresIn,
    });
  }, [bridge, bridge?.ready, bridge?.session, activeGrant]);

  // Route cross-monitor links clicked inside the sandboxed frame via React Router so target ACLs are re-checked.
  useBridgeMessage(bridge, 'page:navigate-monitor', (message) => {
    navigate(message.path);
  });

  const permalink = monitor
    ? monitorShareUrl(monitor.slug, monitor.visibility)
    : monitorPermalink(slug);

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(permalink);
      setCopied(true);
      toast({ description: t('monitors:linkCopied') });
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ description: permalink });
    }
  };

  const handleDownloadMarkdown = () => {
    if (!activeVersionPrefix || !effectiveEntryPath) return;
    const fileName = effectiveEntryPath.split('/').pop() || `${monitor?.slug || 'document'}.md`;
    setDownloading(true);
    downloadServedFile(buildServeUrl(activeVersionPrefix, effectiveEntryPath), fileName)
      .catch(() => {
        toast({ description: t('monitors:markdown.downloadFailed'), variant: 'destructive' });
      })
      .finally(() => setDownloading(false));
  };

  if (detailQuery.isLoading || (!publicMode && capsLoading)) {
    return (
      <div className="monitors-viewer items-center justify-center">
        <Loader size={20} label={t('common.loading')} className="text-(--color-text-tertiary)" />
      </div>
    );
  }

  const hiddenDataJob = !publicMode && monitor?.source_kind === 'data_job' && !dataJobsEnabled;
  if (detailQuery.isError || !monitor || !detail || hiddenDataJob) {
    const status = apiErrorStatus(detailQuery.error);
    const title = status === 403 ? t('monitors:forbiddenTitle') : t('monitors:notFoundTitle');
    const desc = status === 403 ? t('monitors:forbiddenDesc') : t('monitors:notFoundDesc');
    return (
      <div className="monitors-page">
        <div className="monitors-scroll">
          <div className="monitors-container">
            {!publicMode && (
              <Link to="/monitors" className="tap-target inline-flex items-center gap-1.5 text-xs hover:underline" style={{ color: 'var(--color-text-secondary)' }}>
                <ArrowLeft className="h-3.5 w-3.5" />
                {t('monitors:backToList')}
              </Link>
            )}
            <ListError>{title} — {desc}</ListError>
          </div>
        </div>
      </div>
    );
  }

  // DALTON-HOOK(monitors): gate monitor topbar actions (open-in-new-window and go-to-source) on unified HtmlFrameDocState
  const isUnbridgedDoc = docState.kind === 'unbridged';
  const isLinkedDoc = docState.kind === 'linked';
  const showOpenInNewWindow = docState.kind !== 'unbridged' || docState.currentUrl !== null;
  const canOpenSource =
    !publicMode &&
    !isUnbridgedDoc &&
    !isLinkedDoc &&
    Boolean(monitor.can_manage && monitor.source_kind === 'workspace' && monitor.workspace_id);

  return (
    <div className="monitors-viewer">
      <header className="monitors-topbar">
        {!publicMode && (
          <>
            <Link
              to="/monitors"
              className="tap-target inline-flex items-center gap-1 text-xs shrink-0 hover:underline"
              style={{ color: 'var(--color-text-secondary)' }}
              aria-label={t('monitors:backToList')}
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">{t('monitors:backToList')}</span>
            </Link>
            <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }} aria-hidden="true">/</span>
          </>
        )}
        <h1 className="monitors-topbar-title" title={monitor.title}>
          {monitor.title}
        </h1>
        <TagBadge soft>{t(`monitors:visibility.${monitor.visibility}`)}</TagBadge>

        <div className="monitors-topbar-actions">
          {!publicMode && detail.versions.length > 0 && (
            <Select
              aria-label={t('monitors:versionSelect')}
              value={String(effectiveSeq)}
              onChange={(e) => setSelectedSeq(Number(e.target.value))}
              className="monitors-version-select w-28"
              style={{ fontSize: '0.75rem' }}
            >
              {detail.versions.map((v) => (
                <option key={v.seq} value={String(v.seq)}>
                  {t('monitors:versionOption', { seq: v.seq })}
                </option>
              ))}
            </Select>
          )}

          {isMarkdownEntry && (
            <>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="tap-target h-[30px] gap-1.5 px-2.5 text-xs"
                aria-label={rawSource ? t('monitors:markdown.viewRendered') : t('monitors:markdown.viewRaw')}
                onClick={() => setRawSource((v) => !v)}
              >
                {rawSource ? <Eye className="h-3.5 w-3.5" /> : <Code className="h-3.5 w-3.5" />}
                <span className="hidden sm:inline">
                  {rawSource ? t('monitors:markdown.viewRendered') : t('monitors:markdown.viewRaw')}
                </span>
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="tap-target h-[30px] gap-1.5 px-2.5 text-xs"
                aria-label={t('monitors:markdown.download')}
                disabled={downloading}
                onClick={handleDownloadMarkdown}
              >
                <Download className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">{t('monitors:markdown.download')}</span>
              </Button>
            </>
          )}

          <Button
            type="button"
            variant="outline"
            size="sm"
            className="tap-target h-[30px] gap-1.5 px-2.5 text-xs"
            aria-label={t('monitors:copyLink')}
            onClick={() => void copyLink()}
          >
            {copied ? <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} /> : <Copy className="h-3.5 w-3.5" />}
            <span className="hidden sm:inline">{t('monitors:copyLink')}</span>
          </Button>

          {showOpenInNewWindow && (
            <a
              href={permalink}
              target="_blank"
              rel="noopener noreferrer"
              className="tap-target inline-flex h-[30px] items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium hover:bg-(--color-bg-hover)"
              aria-label={t('monitors:openInNewWindow')}
              style={{
                borderColor: 'var(--color-border-muted)',
                color: 'var(--color-text-primary)',
              }}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">{t('monitors:openInNewWindow')}</span>
            </a>
          )}

          {canOpenSource && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="tap-target h-[30px] gap-1.5 px-2.5 text-xs"
              aria-label={t('monitors:goToSource')}
              onClick={() => {
                const target = monitor.entry_path
                  ? `/chat/${monitor.workspace_id}?file=${encodeURIComponent(monitor.entry_path)}`
                  : `/chat/${monitor.workspace_id}`;
                navigate(target);
              }}
            >
              <FileCode2 className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">{t('monitors:goToSource')}</span>
            </Button>
          )}
        </div>
      </header>

      {monitor.last_publish_error && monitor.last_publish_failed_at && (
        <div
          data-testid="monitor-publish-error"
          role="status"
          className="flex items-start gap-2 border-b px-4 py-1.5 text-xs whitespace-normal break-words"
          style={{
            borderColor: 'var(--color-border-muted)',
            backgroundColor: 'var(--color-bg-subtle, transparent)',
            color: 'var(--color-warning)',
          }}
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 flex-1 whitespace-normal break-words leading-relaxed">
            {t('monitors:lastPublishFailed', {
              time: formatTimestamp(monitor.last_publish_failed_at, locale),
            })}
            {monitor.last_publish_error.trim() && (
              <> — {monitor.last_publish_error}</>
            )}
          </span>
        </div>
      )}

      {/* DALTON-HOOK(monitors): expose docState on monitor host and wire BridgedFrame expectedDocKey & onDocStateChange */}
      <div className="monitors-frame-host" data-doc-state={docState.kind}>
        {isMarkdownEntry && effectiveEntryPath && activeVersionPrefix ? (
          <Suspense fallback={null}>
            <SharedFileBody
              scope={`monitor:${monitor.monitor_id}:v${effectiveSeq}`}
              path={effectiveEntryPath}
              frameBase={activeVersionPrefix}
              onDownload={handleDownloadMarkdown}
              downloading={downloading}
              onGone={() => {
                void detailQuery.refetch();
              }}
              rawSource={rawSource}
            />
          </Suspense>
        ) : (
          <BridgedFrame
            source={frameSource}
            title={monitor.title}
            groundKey={monitor.monitor_id}
            expectedDocKey={effectiveEntryPath ? artifactPathKey(effectiveEntryPath) : null}
            onBridge={handleBridge}
            onDocStateChange={handleDocStateChange}
            onUnbridged={() => {
              void detailQuery.refetch();
            }}
          />
        )}
      </div>
    </div>
  );
}
