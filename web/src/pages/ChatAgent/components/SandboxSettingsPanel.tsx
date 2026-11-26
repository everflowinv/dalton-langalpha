import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { useBackdropDismiss, useDialogA11y } from '@/hooks/useDialogA11y';
import { useWorkspace } from '@/hooks/useWorkspace';
import { queryKeys } from '@/lib/queryKeys';
// --- DALTON-HOOK(cos-settings-tools): begin ---
import {
  apiErrorObject, apiErrorStatus, archiveWorkspaceWithSnapshot, formatApiErrorDetail,
  getSandboxStats, getWorkspaceArchivePreview, refreshWorkspace,
  startComputer, stopComputer,
  type ArchivePreviewResponse,
} from '../utils/api';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import { ModalShell } from '@/components/ui/ModalShell';
// --- DALTON-HOOK(cos-settings-tools): end ---
import { patchComputerStatusInCaches, useComputers } from '../hooks/useComputers';
import { isComputerPreparing } from '../utils/computerStatus';
import { denialMessage } from '../utils/denialMessage';
import { ListEmpty, ListSkeleton } from '@/components/mcp/McpPrimitives';
import { McpTab } from './mcp/McpTab';
import { SkillsTab } from './SkillsTab';
import { OverviewTab } from './sandbox/OverviewTab';
import { PackagesTab } from './sandbox/PackagesTab';
import { StorageTab } from './sandbox/StorageTab';
import { ToolsTab } from './sandbox/ToolsTab';
import type { RefreshResult, SandboxStats } from './sandbox/sandboxTypes';
import './SandboxSettingsPanel.css';

interface SandboxSettingsPanelProps {
  onClose: () => void;
  workspaceId: string;
}

/**
 * SandboxSettingsContent -- sandbox settings tabs and content, usable inline or in a modal.
 */
