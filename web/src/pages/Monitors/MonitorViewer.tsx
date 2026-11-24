import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, ArrowLeft, Check, Copy, ExternalLink, FileCode2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import { Select } from '@/components/ui/select';
import { toast } from '@/components/ui/use-toast';
import { ListError, TagBadge } from '@/components/mcp/McpPrimitives';
import { apiErrorStatus } from '@/api/errors';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useLocale } from '@/hooks/useLocale';
import { GRANT_RENEW_MARGIN_MS, MIN_RENEW_INTERVAL_MS, msUntilRenewal } from '@/lib/expiry';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import { BridgedFrame, type FrameSource } from '@/pages/ChatAgent/components/viewers/html/BridgedFrame';
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
  const [grantInfo, setGrantInfo] = useState<{
    slug: string;
    grant: string;
    frameBase: string;
    dataBase: string;
    expiresIn: number;
    receivedAt: number;
  } | null>(null);
  const [copied, setCopied] = useState(false);
  const [bridge, setBridge] = useState<PageBridge | null>(null);

  useEffect(() => {
    setSelectedSeq(null);
  }, [slug]);

  useEffect(() => {
    if (!detail) return;
    setGrantInfo({
      slug: detail.monitor.slug,
      grant: detail.monitor_grant,
      frameBase: detail.frame_base,
      dataBase: detail.data_base,
      expiresIn: detail.expires_in,
      receivedAt: Date.now(),
    });
  }, [detail]);

  // Periodically renew monitor_grant before its 12h expiry and push to iframe via postMessage.
  const activeGrant = grantInfo && grantInfo.slug === slug ? grantInfo : null;
  useEffect(() => {
    if (publicMode || !activeGrant || activeGrant.expiresIn <= 0) return;
    const waitMs = Math.max(
      msUntilRenewal(activeGrant.expiresIn, activeGrant.receivedAt, GRANT_RENEW_MARGIN_MS),
      MIN_RENEW_INTERVAL_MS,
    );
    const timer = window.setTimeout(() => {
      void mintMonitorGrant(slug)
        .then((next) => {
          setGrantInfo({
            slug,
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
  }, [publicMode, activeGrant, slug, detailQuery]);

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

  const effectiveSeq = publicMode
    ? (monitor?.published_seq || 1)
    : (selectedSeq ?? (monitor?.published_seq || detail?.versions[0]?.seq || 1));
  const selectedVersion = !publicMode
    ? detail?.versions.find((v) => v.seq === effectiveSeq)
    : undefined;
  const effectiveEntryPath = selectedVersion?.entry_path || monitor?.entry_path;
  const frameSource = useMemo<FrameSource | null>(() => {
    if (!monitor || !activeGrant) return null;
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
  }, [monitor, activeGrant, effectiveSeq, effectiveEntryPath, publicMode]);

  const permalink = monitor
    ? monitorShareUrl(monitor.slug, monitor.visibility)
    : monitorPermalink(slug);

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(permalink);
      setCopied(true);
      toast({ description: t('monitors.linkCopied') });
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ description: permalink });
    }
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
    const title = status === 403 ? t('monitors.forbiddenTitle') : t('monitors.notFoundTitle');
    const desc = status === 403 ? t('monitors.forbiddenDesc') : t('monitors.notFoundDesc');
    return (
      <div className="monitors-page">
        <div className="monitors-scroll">
          <div className="monitors-container">
            {!publicMode && (
              <Link to="/monitors" className="inline-flex items-center gap-1.5 text-xs hover:underline" style={{ color: 'var(--color-text-secondary)' }}>
                <ArrowLeft className="h-3.5 w-3.5" />
                {t('monitors.backToList')}
              </Link>
            )}
            <ListError>{title} — {desc}</ListError>
          </div>
        </div>
      </div>
    );
  }

  const canOpenSource = !publicMode && Boolean(monitor.can_manage && monitor.source_kind === 'workspace' && monitor.workspace_id);

  return (
    <div className="monitors-viewer">
      <header className="monitors-topbar">
        {!publicMode && (
          <>
            <Link
              to="/monitors"
              className="inline-flex items-center gap-1 text-xs shrink-0 hover:underline"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">{t('monitors.backToList')}</span>
            </Link>
            <span className="text-xs" style={{ color: 'var(--color-text-quaternary)' }} aria-hidden="true">/</span>
          </>
        )}
        <h1 className="monitors-topbar-title" title={monitor.title}>
          {monitor.title}
        </h1>
        <TagBadge soft>{t(`monitors.visibility.${monitor.visibility}`)}</TagBadge>

        {monitor.last_publish_error && monitor.last_publish_failed_at && (
          <span
            className="inline-flex items-center gap-1 text-xs shrink-0"
            style={{ color: 'var(--color-warning)' }}
            title={monitor.last_publish_error}
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            {t('monitors.lastPublishFailed', {
              time: formatTimestamp(monitor.last_publish_failed_at, locale),
            })}
          </span>
        )}

        <div className="monitors-topbar-actions">
          {!publicMode && detail.versions.length > 0 && (
            <Select
              aria-label={t('monitors.versionSelect')}
              value={String(effectiveSeq)}
              onChange={(e) => setSelectedSeq(Number(e.target.value))}
              className="w-28"
              style={{ height: '30px', fontSize: '0.75rem' }}
            >
              {detail.versions.map((v) => (
                <option key={v.seq} value={String(v.seq)}>
                  {t('monitors.versionOption', { seq: v.seq })}
                </option>
              ))}
            </Select>
          )}

          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-[30px] gap-1.5 px-2.5 text-xs"
            onClick={() => void copyLink()}
          >
            {copied ? <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} /> : <Copy className="h-3.5 w-3.5" />}
            <span className="hidden sm:inline">{t('monitors.copyLink')}</span>
          </Button>

          <a
            href={permalink}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex h-[30px] items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium hover:bg-(--color-bg-hover)"
            style={{
              borderColor: 'var(--color-border-muted)',
              color: 'var(--color-text-primary)',
            }}
          >
            <ExternalLink className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">{t('monitors.openInNewWindow')}</span>
          </a>

          {canOpenSource && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-[30px] gap-1.5 px-2.5 text-xs"
              onClick={() => {
                const target = monitor.entry_path
                  ? `/chat/${monitor.workspace_id}?file=${encodeURIComponent(monitor.entry_path)}`
                  : `/chat/${monitor.workspace_id}`;
                navigate(target);
              }}
            >
              <FileCode2 className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">{t('monitors.goToSource')}</span>
            </Button>
          )}
        </div>
      </header>

      <div className="monitors-frame-host">
        <BridgedFrame
          source={frameSource}
          title={monitor.title}
          groundKey={monitor.monitor_id}
          onBridge={setBridge}
          onUnbridged={() => {
            void detailQuery.refetch();
          }}
        />
      </div>
    </div>
  );
}
