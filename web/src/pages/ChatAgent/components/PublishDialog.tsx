// DALTON-HOOK(monitors): integrate 4-tier monitor publishing, ACLs, and admin public confirmation
import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore, type RefObject } from 'react';
import { Check, Copy, ExternalLink, Link2, Lock } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Loader } from '@/components/ui/loader';
import { SegmentedControl, type SegmentedOption } from '@/components/ui/segmented-control';
import { Select } from '@/components/ui/select';
import { ToggleSwitch } from '@/components/ui/switch';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { toast } from '@/components/ui/use-toast';
import { cn } from '@/lib/utils';
import { formatBytes, shortMonthDay } from '@/lib/format';
import { apiErrorDetail, formatApiErrorDetail, isForbiddenError } from '@/api/errors'; // DALTON-HOOK(external-share-guard)
import { queryKeys } from '@/lib/queryKeys';
import { registerAuthReset } from '@/lib/authResets';
import { useCopyShareLink } from '@/hooks/useCopyShareLink';
import { useIsMobile } from '@/hooks/useIsMobile'; // DALTON-HOOK(mobile-web)
import { isCoarsePointer } from '@/lib/dalton/pointer'; // DALTON-HOOK(mobile-web)
import { useLocale } from '@/hooks/useLocale';
import { useCapabilities } from '@/hooks/useCapabilities'; // DALTON-HOOK(colleague-access): gate auto-publish pause tracking on colleague_sharing capability
import { useCallerIdentity, type CallerIdentity } from '@/hooks/useCallerIdentity'; // DALTON-HOOK(external-share-guard)
import { useWorkspace } from '@/hooks/useWorkspace'; // DALTON-HOOK(colleague-access): read workspace auto-publish pause state & token
import {
  useArtifactPublishing,
  useArtifactStatus,
  useFileLeftOut,
  useFileLeftOutPending,
  useArtifactVersions,
  useOwnedArtifact,
  sharedSeq,
} from '@/hooks/useArtifact';
// DALTON-HOOK(monitors): ConfirmDialog and Monitor API helpers for 4-tier publish
import ConfirmDialog from '@/pages/Dashboard/components/ConfirmDialog';
import {
  getMonitorByArtifact,
  listCompanyMembers,
  MONITOR_CATEGORIES,
  monitorShareUrl,
  publishMonitor,
  updateMonitor,
  type CompanyMemberOption,
  type MonitorCategory,
  type MonitorSummary,
  type MonitorVisibility,
} from '@/pages/Monitors/api';
import { pageLoadsOf } from '@/lib/pageLoads';
import { usePageScout } from './viewers/html/usePageScout';
import type {
  ArtifactStatus,
  ArtifactVersion,
  ShareFileEntry,
  Workspace,
  WorkspacesResponse,
} from '@/types/api';
import { useWorkspaceFolders } from '../contexts/WorkspaceContext';
import { basename, dirname } from '../utils/fileRefResolver';
import { shareLinkHref } from '../utils/api/shareLinks';
import { filesChangedIn, refusalIn, type PublishRefusal } from './filePanel/usePublish';
import { useAccount } from '@/hooks/useAccount';
import { useLinkStanding } from '@/hooks/useLinkStanding';
import { RULE } from '@/lib/accountRules';
import { LinkRuleNote } from '@/components/organization/LinkRuleNote';
import { AudiencePicker } from '@/components/organization/AudiencePicker';
import { LibraryPublishRow } from '@/components/organization/LibraryPublishRow';
import type { ShareAudience } from '@/types/api';

/** The server takes a label of 1 to 200 characters. */
const LABEL_MAX = 200;

/**
 * DALTON-HOOK(colleague-access): browser-session execution context (not server
 * state). Holds a publish response's CAS pause token only while the workspace
 * detail cache is empty (for example, while the first detail GET is in flight
 * or when `/credentials/status` fails), so closing and reopening the dialog
 * within this tab still sends the token on the next submit. Removed as soon as
 * workspace detail data is written to the cache, and cleared on sign-out.
 */
const pendingPauseTokens = new Map<string, string>();
const pendingPauseTokenListeners = new Set<() => void>();
const watchedPauseQueryClients = new WeakSet<QueryClient>();

function detailWorkspaceIdFromKey(key: readonly unknown[]): string | null {
  const prefix = queryKeys.workspaces.details();
  if (key.length !== prefix.length + 1) return null;
  for (let i = 0; i < prefix.length; i += 1) {
    if (key[i] !== prefix[i]) return null;
  }
  const id = key[prefix.length];
  return typeof id === 'string' ? id : null;
}

function notifyPendingPauseTokens(): void {
  pendingPauseTokenListeners.forEach((listener) => listener());
}

function subscribePendingPauseTokens(listener: () => void): () => void {
  pendingPauseTokenListeners.add(listener);
  return () => pendingPauseTokenListeners.delete(listener);
}

function deletePendingPauseToken(workspaceId: string): void {
  if (pendingPauseTokens.delete(workspaceId)) {
    notifyPendingPauseTokens();
  }
}

function ensureDetailCacheWatcher(qc: QueryClient): void {
  if (watchedPauseQueryClients.has(qc)) return;
  watchedPauseQueryClients.add(qc);
  qc.getQueryCache().subscribe((event) => {
    if (pendingPauseTokens.size === 0) return;
    if (event.type !== 'updated' && event.type !== 'added') return;
    if (event.query.state.data === undefined) return;
    const wsId = detailWorkspaceIdFromKey(event.query.queryKey);
    if (wsId) deletePendingPauseToken(wsId);
  });
}

function setPendingPauseToken(qc: QueryClient, workspaceId: string, token: string): void {
  ensureDetailCacheWatcher(qc);
  if (pendingPauseTokens.get(workspaceId) === token) return;
  pendingPauseTokens.set(workspaceId, token);
  notifyPendingPauseTokens();
}

registerAuthReset(() => {
  if (pendingPauseTokens.size === 0) return;
  pendingPauseTokens.clear();
  notifyPendingPauseTokens();
});

interface PublishDialogProps {
  open: boolean;
  workspaceId: string;
  /** Any spelling the panel holds; the record's `path` is canonical. */
  path: string;
  onClose: () => void;
  /**
   * DALTON-HOOK(external-share-guard): an external member may only stop a
   * link they already made public — no publish, no keep-updated, no review
   * list. Unshared files never open this mode (the entry is hidden), so the
   * dialog shows the link, where it stands, and the one action.
   */
  unshareOnly?: boolean;
}

/** The last attempt's answer when it was not a new version, standing until the next attempt or close. */
type Outcome =
  /** A 409's list, shown in place of the status until the status is read again after the attempt. */
  | { kind: 'conflict'; files: ShareFileEntry[]; statusAt: number }
  | { kind: 'refused'; refusal: PublishRefusal }
  /** Published, but the files were as the latest version left them. */
  | { kind: 'unchanged'; seq: number };