export function SandboxSettingsContent({ workspaceId }: { workspaceId: string }) {
  const [activeTab, setActiveTab] = useState('overview');
  const [stats, setStats] = useState<SandboxStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Storage expand
  const [showDirBreakdown, setShowDirBreakdown] = useState(false);

  // Tools refresh
  const [refreshing, setRefreshing] = useState(false);
  const [refreshResult, setRefreshResult] = useState<RefreshResult | null>(null);

  // Start/stop
  const [actionLoading, setActionLoading] = useState(false);

  // --- DALTON-HOOK(cos-settings-tools): begin ---
  // Whole-computer archive confirmation dialog state
  const [archiveDialogOpen, setArchiveDialogOpen] = useState(false);
  const [archivePreview, setArchivePreview] = useState<ArchivePreviewResponse | null>(null);
  const [archivePreviewLoading, setArchivePreviewLoading] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [archiveSubmitting, setArchiveSubmitting] = useState(false);
  const [archiveExpanded, setArchiveExpanded] = useState(false);
  const archiveTitleId = useId();
  const archivePreviewReqRef = useRef(0);
  // --- DALTON-HOOK(cos-settings-tools): end ---

  const queryClient = useQueryClient();
  const { t } = useTranslation();

  // The machine this workspace lives on. Start/stop act on it, and its name
  // is what the panel has to say out loud before the user stops five projects.
  const { data: workspace } = useWorkspace(workspaceId);
  const computerId = workspace?.computer_id ?? null;
  const { data: computerData } = useComputers({ enabled: !!computerId });
  const computer = computerId
    ? computerData?.computers.find((c) => c.computer_id === computerId) ?? null
    : null;

  // Only the newest stats request may commit. Refresh is deliberately never
  // disabled, so a slow full-path read (~15s of probes) can still be in flight
  // when a faster post-action read lands — without this the older response wins
  // by arriving last and resurrects a stopped sandbox as running.
  const statsRequestRef = useRef(0);

  const loadStats = useCallback(async () => {
    const requestId = ++statsRequestRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await getSandboxStats(workspaceId);
      if (requestId !== statsRequestRef.current) return;
      setStats(data);
    } catch (err) {
      if (requestId !== statsRequestRef.current) return;
      setError(formatApiErrorDetail(err));
    } finally {
      if (requestId === statsRequestRef.current) setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    if (!workspaceId) return;
    // Drop the outgoing workspace's stats before reading the new one. A refresh
    // now keeps the panel on screen rather than blanking it, so without this a
    // workspace switch would render the old sandbox under the new id.
    setStats(null);
    void loadStats();
  }, [workspaceId, computer?.status, loadStats]);

  // Start and stop belong to the machine: its sandbox is what runs, and every
  // workspace on it moves together. Archive, and start on a row that names no
  // machine, still go through the workspace alias; such a row cannot be stopped.
  async function handleStartStop(action: string) {
    setActionLoading(true);
    try {
      if (computerId && (action === 'start' || action === 'stop')) {
        const res = action === 'start'
          ? await startComputer(computerId, { lazy: true })
          : await stopComputer(computerId);
        patchComputerStatusInCaches(queryClient, computerId, res.status);
        void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.all });
      } else {
        await api.post(`/api/v1/workspaces/${workspaceId}/${action}`);
      }
      await loadStats();
    } catch (err) {
      setError(denialMessage(err, t));
    } finally {
      setActionLoading(false);
    }
  }

  // --- DALTON-HOOK(cos-settings-tools): begin ---
  const loadArchivePreview = useCallback(async (preserveError = false) => {
    const reqId = ++archivePreviewReqRef.current;
    setArchivePreviewLoading(true);
    if (!preserveError) setArchiveError(null);
    try {
      const data = await getWorkspaceArchivePreview(workspaceId);
      if (reqId !== archivePreviewReqRef.current) return;
      setArchivePreview(data);
    } catch (err) {
      if (reqId !== archivePreviewReqRef.current) return;
      setArchiveError(formatApiErrorDetail(err) || t('computer.overview.archiveDialog.loadFailed'));
    } finally {
      if (reqId === archivePreviewReqRef.current) setArchivePreviewLoading(false);
    }
  }, [workspaceId, t]);

  function handleOpenArchiveDialog() {
    setArchiveExpanded(false);
    setArchivePreview(null);
    setArchiveError(null);
    setArchiveDialogOpen(true);
    void loadArchivePreview();
  }

  function handleCloseArchiveDialog() {
    if (archiveSubmitting) return;
    setArchiveDialogOpen(false);
    setArchiveError(null);
  }

  async function handleConfirmArchive() {
    if (!archivePreview || archiveSubmitting) return;
    setArchiveSubmitting(true);
    setArchiveError(null);
    try {
      await archiveWorkspaceWithSnapshot(workspaceId, {
        expected_computer_id: archivePreview.computer_id,
        expected_workspaces: archivePreview.expected_workspaces,
      });
      setArchiveDialogOpen(false);
      if (computerId) {
        patchComputerStatusInCaches(queryClient, computerId, 'stopped');
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.all });
      await loadStats();
    } catch (err) {
      const status = apiErrorStatus(err);
      const detailObj = apiErrorObject(err);
      if (status === 409 && detailObj?.code === 'scope_changed') {
        setArchiveError(t('computer.overview.archiveDialog.scopeChanged'));
        void loadArchivePreview(true);
      } else {
        setArchiveError(denialMessage(err, t));
      }
    } finally {
      setArchiveSubmitting(false);
    }
  }
  // --- DALTON-HOOK(cos-settings-tools): end ---

  async function handleRefresh() {
    setRefreshing(true);
    setRefreshResult(null);
    try {
      const result = await refreshWorkspace(workspaceId);
      setRefreshResult(result);
      // Reload stats to get updated MCP list
      loadStats();
    } catch (err) {
      setRefreshResult({ status: 'error', message: formatApiErrorDetail(err) });
    } finally {
      setRefreshing(false);
    }
  }

  const tabs = [
    { key: 'overview', label: 'Overview' },
    { key: 'mcp', label: 'MCP' },
    { key: 'skills', label: 'Skills' },
    { key: 'storage', label: 'Storage' },
    { key: 'packages', label: 'Packages' },
    { key: 'tools', label: 'Runtime' },
  ];

  // Canonical value only. Provider synonyms are the API's job to translate — see
  // _DISPLAY_STATE_SYNONYMS server-side.
  const displayStats = stats && computer ? { ...stats, state: computer.status } : stats;
  const isRunning = displayStats?.state === 'running';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Tabs. The accent underline is Quiet's mark for the open one; the open
          card styles give it their own (SandboxSettingsPanel.css), since amber does
          not select there. */}
      <div className="pf-sbx-tabs flex flex-wrap gap-1 mb-4 border-b" style={{ borderColor: 'var(--color-shell-divider, var(--color-border-muted))' }}>
        {tabs.map(t => (
          <button
            key={t.key}
            type="button"
            onClick={() => setActiveTab(t.key)}
            data-slot="tab"
            data-active={activeTab === t.key || undefined}
            className="px-3 py-2 text-sm font-medium border-b-2 border-b-transparent data-active:border-b-(--color-accent-primary)"
            style={{ color: activeTab === t.key ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)' }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
      {/* Skeleton only before the first load. Refresh reads through the same
          path, and blanking the panel would discard the status the user is
          watching — and unmount the button they just pressed. */}
      {loading && !stats ? (
        <ListSkeleton rows={4} />
      ) : error ? (
        <ErrorState message={error} onRetry={loadStats} />
      ) : (
        <>
          {activeTab === 'overview' && (
            <OverviewTab
              stats={displayStats!}
              isRunning={isRunning}
              actionLoading={actionLoading}
              refreshing={loading}
              onStartStop={handleStartStop}
              canStop={computerId !== null}
              onRefresh={loadStats}
              computerName={computer?.name ?? null}
              recoverableCreating={computer?.status === 'creating'}
              preparing={isComputerPreparing(computer)}
              dirName={workspace?.dir_name ?? null}
              // DALTON-HOOK(cos-settings-tools): gate Archive button on archivable === true and open whole-computer modal
              archivable={displayStats?.archivable === true}
              onArchiveClick={handleOpenArchiveDialog}
            />
          )}
          {activeTab === 'mcp' && <McpTab workspaceId={workspaceId} />}
          {activeTab === 'skills' && <SkillsTab workspaceId={workspaceId} />}
          {activeTab === 'storage' && (
            isRunning ? (
              <StorageTab
                stats={displayStats!}
                showDirBreakdown={showDirBreakdown}
                onToggleBreakdown={() => setShowDirBreakdown(!showDirBreakdown)}
              />
            ) : (
              <OfflineTabPlaceholder tabName="storage" />
            )
          )}
          {activeTab === 'packages' && (
            isRunning ? (
              <PackagesTab
                workspaceId={workspaceId}
                packages={stats!.packages ?? []}
                defaultPackages={stats!.default_packages ?? []}
                onInstalled={loadStats}
              />
            ) : (
              <OfflineTabPlaceholder tabName="packages" />
            )
          )}
          {activeTab === 'tools' && (
            isRunning ? (
              <ToolsTab
                stats={displayStats!}
                refreshing={refreshing}
                refreshResult={refreshResult}
                onRefresh={handleRefresh}
              />
            ) : (
              <OfflineTabPlaceholder tabName="runtime" />
            )
          )}
        </>
      )}
      </div>
      {/* --- DALTON-HOOK(cos-settings-tools): begin --- */}
      {archiveDialogOpen && (() => {
        const visibleWorkspaces = (archivePreview?.expected_workspaces ?? []).filter((w) => !w.is_stand_in);
        const shownWorkspaces = archiveExpanded ? visibleWorkspaces : visibleWorkspaces.slice(0, 5);
        const hasMore = visibleWorkspaces.length > 5;
        return (
          <ModalShell
            labelId={archiveTitleId}
            title={t('computer.overview.archiveDialog.title')}
            onClose={handleCloseArchiveDialog}
            closeDisabled={archiveSubmitting}
            width="narrow"
            density="form"
            stacked
            footer={(
              <div className="flex flex-wrap items-center justify-end gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  onClick={handleCloseArchiveDialog}
                  disabled={archiveSubmitting}
                  className="min-h-9"
                >
                  {t('computer.overview.archiveDialog.cancel')}
                </Button>
                <Button
                  type="button"
                  onClick={handleConfirmArchive}
                  disabled={archiveSubmitting || archivePreviewLoading || !archivePreview}
                  aria-busy={archiveSubmitting}
                  className="min-h-9"
                >
                  {archiveSubmitting
                    ? t('computer.overview.archiveDialog.archiving')
                    : t('computer.overview.archiveDialog.confirm')}
                </Button>
              </div>
            )}
          >
            <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              {t('computer.overview.archiveDialog.description')}
            </p>
            {archivePreviewLoading && !archivePreview && (
              <p className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                <Loader size={12} style={{ color: 'var(--color-accent-primary)' }} />
                {t('computer.overview.archiveDialog.loading')}
              </p>
            )}
            {archivePreview && (
              <div className="flex flex-col gap-2 text-sm min-w-0">
                <div className="break-words min-w-0" style={{ color: 'var(--color-text-primary)' }}>
                  <span className="font-medium">{t('computer.overview.archiveDialog.computerLabel')}</span>{' '}
                  <span>{archivePreview.computer_name || archivePreview.computer_id}</span>
                </div>
                <div className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('computer.overview.archiveDialog.affectedTitle', { count: visibleWorkspaces.length })}
                </div>
                {visibleWorkspaces.length > 0 && (
                  <ul
                    className="flex flex-col gap-1 rounded-md border px-3 py-2 text-xs min-w-0"
                    style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-text-primary)' }}
                  >
                    {shownWorkspaces.map((ws) => (
                      <li key={ws.workspace_id} className="flex items-center justify-between gap-2 min-w-0">
                        <span className="break-words min-w-0 font-medium">{ws.name || ws.workspace_id}</span>
                        {ws.status && (
                          <span className="shrink-0" style={{ color: 'var(--color-text-tertiary)' }}>
                            {ws.status}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {hasMore && (
                  <button
                    type="button"
                    onClick={() => setArchiveExpanded((v) => !v)}
                    className="self-start text-xs underline"
                    style={{ color: 'var(--color-text-tertiary)' }}
                  >
                    {archiveExpanded
                      ? t('computer.overview.archiveDialog.collapseWorkspaces')
                      : t('computer.overview.archiveDialog.expandWorkspaces', { count: visibleWorkspaces.length })}
                  </button>
                )}
              </div>
            )}
            {archiveError && (
              <p className="text-sm" role="alert" style={{ color: 'var(--color-icon-danger, var(--color-loss))' }}>
                {archiveError}
              </p>
            )}
          </ModalShell>
        );
      })()}
      {/* --- DALTON-HOOK(cos-settings-tools): end --- */}
    </div>
  );
}

/**
 * SandboxSettingsPanel -- full-screen overlay showing sandbox details.
 */
export default function SandboxSettingsPanel({ onClose, workspaceId }: SandboxSettingsPanelProps) {
  const titleId = useId();
  const dialogRef = useDialogA11y<HTMLDivElement>(onClose);
  const backdrop = useBackdropDismiss<HTMLDivElement>(onClose);
  return (
    <div
      className="fixed inset-0 z-1010 flex items-center justify-center"
      style={{ backgroundColor: 'var(--color-bg-overlay-strong)' }}
      {...backdrop}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="relative w-full max-w-2xl rounded-lg p-4 sm:p-6"
        style={{
          backgroundColor: 'var(--color-bg-elevated)',
          border: '1px solid var(--color-border-muted)',
          height: 'min(80vh, 650px)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        {/* Close button */}
        <button
          onClick={onClose}
          className="absolute top-4 right-4 p-1 rounded-full transition-colors hover:bg-foreground/10"
          style={{ color: 'var(--color-text-primary)' }}
          aria-label="Close"
        >
          <X className="h-5 w-5" />
        </button>

        {/* Title */}
        <h2 id={titleId} className="text-xl font-semibold mb-6" style={{ color: 'var(--color-text-primary)' }}>
          Sandbox Settings
        </h2>

        <SandboxSettingsContent workspaceId={workspaceId} />
      </div>
    </div>
  );
}

/** Panel-level load failure. Not `ListError`: this one owns the retry that is
 *  the only way back — nothing polls the stats endpoint. */
function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-4 py-8">
      <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>{message}</p>
      <button
        onClick={onRetry}
        className="px-4 py-2 text-sm rounded-md transition-colors hover:bg-foreground/10"
        style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-elevated)' }}
      >
        Retry
      </button>
    </div>
  );
}

function OfflineTabPlaceholder({ tabName }: { tabName: string }) {
  return <ListEmpty>Start the workspace to view {tabName}</ListEmpty>;
}
