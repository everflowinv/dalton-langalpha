// DALTON-HOOK(monitors): integrate 4-tier monitor publishing, ACLs, and admin public confirmation
import { useEffect, useId, useMemo, useRef, useState, type RefObject } from 'react';
import { Check, Copy, ExternalLink, Link2, Lock } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
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
import { isDaltonAuth } from '@/config/daltonAuth';
import { queryKeys } from '@/lib/queryKeys';
import { useCopyShareLink } from '@/hooks/useCopyShareLink';
import { useLocale } from '@/hooks/useLocale';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
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
import type { ArtifactStatus, ArtifactVersion, ShareFileEntry } from '@/types/api';
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

function FileRow({ artifactId, entry, added, leftOut, gutter, locked }: {
  artifactId: string;
  entry: ShareFileEntry;
  /** Read by the file now but in no version yet: what an Update would add. */
  added: boolean;
  /** Off what a publish carries, and on offer to put back. */
  leftOut: boolean;
  /** Some row of the list has a box, so the names line up past it. */
  gutter: boolean;
  /** A publish is under way with the list as it stands. */
  locked: boolean;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const toggle = useFileLeftOut(artifactId);
  const out = toggle.isPending ? toggle.variables.leftOut : leftOut;
  const dir = dirname(entry.path);
  const name = basename(entry.path);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <li
          tabIndex={0}
          className="flex items-center gap-3 px-3 py-1.5 text-xs outline-none hover:bg-(--color-bg-hover) focus-visible:bg-(--color-bg-hover)"
          data-selectable
        >
          {leftOut || entry.optional ? (
            <input
              type="checkbox"
              className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-(--color-accent-primary)"
              aria-label={t('publish.includeFile', { file: name })}
              checked={!out}
              disabled={locked || toggle.isPending}
              onChange={(e) => toggle.mutate(
                { path: entry.path, leftOut: !e.target.checked },
                { onError: () => toast({ description: t('publish.leftOutFailed'), variant: 'destructive' }) },
              )}
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
            className="shrink-0 w-24 text-right"
            style={{ color: added && !out ? 'var(--color-warning)' : 'var(--color-text-quaternary)' }}
          >
            {out ? t('publish.leftOut') : added ? t('publish.notShared') : t(`shareLink.reason.${entry.reason}`)}
          </span>
        </li>
      </TooltipTrigger>
      <TooltipContent side="top" align="start" className="max-w-md">
        <p className="wrap-anywhere">{entry.path}</p>
        <p className="mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
          {out
            ? t('publish.leftOutDetail')
            : t(`shareLink.reasonDetail.${entry.reason === 'loaded' && entry.optional ? 'loadedBefore' : entry.reason}`)}
        </p>
        {added && !out && <p className="mt-1" style={{ color: 'var(--color-warning)' }}>{t('publish.notSharedDetail')}</p>}
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
        {t('publish.version', { seq: version.seq })}
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
          {t('publish.keptUpdated')}
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
        {t('publish.versions')}
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
            {t('publish.versionsFailed')}
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
function ReviewList({ artifactId, heading, status, files, added, failure, failed, fetching, locked, onRetry }: {
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
        {heading ?? t('publish.listing')}
      </span>
      <div
        className={cn('rounded-lg border overflow-hidden', !status && !failed && 'py-4')}
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        {failed ? (
          <div className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
            <span style={{ color: 'var(--color-text-tertiary)' }}>
              {failure ?? t('publish.listFailed')}
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

function PublishReview({ workspaceId, path, busyRef, onClose, unshareOnly = false }: {
  workspaceId: string;
  path: string;
  busyRef: RefObject<boolean>;
  onClose: () => void;
  /** DALTON-HOOK(external-share-guard): stop-only mode, see the props above. */
  unshareOnly?: boolean;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const qc = useQueryClient();
  const root = useWorkspaceFolders()?.rootDir;
  // DALTON-HOOK(monitors): caller tier gates 4-tier visibility (external=own only, admin=public allowed)
  const { callerTier } = useTeamCredentials();
  const isExternal = callerTier === 'external';
  const isAdmin = callerTier === 'admin' || !isDaltonAuth;

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

  // DALTON-HOOK(monitors): load existing monitor config for this artifact if already published
  const existingMonitorQuery = useQuery<MonitorSummary | null>({
    queryKey: queryKeys.daltonMonitors.byArtifact(artifact?.artifact_id ?? ''),
    queryFn: () => getMonitorByArtifact(artifact!.artifact_id),
    enabled: !unshareOnly && Boolean(artifact?.artifact_id),
    staleTime: 10_000,
  });
  const existingMonitor = existingMonitorQuery.data ?? null;

  const [label, setLabel] = useState('');
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const [categoryDraft, setCategoryDraft] = useState<MonitorCategory | null>(null);
  const [visibilityDraft, setVisibilityDraft] = useState<MonitorVisibility | null>(null);
  const [membersDraft, setMembersDraft] = useState<string[] | null>(null);
  const [keepDraft, setKeepDraft] = useState<boolean | null>(null);
  const [publicConfirmOpen, setPublicConfirmOpen] = useState(false);
  const [publishingMonitor, setPublishingMonitor] = useState(false);
  const [copiedPermalink, setCopiedPermalink] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const busy = publishingMonitor || unshare.isPending || keepUpdated.isPending || audienceChange.isPending;
  useEffect(() => {
    busyRef.current = busy;
  }, [busy, busyRef]);

  // DALTON-HOOK(monitors): track whether this artifact already has a public share or monitor record
  const shared = !!artifact?.shared;
  const monitorPublished = !!existingMonitor;
  const status = statusQuery.data;
  scout.follow(status?.page_sha256);
  const statusRefusal = refusalIn(statusQuery.error);
  const statusFailure = statusRefusal?.kind === 'unavailable' ? t('publish.unavailable')
    : statusRefusal?.kind === 'needsComputer' ? t('publish.needsComputer') : null;
  const blocking = statusFailure ? null : statusRefusal;
  const listFailure = statusFailure ?? (scout.failed ? t('publish.unavailable') : null);
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
  const rawVisibility: MonitorVisibility = visibilityDraft ?? existingMonitor?.visibility ?? 'own';
  const effectiveVisibility: MonitorVisibility = isExternal
    ? 'own'
    : !isAdmin && rawVisibility === 'public'
      ? 'company'
      : rawVisibility;
  const effectiveMembers = membersDraft ?? existingMonitor?.member_user_ids ?? [];
  const keepOn = keepDraft ?? existingMonitor?.keep_updated ?? !!artifact?.keep_updated;

  const companyMembersQuery = useQuery<CompanyMemberOption[]>({
    queryKey: queryKeys.daltonMonitors.companyMembers(),
    queryFn: listCompanyMembers,
    enabled: !unshareOnly && !isExternal && effectiveVisibility === 'members',
    staleTime: 60_000,
  });

  const visibilityOptions = useMemo<SegmentedOption<MonitorVisibility>[]>(() => {
    if (isExternal) {
      return [{ value: 'own', label: t('monitors.visibility.own') }];
    }
    const base: SegmentedOption<MonitorVisibility>[] = [
      { value: 'own', label: t('monitors.visibility.own') },
      { value: 'members', label: t('monitors.visibility.members') },
      { value: 'company', label: t('monitors.visibility.company') },
    ];
    if (isAdmin) {
      base.push({ value: 'public', label: t('monitors.visibility.public') });
    }
    return base;
  }, [isExternal, isAdmin, t]);

  const membersInvalid = effectiveVisibility === 'members' && effectiveMembers.length === 0;
  const listReady = !!status && !overCap && !statusQuery.isFetching && !statusQuery.isError && !scout.holding && !changingList;
  const canPublish = !unshareOnly && !lockedOut && !!artifact && listReady && files.length > 0 && !busy && !membersInvalid;
  const recordFailed = !!owned.error;
  const href = existingMonitor
    ? monitorShareUrl(existingMonitor.slug, existingMonitor.visibility)
    : artifact
      ? shareLinkHref(artifact.code)
      : '';
  const copied = existingMonitor ? copiedPermalink : (!!artifact && copiedCode === artifact.code);

  const failed = (err: unknown, description: string) => {
    console.error('[PublishDialog] Request failed:', err);
    toast({ description, variant: 'destructive' });
  };

  // DALTON-HOOK(monitors): submit through /api/v1/dalton/monitors instead of bare /artifacts/{id}/publish
  const executeMonitorPublish = async () => {
    if (!artifact || !canPublish) return;
    const loads = pageLoadsOf(artifact.workspace_id, artifact.path);
    const statusAt = statusQuery.dataUpdatedAt;
    setOutcome(null);
    setPublishingMonitor(true);
    try {
      const result = await publishMonitor({
        workspace_id: workspaceId,
        path: artifact.path,
        title: effectiveTitle.trim() || name,
        category: effectiveCategory,
        visibility: effectiveVisibility,
        member_user_ids: effectiveVisibility === 'members' ? effectiveMembers : [],
        keep_updated: keepOn,
        label: label.trim() || null,
        files: files.map((f) => f.path),
        loaded: loads.now,
        earlier: loads.earlier,
        page_sha256: status?.page_sha256 ?? undefined,
      });
      setLabel('');
      qc.setQueryData(queryKeys.daltonMonitors.byArtifact(artifact.artifact_id), result.monitor);
      void qc.invalidateQueries({ queryKey: queryKeys.daltonMonitors.all });
      void qc.invalidateQueries({ queryKey: queryKeys.artifacts.pathsIn(workspaceId) });
      if (result.cut === false) {
        setOutcome({ kind: 'unchanged', seq: result.monitor.published_seq });
      } else {
        toast({ description: t('monitors.dialog.publishedToast') });
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
      failed(err, t('publish.failed'));
    } finally {
      setPublishingMonitor(false);
    }
  };

  // DALTON-HOOK(monitors): route publish through executeMonitorPublish with public confirmation,
  // sync monitor keep_updated, and copy monitor permalink when already published.
  const send = () => {
    if (!artifact || !canPublish) return;
    if (effectiveVisibility === 'public') {
      setPublicConfirmOpen(true);
      return;
    }
    void executeMonitorPublish();
  };

  const stop = () => {
    if (!artifact || busy) return;
    setOutcome((prev) => (prev?.kind === 'unchanged' ? null : prev));
    unshare.mutate(artifact.artifact_id, { onError: (err) => failed(err, t('publish.changeFailed')) });
  };

  const toggleKeep = () => {
    if (!artifact || busy) return;
    const next = !keepOn;
    setKeepDraft(next);
    if (existingMonitor) {
      void updateMonitor(existingMonitor.slug, { keep_updated: next })
        .then((res) => {
          qc.setQueryData(queryKeys.daltonMonitors.byArtifact(artifact.artifact_id), res.monitor);
          void qc.invalidateQueries({ queryKey: queryKeys.daltonMonitors.all });
        })
        .catch((err) => failed(err, t('publish.changeFailed')));
    }
  };

  const toggleMember = (memberId: string) => {
    const next = effectiveMembers.includes(memberId)
      ? effectiveMembers.filter((id) => id !== memberId)
      : [...effectiveMembers, memberId];
    setMembersDraft(next);
  };

  const close = () => {
    if (!busy) onClose();
  };

  const copyCurrentLink = () => {
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
    if (!artifact || busy) return;
    audienceChange.mutate(
      { artifactId: artifact.artifact_id, audience: to },
      { onError: (err) => failed(err, t('publish.changeFailed')) },
    );
  };

  // DALTON-HOOK(monitors): summarize monitor publication state or legacy artifact state
  const legacyRefused = shared && !!artifact?.legacy_refused;
  let stateHint: string;
  if (standing.state === 'paused') {
    stateHint = t('organization.share.pausedShort', { whoMid: setterName(RULE.publicLinks).mid });
  } else if (existingMonitor) {
    stateHint = t('publish.publishedAt', { seq: existingMonitor.published_seq || latestSeq });
  } else if (!shared) {
    stateHint = latestSeq > 0 ? t('publish.privateWithVersions') : t('publish.privateHint');
  } else if (legacyRefused) {
    stateHint = t('publish.legacyRefused');
  } else if (visitorsAt === 0 || status?.changed == null) {
    stateHint = membersOnly ? orgT('organization.share.fileMembersHint') : t('publish.sharedHint');
  } else {
    stateHint = t('publish.changedSince', { seq: visitorsAt });
  }

  const listHeading = !monitorPublished && !shared
    ? t('publish.filesPublish', { count: files.length })
    : t('publish.filesUpdate', { count: files.length });

  let refusalText: string | null = null;
  if (refusal?.kind === 'cap') {
    refusalText = refusal.cap.code === 'too_many_bytes'
      ? t('publish.tooManyBytes', { size: formatBytes(refusal.cap.limit, locale) })
      : t('publish.tooManyFiles', { limit: refusal.cap.limit });
    if (overCap && files.some((f) => f.optional)) refusalText += ` ${t('publish.overCapLeaveOut')}`;
  } else if (refusal?.kind === 'missing') {
    refusalText = t('publish.missing');
  } else if (refusal?.kind === 'unavailable') {
    refusalText = t('publish.unavailable');
  } else if (refusal?.kind === 'needsComputer') {
    refusalText = t('publish.needsComputer');
  }

  return (
    <>
      {scout.element}
      <DialogTitle className="flex min-w-0 text-base font-semibold pr-6" style={{ color: 'var(--color-text-primary)' }} title={name}>
        <span className="truncate">{t('publish.title', { file: name })}</span>
      </DialogTitle>
      <DialogDescription className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
        {t('publish.description')}
      </DialogDescription>

      <div className="flex flex-col gap-4 pt-1">
        {/* The link */}
        <div className="flex items-center gap-2">
          <div
            className="flex-1 flex items-center px-3 py-2 rounded-md text-xs min-w-0"
            style={{ backgroundColor: 'var(--color-bg-input)', color: 'var(--color-text-secondary)' }}
          >
            <span className="truncate font-mono" data-selectable>
              {href || (recordFailed ? t('publish.linkFailed') : '…')}
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
              disabled={!artifact}
            >
              {copied
                ? <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} />
                : <Copy className="h-3.5 w-3.5" />}
              {copied ? t('publish.copied') : t('publish.copy')}
            </Button>
          )}
        </div>

        {/* DALTON-HOOK(monitors): monitor title, category, and 4-tier visibility controls */}
        {!unshareOnly && (
          <div className="flex flex-col gap-3 rounded-lg border p-3" style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-bg-card)' }}>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('monitors.dialog.titleField')}
                </span>
                <Input
                  value={effectiveTitle}
                  onChange={(e) => setTitleDraft(e.target.value)}
                  placeholder={t('monitors.dialog.titlePlaceholder')}
                  maxLength={300}
                  disabled={busy}
                  className="h-8 sm:text-xs bg-(--color-bg-input) border-(--color-border-muted) text-(--color-text-primary)"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('monitors.dialog.categoryField')}
                </span>
                <Select
                  value={effectiveCategory}
                  onChange={(e) => setCategoryDraft(e.target.value as MonitorCategory)}
                  disabled={busy}
                  style={{ height: '32px', fontSize: '0.75rem' }}
                >
                  {MONITOR_CATEGORIES.map((cat) => (
                    <option key={cat} value={cat}>
                      {t(`monitors.categories.${cat}`)}
                    </option>
                  ))}
                </Select>
              </label>
            </div>

            <div className="flex flex-col gap-1.5">
              <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                {t('monitors.dialog.visibilityField')}
              </span>
              <SegmentedControl
                value={effectiveVisibility}
                onChange={(val) => setVisibilityDraft(val)}
                options={visibilityOptions}
                label={t('monitors.dialog.visibilityField')}
                disabled={busy}
                size="compact"
              />
              {isExternal && (
                <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('monitors.dialog.externalOwnOnlyHint')}
                </p>
              )}
              {!isExternal && !isAdmin && existingMonitor?.visibility === 'public' && (
                <p className="text-[0.6875rem]" role="note" style={{ color: 'var(--color-warning)' }}>
                  {t('monitors.dialog.nonAdminPublicDowngradeHint')}
                </p>
              )}
            </div>

            {!isExternal && effectiveVisibility === 'members' && (
              <div className="flex flex-col gap-1.5">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('monitors.dialog.membersField')}
                </span>
                {companyMembersQuery.isLoading ? (
                  <div className="flex items-center gap-2 py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    <Loader size={14} />
                    {t('monitors.dialog.membersLoading')}
                  </div>
                ) : (companyMembersQuery.data ?? []).length === 0 ? (
                  <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('monitors.dialog.membersEmpty')}
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
                          aria-pressed={selected}
                          className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left text-xs hover:bg-(--color-bg-hover)"
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
                    {t('monitors.dialog.membersRequired')}
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
                    ? t(`monitors.visibility.${existingMonitor.visibility}`)
                    : !shared
                      ? t('publish.private')
                      : visitorsAt > 0 ? t('publish.publishedAt', { seq: visitorsAt }) : t('publish.published')}
                </span>
                <span className="text-xs" style={{ color: legacyRefused ? 'var(--color-warning)' : 'var(--color-text-tertiary)' }}>{stateHint}</span>
              </div>
            </div>
            {shared && (unshareOnly || !existingMonitor) && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 shrink-0 gap-1.5 px-2.5 text-xs"
                onClick={stop}
                disabled={busy || lockedOut}
              >
                {unshare.isPending && <Loader size={12} style={{ color: 'inherit' }} />}
                {t('publish.unshare')}
              </Button>
            )}
          </div>
          {standing.picker && (
            <div className="px-3 py-2.5" style={{ borderColor: 'var(--color-border-muted)' }}>
              <AudiencePicker
                {...standing.picker}
                disabled={!artifact || busy}
                onChange={chooseAudience}
              />
            </div>
          )}
          {/* DALTON-HOOK(external-share-guard): keep-updated is the owner's */}
          {!unshareOnly && !blocked && (
          <div
            className="flex items-center justify-between gap-3 px-3 py-2.5"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            <div className="flex flex-col min-w-0">
              <span className="text-sm" style={{ color: 'var(--color-text-primary)' }}>
                {t('monitors.dialog.keepUpdated')}
              </span>
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('monitors.dialog.keepUpdatedHint')}
              </span>
            </div>
            {keepUpdated.isPending
              ? <Loader size={16} className="text-(--color-text-tertiary)" />
              : (
                <ToggleSwitch
                  checked={keepOn}
                  onChange={toggleKeep}
                  disabled={!artifact || busy || (shared && lockedOut)}
                  ariaLabel={t('monitors.dialog.keepUpdated')}
                />
              )}
          </div>
          )}
        </div>

        <LinkRuleNote note={standing.note} />

        {/* DALTON-HOOK(external-share-guard): versions are the owner's review */}
        {!unshareOnly && latestSeq > 0 && <VersionList artifactId={artifact?.artifact_id} />}

        {/* Notices, about the list and the last attempt */}
        {outcome?.kind === 'conflict' && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
            {t('publish.filesChanged')}
          </p>
        )}
        {refusalText && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
            {refusalText}
          </p>
        )}
        {outcome?.kind === 'unchanged' && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('publish.unchanged', { seq: outcome.seq })}
          </p>
        )}
        {added.size > 0 && (
          <p
            className="rounded-lg border px-3 py-2.5 text-xs"
            style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-warning-soft)', color: 'var(--color-text-primary)' }}
          >
            {t('publish.driftAdded', { count: added.size })}
          </p>
        )}
        {rootRefs.length > 0 && (
          <p className="text-xs" role="status" style={{ color: 'var(--color-warning)' }}>
            <Trans
              i18nKey="publish.rootRefs"
              values={{ example: rootRefs[0] }}
              components={{ path: <code className="break-all" />, cmd: <code className="whitespace-nowrap" /> }}
            />
          </p>
        )}

        {/* DALTON-HOOK(monitors): submit monitor review list and version note via send() */}
        {!unshareOnly && !recordFailed && !blocking && !blocked && (
          <ReviewList
            artifactId={artifact?.artifact_id ?? ''}
            heading={status ? listHeading : null}
            status={status}
            files={files}
            added={added}
            failure={listFailure}
            locked={busy}
            failed={statusQuery.isError || scout.failed}
            fetching={statusQuery.isFetching}
            onRetry={() => (scout.failed ? scout.retry() : void statusQuery.refetch())}
          />
        )}

        {/* DALTON-HOOK(monitors): disabled state syncs with publishingMonitor and send() handles Enter submit */}
        {!unshareOnly && !blocked && (
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
            {t('publish.label')}
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
            placeholder={t('publish.labelPlaceholder')}
            disabled={busy || !artifact}
            className="h-8 sm:text-xs bg-(--color-bg-input) border-(--color-border-muted) text-(--color-text-primary)"
          />
        </label>
        )}
        {artifact && (
          <LibraryPublishRow
            target={{ kind: 'file', artifactId: artifact.artifact_id }}
            linkOn={shared}
            hint={t('organization.library.publishFileHint')}
          />
        )}

        {/* Footer */}
        <div className="flex items-center justify-between gap-3 pt-1">
          {existingMonitor ? (
            <a
              href={monitorShareUrl(existingMonitor.slug, existingMonitor.visibility)}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 text-xs hover:underline"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t('monitors.openInNewWindow')}
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
              {monitorPublished ? t('publish.update') : t('publish.publish')}
            </Button>
            )}
          </div>
        </div>
      </div>

      {/* DALTON-HOOK(monitors): confirm dialog for public monitor link exposure */}
      <ConfirmDialog
        open={publicConfirmOpen}
        onOpenChange={setPublicConfirmOpen}
        title={t('monitors.dialog.publicConfirmTitle')}
        message={t('monitors.dialog.publicConfirmMessage')}
        confirmLabel={t('monitors.dialog.publicConfirmButton')}
        onConfirm={executeMonitorPublish}
      />
    </>
  );
}

export default PublishDialog;