function FileRow({ artifactId, entry, added, leftOut, gutter, locked, identity, onLeftOutError }: {
  artifactId: string;
  entry: ShareFileEntry;
  /** Read by the file now but in no version yet: what an Update would add. */
  added: boolean;
  /** Off what a publish carries, and on offer to put back. */
  leftOut: boolean;
  /** Some row of the list has a box, so the names line up past it. */
  gutter: boolean;
  /** A publish is under way with the list as it stands, or identity cannot edit. */
  locked: boolean;
  // DALTON-HOOK(external-share-guard): verify latest identity synchronously and reuse dialog error handling for left-out toggles
  identity: CallerIdentity;
  onLeftOutError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const isMobile = useIsMobile(); // DALTON-HOOK(mobile-web)
  const toggle = useFileLeftOut(artifactId);
  const out = toggle.isPending ? toggle.variables.leftOut : leftOut;
  const dir = dirname(entry.path);
  const name = basename(entry.path);
  // DALTON-HOOK(mobile-web): surface tooltip detail inline on mobile / coarse-pointer devices where hover is unavailable
  const detailText = out
    ? t('publish:leftOutDetail')
    : t(`shareLink.reasonDetail.${entry.reason === 'loaded' && entry.optional ? 'loadedBefore' : entry.reason}`);
  const showInlineDetail = (isMobile || isCoarsePointer()) && (out || (added && !out) || entry.reason !== 'entry');
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <li
          tabIndex={0}
          className="flex flex-col gap-0.5 px-3 py-1.5 text-xs outline-none hover:bg-(--color-bg-hover) focus-visible:bg-(--color-bg-hover)"
          data-selectable
        >
          <div className="flex items-center gap-3 min-w-0">
            {leftOut || entry.optional ? (
              <input
                type="checkbox"
                className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-(--color-accent-primary) disabled:cursor-not-allowed"
                aria-label={t('publish:includeFile', { file: name })}
                checked={!out}
                disabled={locked || toggle.isPending}
                onChange={(e) => {
                  // DALTON-HOOK(external-share-guard): block left-out writes while identity is unconfirmed/confirming or non-owner
                  const latest = identity.getLatestIdentity();
                  if (
                    locked ||
                    latest.isConfirming ||
                    latest.status !== 'ready' ||
                    latest.isExternal ||
                    !latest.canActAsOwner
                  ) {
                    return;
                  }
                  toggle.mutate(
                    { path: entry.path, leftOut: !e.target.checked },
                    { onError: onLeftOutError },
                  );
                }}
              />
            ) : gutter && <span aria-hidden className="w-3.5 shrink-0" />}
            {/* The folder gives way first: rows cut from the end would all read as
                the same folder. A touch screen cannot hover, so there it all wraps. */}
            <span className="flex flex-1 min-w-0 pointer-coarse:block pointer-coarse:wrap-anywhere">
              {dir && (
                <span className="min-w-0 truncate pointer-coarse:whitespace-normal" style={{ color: 'var(--color-text-tertiary)' }}>
                  {dir}/
                </span>
              )}
              <span
                className="shrink-0 max-w-full truncate pointer-coarse:whitespace-normal"
                style={{ color: out ? 'var(--color-text-tertiary)' : 'var(--color-text-primary)' }}
              >
                {name}
              </span>
            </span>
            <span className="shrink-0 tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>
              {entry.size !== null && formatBytes(entry.size, locale)}
            </span>
            <span
              className="shrink-0 w-auto md:w-24 text-right"
              style={{ color: added && !out ? 'var(--color-warning)' : 'var(--color-text-quaternary)' }}
            >
              {out ? t('publish:leftOut') : added ? t('publish:notShared') : t(`shareLink.reason.${entry.reason}`)}
            </span>
          </div>
          {/* DALTON-HOOK(mobile-web): inline file reason on mobile / coarse-pointer */}
          {showInlineDetail && (
            <p
              data-testid="publish-file-inline-reason"
              className={cn('text-[11px] leading-snug wrap-anywhere', gutter && 'pl-[26px]')}
              style={{ color: added && !out ? 'var(--color-warning)' : 'var(--color-text-tertiary)' }}
            >
              {detailText}
              {added && !out ? ` · ${t('publish:notSharedDetail')}` : ''}
            </p>
          )}
        </li>
      </TooltipTrigger>
      <TooltipContent side="top" align="start" className="max-w-md">
        <p className="wrap-anywhere">{entry.path}</p>
        <p className="mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
          {detailText}
        </p>
        {added && !out && <p className="mt-1" style={{ color: 'var(--color-warning)' }}>{t('publish:notSharedDetail')}</p>}
      </TooltipContent>
    </Tooltip>
  );
}

function VersionRow({ version }: { version: ArtifactVersion }) {
  const { t } = useTranslation();
  const locale = useLocale();
  return (
    <li className="flex items-center gap-3 px-3 py-1.5 text-xs">
      <span className="shrink-0 tabular-nums font-medium" style={{ color: 'var(--color-text-primary)' }}>
        {t('publish:version', { seq: version.seq })}
      </span>
      <span className="shrink-0 tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>
        {shortMonthDay(new Date(version.published_at), locale)}
      </span>
      <span
        className="flex-1 min-w-0 truncate"
        title={version.label ?? undefined}
        style={{ color: 'var(--color-text-secondary)' }}
        data-selectable
      >
        {version.label}
      </span>
      {version.origin === 'keep_updated' && (
        <span className="shrink-0" style={{ color: 'var(--color-text-quaternary)' }}>
          {t('publish:keptUpdated')}
        </span>
      )}
    </li>
  );
}

/** Versions, newest first. */
function VersionList({ artifactId }: { artifactId: string | undefined }) {
  const { t } = useTranslation();
  const headingId = useId();
  const versionsQuery = useArtifactVersions(artifactId);
  return (
    <div className="flex flex-col gap-1.5">
      <span id={headingId} className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
        {t('publish:versions')}
      </span>
      <div
        className={cn('rounded-lg border overflow-hidden', versionsQuery.isLoading && 'py-3')}
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        {versionsQuery.isLoading ? (
          <div className="flex justify-center">
            <Loader size={16} className="text-(--color-text-tertiary)" />
          </div>
        ) : versionsQuery.isError ? (
          <p className="px-3 py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('publish:versionsFailed')}
          </p>
        ) : (
          <ul
            className="max-h-32 overflow-y-auto divide-y"
            style={{ borderColor: 'var(--color-border-subtle)' }}
            aria-labelledby={headingId}
          >
            {(versionsQuery.data ?? []).map((version) => (
              <VersionRow key={version.seq} version={version} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** The review list: exactly what a publish sends. */
function ReviewList({ artifactId, heading, status, files, added, failure, failed, fetching, locked, identity, onLeftOutError, onRetry }: {
  artifactId: string;
  /** Null while the status is read. */
  heading: string | null;
  status: ArtifactStatus | undefined;
  files: ShareFileEntry[];
  added: ReadonlySet<string>;
  /** Why the status could not be read, when the owner can do something about it. */
  failure: string | null;
  failed: boolean;
  fetching: boolean;
  locked: boolean;
  // DALTON-HOOK(external-share-guard): forward unified identity & error handler to file checkboxes
  identity: CallerIdentity;
  onLeftOutError: (err: unknown) => void;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const headingId = useId();
  const leftOut = status?.left_out ?? [];
  // By name rather than as the server found them, so a row stays under the
  // pointer when its box is changed. One array, so it also keeps its focus.
  const rows = [...files, ...leftOut].sort((a, b) =>
    Number(b.reason === 'entry') - Number(a.reason === 'entry') || a.path.localeCompare(b.path));
  const gutter = rows.some((entry) => entry.optional) || leftOut.length > 0;
  return (
    <div className="flex flex-col gap-1.5">
      <span id={headingId} className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
        {heading ?? t('publish:listing')}
      </span>
      <div
        className={cn('rounded-lg border overflow-hidden', !status && !failed && 'py-4')}
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        {failed ? (
          <div className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
            <span style={{ color: 'var(--color-text-tertiary)' }}>
              {failure ?? t('publish:listFailed')}
            </span>
            <button
              type="button"
              onClick={onRetry}
              disabled={fetching}
              className="shrink-0 hover:underline disabled:opacity-50"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {t('common.retry')}
            </button>
          </div>
        ) : !status ? (
          <div className="flex justify-center">
            <Loader size={16} className="text-(--color-text-tertiary)" />
          </div>
        ) : (
          <TooltipProvider delayDuration={300} skipDelayDuration={600}>
            <ul
              className="max-h-56 overflow-y-auto divide-y"
              style={{ borderColor: 'var(--color-border-subtle)' }}
              aria-labelledby={headingId}
            >
              {rows.map((entry) => (
                <FileRow
                  key={entry.path}
                  artifactId={artifactId}
                  entry={entry}
                  added={added.has(entry.path)}
                  leftOut={leftOut.includes(entry)}
                  gutter={gutter}
                  locked={locked}
                  identity={identity}
                  onLeftOutError={onLeftOutError}
                />
              ))}
            </ul>
          </TooltipProvider>
        )}
      </div>
    </div>
  );
}

/**
 * Publish a workspace file: share its `/a/<code>` link and cut a version.
 *
 * The list is the review step: Publish (and Update, which is publishing
 * again) sends exactly the paths shown, and the server refuses a list that no
 * longer matches (409) with the one it found, which replaces the list and
 * waits for the owner to confirm again. Visitors get the latest version;
 * Unshare stops the link and keeps every version. The chat and the owner's
 * comments are never part of it.
 */
// DALTON-HOOK(external-share-guard): unshareOnly mode — stop a public link,
// never start one
function PublishDialog({ open, workspaceId, path, onClose, unshareOnly = false }: PublishDialogProps) {
  // Written by the review while a request is in flight, which holds the dialog open.
  const busy = useRef(false);
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o && !busy.current) onClose(); }}>
      <DialogContent
        className="sm:max-w-lg"
        style={{ backgroundColor: 'var(--color-shell-float, var(--color-bg-page))', borderColor: 'var(--color-shell-edge, var(--color-border-muted))' }}
      >
        {/* Mounted only while the dialog shows, so every open starts a fresh
            review, and the exit animation plays over the last one. */}
        <PublishReview
          open={open}
          workspaceId={workspaceId}
          path={path}
          busyRef={busy}
          onClose={onClose}
          unshareOnly={unshareOnly}
        />
      </DialogContent>
    </Dialog>
  );
}

function PublishReview({ open, workspaceId, path, busyRef, onClose, unshareOnly = false }: {
  // DALTON-HOOK(external-share-guard): drive open/visibility/focus revalidation in useCallerIdentity
  open: boolean;
  workspaceId: string;
  path: string;
  busyRef: RefObject<boolean>;
  onClose: () => void;
  /** DALTON-HOOK(external-share-guard): stop-only mode, see the props above. */
  unshareOnly?: boolean;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const isMobile = useIsMobile(); // DALTON-HOOK(mobile-web)
  const qc = useQueryClient();
  const root = useWorkspaceFolders()?.rootDir;
  // DALTON-HOOK(external-share-guard): unified caller identity with open/focus/403 revalidation
  const identity = useCallerIdentity({ revalidateOnOpen: true, open });
  // DALTON-HOOK(colleague-access): gate auto-publish pause tracking on colleague_sharing capability
  const { hasCapability } = useCapabilities();
  const colleagueSharingCapable = hasCapability('colleague_sharing');
  const colleagueSharingEnabled = identity.colleagueSharingEnabled;
  const colleagueSharingKnown = identity.status === 'ready';
  const isExternal = identity.isExternal;
  const isAdmin = identity.canPublishPublic;
  const isIdentityBlocked = identity.status !== 'ready' || identity.isConfirming;

  // A file never recorded has no status to review, so this makes its private record.
  const owned = useOwnedArtifact(workspaceId, path, { root });
  const artifact = owned.record ?? null;
  const latestSeq = artifact?.latest_seq ?? 0;
  // What the dialog says of visitors is said of their version, not the newest.
  const visitorsAt = sharedSeq(artifact);
  // DALTON-HOOK(external-share-guard): stop-only mode reviews nothing, so it
  // neither runs the page scout nor reads the status.
  const scout = usePageScout(workspaceId, unshareOnly ? null : (artifact?.path ?? null));
  // Read once before the scout loads the page, naming the code it will run,
  // and again once it has heard the page out.
  const statusQuery = useArtifactStatus(artifact, { always: true, report: true, enabled: !unshareOnly && scout.reads });
  const { unshare, keepUpdated, audience: audienceChange } = useArtifactPublishing();
  const changingList = useFileLeftOutPending(artifact?.artifact_id ?? '');
  const { setterName, orgT } = useAccount();
  const { copy, copiedCode } = useCopyShareLink();

  // DALTON-HOOK(monitors): load existing monitor config for this artifact in both normal and unshareOnly modes
  const existingMonitorQuery = useQuery<MonitorSummary | null>({
    queryKey: queryKeys.daltonMonitors.byArtifact(artifact?.artifact_id ?? ''),
    queryFn: () => getMonitorByArtifact(artifact!.artifact_id),
    enabled: Boolean(artifact?.artifact_id),
    staleTime: 10_000,
    retry: false,
  });
  const existingMonitor = existingMonitorQuery.data ?? null;
  const existingMonitorPending = Boolean(artifact?.artifact_id) && existingMonitorQuery.isPending;
  const existingMonitorFailed = Boolean(artifact?.artifact_id) && existingMonitorQuery.isError;
  const monitorStateReady = !artifact?.artifact_id || (!existingMonitorPending && !existingMonitorFailed);

  const [label, setLabel] = useState('');
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const [categoryDraft, setCategoryDraft] = useState<MonitorCategory | null>(null);
  const [visibilityDraft, setVisibilityDraft] = useState<MonitorVisibility | null>(null);
  const [membersDraft, setMembersDraft] = useState<string[] | null>(null);
  const [keepDraft, setKeepDraft] = useState<boolean | null>(null);
  const [publicConfirmOpen, setPublicConfirmOpen] = useState(false);
  // DALTON-HOOK(external-share-guard): clear public selection on live admin -> internal downgrade so it cannot rebound to public
  const [publicReselectRequired, setPublicReselectRequired] = useState(false);
  const [downgradingToOwn, setDowngradingToOwn] = useState(false);
  const [publishingMonitor, setPublishingMonitor] = useState(false);
  const [copiedPermalink, setCopiedPermalink] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const prevReadyTierRef = useRef<string | null>(identity.status === 'ready' ? identity.tier : null);
  useEffect(() => {
    if (identity.status !== 'ready') {
      setPublicConfirmOpen(false);
      return;
    }
    const prevTier = prevReadyTierRef.current;
    prevReadyTierRef.current = identity.tier;
    if (prevTier === 'admin' && identity.tier === 'internal') {
      setPublicConfirmOpen(false);
      if (visibilityDraft === 'public' || existingMonitor?.visibility === 'public') {
        setVisibilityDraft(null);
        setPublicReselectRequired(true);
      }
    } else if (identity.tier === 'external') {
      setPublicConfirmOpen(false);
      setPublicReselectRequired(false);
      setMembersDraft(null);
      if (visibilityDraft !== null && visibilityDraft !== 'own') {
        setVisibilityDraft(null);
      }
    } else if (identity.tier === 'admin') {
      setPublicReselectRequired(false);
    }
  }, [identity.status, identity.tier, visibilityDraft, existingMonitor?.visibility]);
  // DALTON-HOOK(colleague-access): read workspace auto-publish pause state & CAS token only when colleague sharing is enabled
  const trackWorkspacePause =
    !unshareOnly && colleagueSharingCapable && colleagueSharingKnown && colleagueSharingEnabled;
  const trackPublishResponsePause =
    !unshareOnly && colleagueSharingCapable && (!colleagueSharingKnown || colleagueSharingEnabled);
  const workspaceQuery = useWorkspace(trackWorkspacePause ? workspaceId : null);
  const refetchWorkspace = workspaceQuery.refetch;
  useEffect(() => {
    if (trackWorkspacePause && workspaceId && refetchWorkspace) {
      void refetchWorkspace();
    }
  }, [trackWorkspacePause, workspaceId, refetchWorkspace]);
  const trackPublishResponsePauseRef = useRef(trackPublishResponsePause);
  useEffect(() => {
    trackPublishResponsePauseRef.current = trackPublishResponsePause;
  }, [trackPublishResponsePause]);
  // Publish response notice stays in component state until dialog close or the next submit.
  const [pauseNotice, setPauseNotice] = useState<string | null>(null);
  // Read cached workspace detail reactively without triggering a fetch when sharing status is unknown.
  const subscribeCachedDetail = useCallback(
    (onStoreChange: () => void) => {
      if (!trackPublishResponsePause || trackWorkspacePause) return () => {};
      return qc.getQueryCache().subscribe((event) => {
        if (detailWorkspaceIdFromKey(event.query.queryKey) === workspaceId) {
          onStoreChange();
        }
      });
    },
    [qc, workspaceId, trackPublishResponsePause, trackWorkspacePause],
  );
  const cachedDetail = useSyncExternalStore(
    subscribeCachedDetail,
    () =>
      trackPublishResponsePause && !trackWorkspacePause
        ? qc.getQueryData<Workspace>(queryKeys.workspaces.detail(workspaceId))
        : undefined,
  );
  const pendingPauseToken = useSyncExternalStore(
    subscribePendingPauseTokens,
    () => (trackPublishResponsePause ? (pendingPauseTokens.get(workspaceId) ?? null) : null),
  );
  // Placeholder data (useWorkspace seeds Home's detail from the Home query) is not
  // the workspace's detail yet, so it neither supersedes nor clears a pending token.
  const workspaceDetail = trackWorkspacePause
    ? workspaceQuery.isPlaceholderData
      ? undefined
      : workspaceQuery.data
    : cachedDetail;
  useEffect(() => {
    if (workspaceDetail !== undefined) {
      deletePendingPauseToken(workspaceId);
    }
  }, [workspaceDetail, workspaceId]);
  const autoPublishPaused = trackWorkspacePause
    ? workspaceDetail !== undefined
      ? Boolean(workspaceDetail.auto_publish_paused)
      : pendingPauseToken !== null
    : false;
  const showAutoPublishPausedHint = autoPublishPaused;
  const autoPublishPauseToken = trackPublishResponsePause
    ? workspaceDetail !== undefined
      ? (workspaceDetail.auto_publish_pause_token ?? null)
      : pendingPauseToken
    : null;
  const autoPublishPauseNotice = trackPublishResponsePause ? pauseNotice : null;

  const busy = publishingMonitor || downgradingToOwn || unshare.isPending || keepUpdated.isPending || audienceChange.isPending;
  useEffect(() => {
    busyRef.current = busy;
  }, [busy, busyRef]);

  // DALTON-HOOK(monitors): track whether this artifact already has a public share or monitor record
  const shared = !!artifact?.shared;
  const monitorPublished = !!existingMonitor;
  const status = statusQuery.data;
  scout.follow(status?.page_sha256);
  const statusRefusal = refusalIn(statusQuery.error);
  // A cap or a missing file ends the review; a file unreadable for now is
  // retried from the list.
  // Said where the list would be, so not again below it.
  const statusFailure = statusRefusal?.kind === 'unavailable' ? t('publish:unavailable')
    : statusRefusal?.kind === 'needsComputer' ? t('publish:needsComputer') : null;
  const blocking = statusFailure ? null : statusRefusal;
  // The page could not run to say what it loads: said and retried as a list
  // unreadable for now is.
  const listFailure = statusFailure ?? (scout.failed ? t('publish:unavailable') : null);
  // Past a cap the list still comes, for the owner to leave out what puts it over.
  const overCap = status?.over_cap ?? null;
  const refusal = blocking
    ?? (overCap ? { kind: 'cap' as const, cap: overCap } : null)
    ?? (outcome?.kind === 'refused' ? outcome.refusal : null);
  const files = outcome?.kind === 'conflict' && statusQuery.dataUpdatedAt <= outcome.statusAt
    ? outcome.files
    : status?.files ?? [];
  const drift = shared ? status?.drift ?? null : null;
  const added = new Set(drift?.added ?? []);
  const rootRefs = status?.root_refs ?? [];
  // A rule against public links refuses publishing; unsharing never is. A
  // page published before it keeps its row, and visitors wait for the rule.
  // A member held by the workspace's managers reads the file's link but
  // does not publish, update, unshare or keep it updated.
  const standing = useLinkStanding(workspaceId, {
    surface: 'file',
    linkOn: shared,
    audience: artifact?.audience,
    audiences: artifact?.audiences,
  });
  const { membersOnly, lockedOut } = standing;
  const blocked = standing.state !== 'open';
  const name = artifact?.name ?? basename(path);
  const effectiveTitle = titleDraft ?? existingMonitor?.title ?? name.replace(/\.html?$/i, '');
  const effectiveCategory: MonitorCategory = categoryDraft ?? existingMonitor?.category ?? 'other';
  // DALTON-HOOK(external-share-guard): enforce tier-aware visibility selection and block external republish on non-own monitors
  const externalNeedsDowngrade =
    !unshareOnly && isExternal && Boolean(existingMonitor && existingMonitor.visibility !== 'own');
  const rawVisibility: MonitorVisibility | null =
    publicReselectRequired && visibilityDraft === null
      ? null
      : (visibilityDraft ?? existingMonitor?.visibility ?? 'own');
  const effectiveVisibility: MonitorVisibility | null = isExternal
    ? 'own'
    : rawVisibility === null
      ? null
      : !isAdmin && rawVisibility === 'public'
        ? null
        : rawVisibility;
  const effectiveMembers = membersDraft ?? existingMonitor?.member_user_ids ?? [];
  const keepOn = isExternal
    ? false
    : (keepDraft ?? existingMonitor?.keep_updated ?? !!artifact?.keep_updated);

  const companyMembersQuery = useQuery<CompanyMemberOption[]>({
    queryKey: queryKeys.daltonMonitors.companyMembers(),
    queryFn: listCompanyMembers,
    enabled: !unshareOnly && identity.status === 'ready' && !isExternal && identity.canActAsOwner && effectiveVisibility === 'members',
    staleTime: 60_000,
  });

  const visibilityOptions = useMemo<SegmentedOption<MonitorVisibility>[]>(() => {
    if (isExternal) {
      return [{ value: 'own', label: t('monitors:visibility.own') }];
    }
    const base: SegmentedOption<MonitorVisibility>[] = [
      { value: 'own', label: t('monitors:visibility.own') },
      { value: 'members', label: t('monitors:visibility.members') },
      { value: 'company', label: t('monitors:visibility.company') },
    ];
    if (isAdmin) {
      base.push({ value: 'public', label: t('monitors:visibility.public') });
    }
    return base;
  }, [isExternal, isAdmin, t]);

  const membersInvalid = effectiveVisibility === 'members' && effectiveMembers.length === 0;
  // DALTON-HOOK(external-share-guard): fail closed while existing monitor state is pending or errored
  const listReady =
    !!status
    && !overCap
    && !statusQuery.isFetching
    && !statusQuery.isError
    && !scout.holding
    && !changingList
    && monitorStateReady;
  const canPublish =
    !unshareOnly
    && !lockedOut
    && !isIdentityBlocked
    && !externalNeedsDowngrade
    && effectiveVisibility !== null
    && (isExternal ? effectiveVisibility === 'own' : identity.canActAsOwner)
    && !!artifact
    && listReady
    && files.length > 0
    && !busy
    && !membersInvalid;
  const recordFailed = !!owned.error;
  const href = existingMonitor
    ? monitorShareUrl(existingMonitor.slug, existingMonitor.visibility)
    : artifact
      ? shareLinkHref(artifact.code)
      : '';
  const copied = existingMonitor ? copiedPermalink : (!!artifact && copiedCode === artifact.code);
  const canCopyLink =
    !unshareOnly
    && identity.status === 'ready'
    && !identity.isConfirming
    && identity.canActAsOwner
    && !isExternal;

  const failed = (err: unknown, fallback: string) => {
    console.error('[PublishDialog] Request failed:', err);
    if (isForbiddenError(err)) {
      void identity.revalidate();
    }
    const description =
      isForbiddenError(err) && apiErrorDetail(err) != null
        ? formatApiErrorDetail(err)
        : fallback;
    toast({ description, variant: 'destructive' });
  };

  // DALTON-HOOK(monitors): submit through /api/v1/dalton/monitors instead of bare /artifacts/{id}/publish
  const executeMonitorPublish = async () => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready') return;
    if (existingMonitorQuery.isPending || existingMonitorQuery.isError) return;
    if (!artifact || !canPublish || effectiveVisibility === null) return;
    if (latest.isExternal) {
      if (existingMonitor && existingMonitor.visibility !== 'own') return;
      if (effectiveVisibility !== 'own') return;
    } else if (!latest.canActAsOwner) {
      return;
    }
    if (effectiveVisibility === 'public' && !latest.canPublishPublic) return;
    const loads = pageLoadsOf(artifact.workspace_id, artifact.path);
    const statusAt = statusQuery.dataUpdatedAt;
    setOutcome(null);
    setPauseNotice(null);
    setPublishingMonitor(true);
    try {
      // DALTON-HOOK(colleague-access): include CAS confirm_pause_token and surface server pause notice
      const result = await publishMonitor({
        workspace_id: workspaceId,
        path: artifact.path,
        title: effectiveTitle.trim() || name,
        category: effectiveCategory,
        visibility: effectiveVisibility,
        member_user_ids: effectiveVisibility === 'members' ? effectiveMembers : [],
        keep_updated: latest.isExternal || !latest.canActAsOwner ? false : keepOn,
        label: label.trim() || null,
        files: files.map((f) => f.path),
        loaded: loads.now,
        earlier: loads.earlier,
        page_sha256: status?.page_sha256 ?? undefined,
        ...(trackPublishResponsePause && autoPublishPauseToken
          ? { confirm_pause_token: autoPublishPauseToken }
          : {}),
      });
      setLabel('');
      qc.setQueryData(queryKeys.daltonMonitors.byArtifact(artifact.artifact_id), result.monitor);
      void qc.invalidateQueries({ queryKey: queryKeys.daltonMonitors.all });
      void qc.invalidateQueries({ queryKey: queryKeys.artifacts.pathsIn(workspaceId) });
      if (trackPublishResponsePauseRef.current) {
        const detailKey = queryKeys.workspaces.detail(workspaceId);
        const paused = Boolean(result.auto_publish_paused);
        const token = paused ? (result.auto_publish_pause_token ?? null) : null;
        const notice = paused ? (result.auto_publish_pause_notice ?? null) : null;
        // Cancel any pre-publish detail GET (including a first cold fetch whose data is still undefined)
        // so its older payload never overwrites this response and invalidateQueries starts a fresh GET.
        await qc.cancelQueries({ queryKey: detailKey, exact: true });
        if (trackPublishResponsePauseRef.current) {
          setPauseNotice(notice);
          let mergedDetail = false;
          qc.setQueryData<Workspace>(detailKey, (old) => {
            if (!old) return old;
            mergedDetail = true;
            return {
              ...old,
              auto_publish_paused: paused,
              auto_publish_pause_token: token,
            };
          });
          qc.setQueriesData<WorkspacesResponse | undefined>(
            { queryKey: queryKeys.workspaces.lists() },
            (old) => {
              if (!old?.workspaces) return old;
              let matched = false;
              const workspaces = old.workspaces.map((row) => {
                if (row.workspace_id !== workspaceId) return row;
                matched = true;
                return {
                  ...row,
                  auto_publish_paused: paused,
                  auto_publish_pause_token: token,
                };
              });
              return matched ? { ...old, workspaces } : old;
            },
          );
          if (mergedDetail) {
            deletePendingPauseToken(workspaceId);
          } else if (token) {
            setPendingPauseToken(qc, workspaceId, token);
          } else {
            deletePendingPauseToken(workspaceId);
          }
          void qc.invalidateQueries({ queryKey: detailKey, exact: true });
          void qc.invalidateQueries({ queryKey: queryKeys.workspaces.lists() });
        }
      }
      if (result.cut === false) {
        setOutcome({ kind: 'unchanged', seq: result.monitor.published_seq });
      } else {
        toast({ description: t('monitors:dialog.publishedToast') });
      }
    } catch (err) {
      const changed = filesChangedIn(err);
      if (changed) {
        setOutcome({ kind: 'conflict', files: changed, statusAt });
        return;
      }
      const ref = refusalIn(err);
      if (ref) {
        setOutcome({ kind: 'refused', refusal: ref });
        return;
      }
      failed(err, t('publish:failed'));
    } finally {
      setPublishingMonitor(false);
    }
  };

  // DALTON-HOOK(monitors): route publish through executeMonitorPublish with public confirmation,
  // sync monitor keep_updated, downgrade non-own external monitors, and copy permalink with sync identity check.
  const handleVisibilityChange = (val: MonitorVisibility) => {
    setVisibilityDraft(val);
    setPublicReselectRequired(false);
  };

  const send = () => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready') return;
    if (existingMonitorQuery.isPending || existingMonitorQuery.isError) return;
    if (!artifact || !canPublish || effectiveVisibility === null) return;
    if (latest.isExternal) {
      if (existingMonitor && existingMonitor.visibility !== 'own') return;
      if (effectiveVisibility !== 'own') return;
    } else if (!latest.canActAsOwner) {
      return;
    }
    if (effectiveVisibility === 'public') {
      if (!latest.canPublishPublic) return;
      setPublicConfirmOpen(true);
      return;
    }
    void executeMonitorPublish();
  };

  const downgradeToOwn = async () => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready') return;
    if (!artifact || !existingMonitor || busy) return;
    setDowngradingToOwn(true);
    try {
      const res = await updateMonitor(existingMonitor.slug, { visibility: 'own', member_user_ids: [] });
      setVisibilityDraft('own');
      setMembersDraft([]);
      qc.setQueryData(queryKeys.daltonMonitors.byArtifact(artifact.artifact_id), res.monitor);
      void qc.invalidateQueries({ queryKey: queryKeys.daltonMonitors.all });
      void qc.invalidateQueries({ queryKey: queryKeys.artifacts.pathsIn(workspaceId) });
      void qc.invalidateQueries({ queryKey: queryKeys.shareLinks.byWorkspace(workspaceId) });
      toast({ description: t('monitors:dialog.downgradedToOwnToast') });
    } catch (err) {
      failed(err, t('publish:changeFailed'));
    } finally {
      setDowngradingToOwn(false);
    }
  };

  // DALTON-HOOK(monitors): stopping a share also refreshes monitor, path and share-link queries.
  const stop = () => {
    if (!artifact || busy) return;
    setOutcome((prev) => (prev?.kind === 'unchanged' ? null : prev));
    unshare.mutate(artifact.artifact_id, {
      onSuccess: () => {
        void qc.invalidateQueries({ queryKey: queryKeys.daltonMonitors.all });
        void qc.invalidateQueries({ queryKey: queryKeys.artifacts.pathsIn(workspaceId) });
        void qc.invalidateQueries({ queryKey: queryKeys.shareLinks.byWorkspace(workspaceId) });
      },
      onError: (err) => failed(err, t('publish:changeFailed')),
    });
  };

  const toggleKeep = () => {
    const latest = identity.getLatestIdentity();
    if (
      latest.isConfirming ||
      latest.status !== 'ready' ||
      latest.isExternal ||
      !latest.canActAsOwner
    ) {
      return;
    }
    if (!artifact || busy || !monitorStateReady) return;
    const next = !keepOn;
    setKeepDraft(next);
    if (existingMonitor) {
      void updateMonitor(existingMonitor.slug, { keep_updated: next })
        .then((res) => {
          qc.setQueryData(queryKeys.daltonMonitors.byArtifact(artifact.artifact_id), res.monitor);
          void qc.invalidateQueries({ queryKey: queryKeys.daltonMonitors.all });
        })
        .catch((err) => failed(err, t('publish:changeFailed')));
    }
  };

  // DALTON-HOOK(monitors): keep toggleMember helper alongside monitor keep-updated controls
  // DALTON-HOOK(external-share-guard): ignore member selection changes while identity is unconfirmed/confirming or busy
  const toggleMember = (memberId: string) => {
    const latest = identity.getLatestIdentity();
    if (
      busy ||
      latest.isConfirming ||
      latest.status !== 'ready' ||
      latest.isExternal ||
      !latest.canActAsOwner
    ) {
      return;
    }
    const next = effectiveMembers.includes(memberId)
      ? effectiveMembers.filter((id) => id !== memberId)
      : [...effectiveMembers, memberId];
    setMembersDraft(next);
  };

  const close = () => {
    if (!busy) onClose();
  };

  const copyCurrentLink = () => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) return;
    if (existingMonitor) {
      void navigator.clipboard.writeText(monitorShareUrl(existingMonitor.slug, existingMonitor.visibility)).then(() => {
        setCopiedPermalink(true);
        window.setTimeout(() => setCopiedPermalink(false), 2000);
      });
      return;
    }
    if (artifact) void copy(artifact.code);
  };

  const chooseAudience = (to: ShareAudience) => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) return;
    if (to === 'public' && !latest.canPublishPublic) return;
    if (!artifact || busy) return;
    audienceChange.mutate(
      { artifactId: artifact.artifact_id, audience: to },
      { onError: (err) => failed(err, t('publish:changeFailed')) },
    );
  };

  // DALTON-HOOK(monitors): summarize monitor publication state or legacy artifact state
  const legacyRefused = shared && !!artifact?.legacy_refused;
  let stateHint: string;
  if (standing.state === 'paused') {
    stateHint = t('organization.share.pausedShort', { whoMid: setterName(RULE.publicLinks).mid });
  } else if (existingMonitor) {
    stateHint = t('publish:publishedAt', { seq: existingMonitor.published_seq || latestSeq });
  } else if (!shared) {
    stateHint = latestSeq > 0 ? t('publish:privateWithVersions') : t('publish:privateHint');
  } else if (legacyRefused) {
    stateHint = t('publish:legacyRefused');
  } else if (visitorsAt === 0 || status?.changed == null) {
    stateHint = membersOnly ? orgT('organization.share.fileMembersHint') : t('publish:sharedHint');
  } else {
    stateHint = t('publish:changedSince', { seq: visitorsAt });
  }

  const listHeading = !monitorPublished && !shared
    ? t('publish:filesPublish', { count: files.length })
    : t('publish:filesUpdate', { count: files.length });

  let refusalText: string | null = null;
  if (refusal?.kind === 'cap') {
    refusalText = refusal.cap.code === 'too_many_bytes'
      ? t('publish:tooManyBytes', { size: formatBytes(refusal.cap.limit, locale) })
      : t('publish:tooManyFiles', { limit: refusal.cap.limit });
    // Said only where a box can bring it back under.
    if (overCap && files.some((f) => f.optional)) refusalText += ` ${t('publish:overCapLeaveOut')}`;
  } else if (refusal?.kind === 'missing') {
    refusalText = t('publish:missing');
  } else if (refusal?.kind === 'unavailable') {
    refusalText = t('publish:unavailable');
  } else if (refusal?.kind === 'needsComputer') {
    refusalText = t('publish:needsComputer');
  }

  return (
    <>
      {scout.element}
      <DialogTitle className="flex min-w-0 text-base font-semibold pr-6" style={{ color: 'var(--color-text-primary)' }} title={name}>
        <span className="truncate">{t('publish:title', { file: name })}</span>
      </DialogTitle>
      <DialogDescription className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
        {t('publish:description')}
      </DialogDescription>

      <div className="flex flex-col gap-4 pt-1">
        {/* DALTON-HOOK(external-share-guard): surface live permission confirmation or retryable error banner */}
        {identity.isConfirming && (
          <div
            role="status"
            className="flex items-center gap-2 rounded-md border px-3 py-2 text-xs"
            style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-text-secondary)' }}
          >
            <Loader size={12} className="text-(--color-text-tertiary)" />
            <span>{t('monitors:dialog.confirmingIdentity')}</span>
          </div>
        )}
        {!identity.isConfirming && identity.status === 'error' && (
          <div
            role="alert"
            className="flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs"
            style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-warning)' }}
          >
            <span>{t('monitors:dialog.identityFailed')}</span>
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => void identity.revalidate()}
            >
              {t('common.retry')}
            </Button>
          </div>
        )}
        {/* DALTON-HOOK(external-share-guard): surface retryable error when existing monitor status fails to load */}
        {!unshareOnly && existingMonitorFailed && (
          <div
            role="alert"
            className="flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs"
            style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-warning)' }}
          >
            <span>{t('monitors:loadFailed')}</span>
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => void existingMonitorQuery.refetch()}
              disabled={existingMonitorQuery.isFetching}
            >
              {t('common.retry')}
            </Button>
          </div>
        )}

        {/* The link — DALTON-HOOK(external-share-guard): hidden in unshareOnly and for external/unconfirmed callers */}
        {canCopyLink && (
        <div className="flex items-center gap-2">
          <div
            className="flex-1 flex items-center px-3 py-2 rounded-md text-xs min-w-0"
            style={{ backgroundColor: 'var(--color-bg-input)', color: 'var(--color-text-secondary)' }}
          >
            <span className="truncate font-mono" data-selectable>
              {href || (recordFailed ? t('publish:linkFailed') : '…')}
            </span>
          </div>
          {recordFailed ? (
            <Button variant="outline" size="sm" className="text-xs" onClick={owned.retry} disabled={owned.fetching}>
              {t('common.retry')}
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5 text-xs"
              onClick={copyCurrentLink}
              disabled={!artifact || isIdentityBlocked}
            >
              {copied
                ? <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} />
                : <Copy className="h-3.5 w-3.5" />}
              {copied ? t('publish:copied') : t('publish:copy')}
            </Button>
          )}
        </div>
        )}

        {/* DALTON-HOOK(monitors): monitor title, category, and 4-tier visibility controls */}
        {!unshareOnly && (
          <div className="flex flex-col gap-3 rounded-lg border p-3" style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-bg-card)' }}>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('monitors:dialog.titleField')}
                </span>
                <Input
                  value={effectiveTitle}
                  onChange={(e) => setTitleDraft(e.target.value)}
                  placeholder={t('monitors:dialog.titlePlaceholder')}
                  maxLength={300}
                  disabled={busy || isIdentityBlocked || externalNeedsDowngrade}
                  className="h-8 sm:text-xs bg-(--color-bg-input) border-(--color-border-muted) text-(--color-text-primary)"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('monitors:dialog.categoryField')}
                </span>
                <Select
                  value={effectiveCategory}
                  onChange={(e) => setCategoryDraft(e.target.value as MonitorCategory)}
                  disabled={busy || isIdentityBlocked || externalNeedsDowngrade}
                  style={{ height: '32px', fontSize: '0.75rem' }}
                >
                  {MONITOR_CATEGORIES.map((cat) => (
                    <option key={cat} value={cat}>
                      {t(`monitors:categories.${cat}`)}
                    </option>
                  ))}
                </Select>
              </label>
            </div>

            <div className="flex flex-col gap-1.5">
              <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                {t('monitors:dialog.visibilityField')}
              </span>
              {/* DALTON-HOOK(mobile-web): render 4-tier visibility as a 2x2 grid on phones so labels never clip */}
              {isMobile ? (
                <div
                  role="group"
                  aria-label={t('monitors:dialog.visibilityField')}
                  data-testid="publish-visibility-grid"
                  className="grid grid-cols-2 gap-1.5"
                >
                  {visibilityOptions.map((option) => {
                    const selected = option.value === effectiveVisibility;
                    return (
                      <button
                        key={option.value}
                        type="button"
                        aria-pressed={selected}
                        disabled={busy || isIdentityBlocked || externalNeedsDowngrade || option.disabled}
                        onClick={() => handleVisibilityChange(option.value)}
                        data-testid={option.testId}
                        className={cn(
                          'flex min-h-10 items-center justify-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-colors disabled:cursor-default',
                          option.disabled
                            ? 'text-(--color-text-quaternary)'
                            : selected
                              ? 'text-(--color-text-primary)'
                              : 'text-(--color-text-secondary)',
                        )}
                        style={{
                          borderColor: selected
                            ? 'var(--color-border-strong, var(--color-text-tertiary))'
                            : 'var(--color-border-muted)',
                          backgroundColor: selected
                            ? 'var(--color-shell-fill, var(--color-bg-tag))'
                            : 'transparent',
                        }}
                      >
                        {option.label}
                      </button>
                    );
                  })}
                </div>
              ) : (
                <SegmentedControl
                  value={(effectiveVisibility ?? '') as MonitorVisibility}
                  onChange={handleVisibilityChange}
                  options={visibilityOptions}
                  label={t('monitors:dialog.visibilityField')}
                  disabled={busy || isIdentityBlocked || externalNeedsDowngrade}
                  size="compact"
                />
              )}
              {isExternal && !externalNeedsDowngrade && (
                <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('monitors:dialog.externalOwnOnlyHint')}
                </p>
              )}
              {externalNeedsDowngrade && (
                <div
                  role="note"
                  className="flex flex-col gap-2 rounded-md border px-2.5 py-2 text-xs"
                  style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-warning)' }}
                >
                  <p>{t('monitors:dialog.externalDowngradeNotice')}</p>
                  <div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 gap-1.5 px-2.5 text-xs"
                      onClick={() => void downgradeToOwn()}
                      disabled={busy || isIdentityBlocked}
                    >
                      {downgradingToOwn && <Loader size={12} style={{ color: 'inherit' }} />}
                      {t('monitors:dialog.downgradeToOwn')}
                    </Button>
                  </div>
                </div>
              )}
              {publicReselectRequired && effectiveVisibility === null && (
                <p className="text-[0.6875rem]" role="note" style={{ color: 'var(--color-warning)' }}>
                  {t('monitors:dialog.adminDowngradedReselectHint')}
                </p>
              )}
              {identity.isInternal && !isAdmin && !publicReselectRequired && existingMonitor?.visibility === 'public' && effectiveVisibility === null && (
                <p className="text-[0.6875rem]" role="note" style={{ color: 'var(--color-warning)' }}>
                  {t('monitors:dialog.nonAdminPublicDowngradeHint')}
                </p>
              )}
            </div>

            {!isExternal && effectiveVisibility === 'members' && (
              <div className="flex flex-col gap-1.5">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('monitors:dialog.membersField')}
                </span>
                {companyMembersQuery.isLoading ? (
                  <div className="flex items-center gap-2 py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    <Loader size={14} />
                    {t('monitors:dialog.membersLoading')}
                  </div>
                ) : (companyMembersQuery.data ?? []).length === 0 ? (
                  <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('monitors:dialog.membersEmpty')}
                  </p>
                ) : (
                  <div
                    className="max-h-32 overflow-y-auto rounded-md border divide-y"
                    style={{ borderColor: 'var(--color-border-muted)' }}
                  >
                    {(companyMembersQuery.data ?? []).map((member) => {
                      const selected = effectiveMembers.includes(member.user_id);
                      const labelText = member.name
                        ? `${member.name} (${member.email || member.user_id})`
                        : member.email || member.user_id;
                      return (
                        <button
                          key={member.user_id}
                          type="button"
                          onClick={() => toggleMember(member.user_id)}
                          disabled={busy || isIdentityBlocked || externalNeedsDowngrade}
                          aria-pressed={selected}
                          className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left text-xs hover:bg-(--color-bg-hover) disabled:opacity-50 disabled:cursor-not-allowed"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          <span className="truncate">{labelText}</span>
                          {selected && <Check className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-success)' }} />}
                        </button>
                      );
                    })}
                  </div>
                )}
                {membersInvalid && (
                  <p className="text-[0.6875rem]" style={{ color: 'var(--color-warning)' }}>
                    {t('monitors:dialog.membersRequired')}
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {/* Where it stands, and keep updated */}
        <div
          className="flex flex-col rounded-lg border divide-y"
          style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-bg-card)' }}
        >
          <div className="flex items-center justify-between gap-3 px-3 py-2.5">
            {/* DALTON-HOOK(monitors): show monitor visibility badge or unshare control for legacy public links */}
            <div className="flex items-center gap-2 min-w-0">
              {shared || monitorPublished
                ? <Link2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
                : <Lock className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />}
              <div className="flex flex-col min-w-0">
                <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {monitorPublished
                    ? t(`monitors:visibility.${existingMonitor.visibility}`)
                    : !shared
                      ? t('publish:private')
                      : visitorsAt > 0 ? t('publish:publishedAt', { seq: visitorsAt }) : t('publish:published')}
                </span>
                <span className="text-xs" style={{ color: legacyRefused ? 'var(--color-warning)' : 'var(--color-text-tertiary)' }}>{stateHint}</span>
              </div>
            </div>
            {/* DALTON-HOOK(external-share-guard): stop button stays available for active shares and historical public monitors even while confirming */}
            {(shared || existingMonitor?.visibility === 'public') && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 shrink-0 gap-1.5 px-2.5 text-xs"
                onClick={stop}
                disabled={busy || lockedOut}
              >
                {unshare.isPending && <Loader size={12} style={{ color: 'inherit' }} />}
                {t('publish:unshare')}
              </Button>
            )}
          </div>
          {/* DALTON-HOOK(external-share-guard): hide audience picker in unshareOnly and for external/non-owner callers */}
          {!unshareOnly && !isExternal && identity.canActAsOwner && standing.picker && (
            <div className="px-3 py-2.5" style={{ borderColor: 'var(--color-border-muted)' }}>
              <AudiencePicker
                {...standing.picker}
                disabled={!artifact || busy || isIdentityBlocked}
                onChange={chooseAudience}
              />
            </div>
          )}
          {/* DALTON-HOOK(external-share-guard): keep-updated requires internal/admin owner access */}
          {!unshareOnly && !blocked && !isExternal && identity.canActAsOwner && (
          <div
            className="flex flex-col gap-1.5 px-3 py-2.5"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            <div className="flex items-center justify-between gap-3">
              <div className="flex flex-col min-w-0">
                <span className="text-sm" style={{ color: 'var(--color-text-primary)' }}>
                  {t('monitors:dialog.keepUpdated')}
                </span>
                <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('monitors:dialog.keepUpdatedHint')}
                </span>
              </div>
              {keepUpdated.isPending
                ? <Loader size={16} className="text-(--color-text-tertiary)" />
                : (
                  <ToggleSwitch
                    checked={keepOn}
                    onChange={toggleKeep}
                    disabled={!artifact || busy || isIdentityBlocked || !monitorStateReady || isExternal || !identity.canActAsOwner || (shared && lockedOut)}
                    ariaLabel={t('monitors:dialog.keepUpdated')}
                  />
                )}
            </div>
            {/* DALTON-HOOK(colleague-access): show auto-publish pause guidance under keep-updated toggle */}
            {showAutoPublishPausedHint && (
              <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
                {t('monitors:dialog.autoPublishPausedHint', {
                  action: monitorPublished ? t('publish:update') : t('publish:publish'),
                })}
              </p>
            )}
          </div>
          )}
        </div>

        <LinkRuleNote note={standing.note} />

        {/* DALTON-HOOK(external-share-guard): versions are the owner's review */}
        {!unshareOnly && latestSeq > 0 && <VersionList artifactId={artifact?.artifact_id} />}

        {/* Notices, about the list and the last attempt */}
        {/* DALTON-HOOK(colleague-access): display server auto_publish_pause_notice when pause stays active */}
        {autoPublishPauseNotice && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
            {autoPublishPauseNotice}
          </p>
        )}
        {outcome?.kind === 'conflict' && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
            {t('publish:filesChanged')}
          </p>
        )}
        {refusalText && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
            {refusalText}
          </p>
        )}
        {outcome?.kind === 'unchanged' && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('publish:unchanged', { seq: outcome.seq })}
          </p>
        )}
        {added.size > 0 && (
          <p
            className="rounded-lg border px-3 py-2.5 text-xs"
            style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-warning-soft)', color: 'var(--color-text-primary)' }}
          >
            {t('publish:driftAdded', { count: added.size })}
          </p>
        )}
        {rootRefs.length > 0 && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
            <Trans
              i18nKey="publish:rootRefs"
              values={{ example: rootRefs[0] }}
              components={{ path: <code className="break-all" />, cmd: <code className="whitespace-nowrap" /> }}
            />
          </p>
        )}

        {/* DALTON-HOOK(monitors): submit monitor review list and version note via send() */}
        {/* DALTON-HOOK(external-share-guard): lock file inclusion checkboxes during identity revalidation or without owner edit rights */}
        {!unshareOnly && !recordFailed && !blocking && !blocked && (
          <ReviewList
            artifactId={artifact?.artifact_id ?? ''}
            heading={status ? listHeading : null}
            status={status}
            files={files}
            added={added}
            failure={listFailure}
            locked={busy || isIdentityBlocked || isExternal || !identity.canActAsOwner}
            identity={identity}
            onLeftOutError={(err) => failed(err, t('publish:leftOutFailed'))}
            failed={statusQuery.isError || scout.failed}
            fetching={statusQuery.isFetching}
            onRetry={() => (scout.failed ? scout.retry() : void statusQuery.refetch())}
          />
        )}

        {/* DALTON-HOOK(monitors): disabled state syncs with publishingMonitor and send() handles Enter submit */}
        {!unshareOnly && !blocked && (
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
            {t('publish:label')}
          </span>
          <Input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send();
              }
            }}
            maxLength={LABEL_MAX}
            placeholder={t('publish:labelPlaceholder')}
            disabled={busy || !artifact}
            className="h-8 sm:text-xs bg-(--color-bg-input) border-(--color-border-muted) text-(--color-text-primary)"
          />
        </label>
        )}
        {/* DALTON-HOOK(external-share-guard): hide organization library publish row in unshareOnly or for external/unconfirmed callers */}
        {!unshareOnly && artifact && identity.status === 'ready' && identity.canActAsOwner && !isExternal && (
          <LibraryPublishRow
            target={{ kind: 'file', artifactId: artifact.artifact_id }}
            linkOn={shared}
            hint={t('organization.library.publishFileHint')}
          />
        )}

        {/* Footer */}
        <div className="flex items-center justify-between gap-3 pt-1">
          {/* DALTON-HOOK(external-share-guard): hide external monitor link in unshareOnly and for external/unconfirmed/confirming callers */}
          {!unshareOnly && existingMonitor && identity.status === 'ready' && !identity.isConfirming && identity.canActAsOwner && !isExternal ? (
            <a
              href={monitorShareUrl(existingMonitor.slug, existingMonitor.visibility)}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => {
                const latest = identity.getLatestIdentity();
                if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) {
                  e.preventDefault();
                }
              }}
              className="flex items-center gap-1.5 text-xs hover:underline"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t('monitors:openInNewWindow')}
            </a>
          ) : <span />}
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={close} disabled={busy}>
              {t('common.close')}
            </Button>
            {/* DALTON-HOOK(external-share-guard): no publish button here */}
            {!unshareOnly && !blocked && (
            <Button size="sm" className="gap-1.5" onClick={send} disabled={!canPublish}>
              {publishingMonitor && <Loader size={14} style={{ color: 'inherit' }} />}
              {monitorPublished ? t('publish:update') : t('publish:publish')}
            </Button>
            )}
          </div>
        </div>
      </div>

      {/* DALTON-HOOK(monitors): confirm dialog for public monitor link exposure */}
      {/* DALTON-HOOK(external-share-guard): disable public publish confirmation while identity is unknown/confirming */}
      <ConfirmDialog
        open={publicConfirmOpen}
        onOpenChange={setPublicConfirmOpen}
        title={t('monitors:dialog.publicConfirmTitle')}
        message={t('monitors:dialog.publicConfirmMessage')}
        confirmLabel={t('monitors:dialog.publicConfirmButton')}
        confirmDisabled={isIdentityBlocked || busy}
        onConfirm={executeMonitorPublish}
      />
    </>
  );
}

export default PublishDialog;
