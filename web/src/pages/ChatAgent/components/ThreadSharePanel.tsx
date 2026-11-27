import { useState, useCallback, type ElementType } from 'react';
import { Copy, Check, ExternalLink, Link2, Lock, FileText } from 'lucide-react'; // DALTON-HOOK(share-redaction)
import { useTranslation } from 'react-i18next';
// DALTON-HOOK(share-redaction): publishing boundary & hidden item count summary
import { ShareBoundaryNotice } from '@/components/dalton/RedactedSharePlaceholders';
import { apiErrorDetail, formatApiErrorDetail, isForbiddenError } from '@/api/errors'; // DALTON-HOOK(external-share-guard)
import { Loader } from '@/components/ui/loader'; // DALTON-HOOK(external-share-guard)
import { toast } from '@/components/ui/use-toast';
import { copyText } from '@/lib/clipboard';
import { shortMonthDay } from '@/lib/format';
import { useCopyShareLink } from '@/hooks/useCopyShareLink';
import { useLocale } from '@/hooks/useLocale';
import { useArtifactPublishing } from '@/hooks/useArtifact';
import { useSharedLinks, useShareLinkMutations } from '@/hooks/useShareLink';
// DALTON-HOOK(external-share-guard): unified caller identity with open/focus/403 revalidation
import { useCallerIdentity, type CallerIdentity } from '@/hooks/useCallerIdentity';
import { queryKeys } from '@/lib/queryKeys';
import { useQueryClient } from '@tanstack/react-query';
import {
  saveThreadSharing,
  useThreadShareStatus,
  type ThreadShareUpdateBody,
} from '../hooks/useThreadShareStatus';
import type { ShareLink, ThreadSharePermissions } from '../../../types/api';
import { useAccount } from '@/hooks/useAccount';
import { useLinkStanding } from '@/hooks/useLinkStanding';
import { LinkRuleNote } from '@/components/organization/LinkRuleNote';
import { AudiencePicker } from '@/components/organization/AudiencePicker';
import { LibraryPublishRow } from '@/components/organization/LibraryPublishRow';
import type { ShareAudience } from '@/types/api';

function sharedLinkName(link: ShareLink): string {
  return link.title || link.path?.split('/').pop() || link.code;
}

/** Every link the workspace has made public, each with Copy and Stop. */
function SharedInWorkspace({
  workspaceId,
  identity,
}: {
  workspaceId: string;
  // DALTON-HOOK(external-share-guard): share panel's unified identity controls copy visibility and 403 revalidation
  identity: CallerIdentity;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const queryClient = useQueryClient();
  const { data: links } = useSharedLinks(workspaceId);
  const { patch } = useShareLinkMutations(workspaceId);
  const { unshare } = useArtifactPublishing();
  const { copy, copiedCode } = useCopyShareLink();

  if (!links?.length) return null;

  const canCopyLinks = identity.status === 'ready' && identity.canActAsOwner && !identity.isExternal;
  const isConfirming = identity.isConfirming;

  const failed = (err: unknown) => {
    console.error('[ShareButton] Stop sharing failed:', err);
    if (isForbiddenError(err)) {
      void identity.revalidate();
    }
    const description =
      isForbiddenError(err) && apiErrorDetail(err) != null
        ? formatApiErrorDetail(err)
        : t('shareLink.shareFailed');
    toast({ description, variant: 'destructive' });
  };
  const copyLink = (code: string) => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) return;
    void copy(code);
  };
  // A file's link is its artifact's, whose unshare also refreshes the record
  // a file tab or the Publish dialog shows.
  // DALTON-HOOK(monitors): stopping a file link uniquely calls POST /artifacts/{id}/unshare and invalidates artifacts, shareLinks, and daltonMonitors caches
  const stopSharing = (link: ShareLink) => {
    if (link.kind === 'file' && link.artifact_id) {
      unshare.mutate(link.artifact_id, {
        onSuccess: () => {
          void queryClient.invalidateQueries({ queryKey: queryKeys.daltonMonitors.all });
          void queryClient.invalidateQueries({ queryKey: queryKeys.artifacts.pathsIn(workspaceId) });
          void queryClient.invalidateQueries({ queryKey: queryKeys.shareLinks.byWorkspace(workspaceId) });
        },
        onError: failed,
      });
    } else {
      patch.mutate({ code: link.code, patch: { shared: false } }, { onError: failed });
    }
  };

  return (
    <div className="pt-3 mt-3 border-t space-y-2" style={{ borderColor: 'var(--color-border-muted)' }}>
      <div className="flex items-center justify-between">
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('share.sharedInWorkspace')}
        </p>
        <span className="text-xs tabular-nums" style={{ color: 'var(--color-text-quaternary)' }}>{links.length}</span>
      </div>
      <ul className="max-h-48 overflow-y-auto space-y-1">
        {links.map((link) => (
          <li key={link.code} className="flex items-center gap-2 min-w-0">
            <FileText className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-icon-muted)' }} />
            <div className="flex-1 min-w-0">
              <p className="text-xs truncate" style={{ color: 'var(--color-text-primary)' }} title={link.path ?? undefined}>
                {sharedLinkName(link)}
              </p>
              {link.shared_at && (
                <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('share.sharedOn', { date: shortMonthDay(new Date(link.shared_at), locale) })}
                </p>
              )}
            </div>
            {canCopyLinks && (
              <button
                type="button"
                onClick={() => copyLink(link.code)}
                disabled={isConfirming}
                className="p-1 rounded-md shrink-0 disabled:opacity-50"
                style={{ color: copiedCode === link.code ? 'var(--color-success)' : 'var(--color-text-secondary)' }}
                title={t('share.copyLink')}
                aria-label={t('share.copyLink')}
              >
                {copiedCode === link.code ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              </button>
            )}
            <button
              type="button"
              onClick={() => stopSharing(link)}
              disabled={patch.isPending || unshare.isPending}
              className="text-xs px-1.5 py-0.5 rounded-md shrink-0 disabled:opacity-50 hover:underline"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {t('share.stop')}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface ThreadSharePanelProps {
  threadId: string;
  /** The workspace whose shared files the panel lists; null hides the list. */
  workspaceId?: string | null;
  /** DALTON-HOOK(external-share-guard): drive open/visibility/focus revalidation when hosted in a popover or dialog. */
  open?: boolean;
  /** What the heading renders as: a dialog passes its title so the dialog is named by it. */
  Heading?: ElementType;
}

/**
 * A thread's public link: the share toggle, the URL to copy, and what visitors
 * may open. The status is the thread's one cached entry, read again as the
 * panel opens, so a host mounts it only while it is shown (the chat header's
 * popover, the sidebar's share dialog), keyed by the thread.
 */
export function ThreadSharePanel({
  threadId,
  workspaceId = null,
  open = true,
  Heading = 'span',
}: ThreadSharePanelProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [updating, setUpdating] = useState(false);
  const { orgT } = useAccount();

  const queryClient = useQueryClient();
  // DALTON-HOOK(external-share-guard): unified caller identity with open/focus/403 revalidation
  const identity = useCallerIdentity({ revalidateOnOpen: true, open });
  const isExternal = identity.isExternal;
  const isIdentityBlocked = identity.status !== 'ready' || identity.isConfirming;
  // DALTON-HOOK(share-redaction): fetch redaction_summary only while panel is open
  const { data: shareState, isPending, isError, refetch } = useThreadShareStatus(threadId, {
    refetchOnMount: 'always',
    includeSummary: open,
  });

  const isShared = shareState?.is_shared === true;
  const permissions = shareState?.permissions || {};
  // A rule against public links refuses turning one on, never turning one
  // off: a link shared before it stays, paused, and opens again unchanged
  // once the rule lifts.
  const standing = useLinkStanding(workspaceId, {
    surface: 'thread',
    linkOn: isShared,
    audience: permissions.audience,
    audiences: shareState?.audiences,
  });
  const { membersOnly, lockedOut, keptIn, state } = standing;
  const paused = state === 'paused';

  const save = useCallback(async (body: ThreadShareUpdateBody) => {
    setUpdating(true);
    try {
      await saveThreadSharing(queryClient, threadId, body);
    } catch (e) {
      console.error('Failed to update sharing:', e);
      if (isForbiddenError(e)) {
        void identity.revalidate();
      }
      const description =
        isForbiddenError(e) && apiErrorDetail(e) != null
          ? formatApiErrorDetail(e)
          : t('share.updateFailed');
      toast({ description, variant: 'destructive' });
    } finally {
      setUpdating(false);
    }
  }, [queryClient, threadId, identity, t]);

  // DALTON-HOOK(external-share-guard): stopping a share sends `{ is_shared: false }` without `permissions`; starting/editing verifies latest identity synchronously and strips file permissions for non-admins
  const handleToggleShare = (enabled: boolean) => {
    if (!enabled) {
      return save({ is_shared: false });
    }
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) {
      return;
    }
    const rawPerms = shareState?.permissions || { allow_files: false, allow_download: false };
    const perms = latest.isAdmin
      ? rawPerms
      : { ...rawPerms, allow_files: false, allow_download: false };
    const toMembers = standing.turnsOnToMembers;
    return save({ is_shared: true, permissions: toMembers ? { ...perms, audience: 'organization' } : perms });
  };

  const handleAudience = (audience: ShareAudience) => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) {
      return;
    }
    if (audience === 'public' && !latest.canPublishPublic) {
      return;
    }
    return save({
      is_shared: shareState?.is_shared === true,
      permissions: { ...shareState?.permissions, audience },
    });
  };

  // DALTON-HOOK(external-share-guard): verify latest admin identity synchronously before toggling file permissions
  const handlePermissionChange = (key: keyof ThreadSharePermissions, value: boolean) => {
    const latest = identity.getLatestIdentity();
    if (
      latest.isConfirming ||
      latest.status !== 'ready' ||
      latest.isExternal ||
      !latest.canActAsOwner ||
      !latest.isAdmin
    ) {
      return;
    }
    const newPerms = { ...shareState?.permissions, [key]: value };
    // If disabling files, also disable download
    if (key === 'allow_files' && !value) {
      newPerms.allow_download = false;
    }
    return save({ is_shared: true, permissions: newPerms });
  };

  // DALTON-HOOK(external-share-guard): verify latest identity synchronously before copying thread share link
  const handleCopy = useCallback(() => {
    const latest = identity.getLatestIdentity();
    if (latest.isConfirming || latest.status !== 'ready' || latest.isExternal || !latest.canActAsOwner) return;
    if (!shareState?.share_url) return;
    const fullUrl = `${window.location.origin}${shareState.share_url}`;
    copyText(fullUrl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }, (e) => {
      console.error('Failed to copy share link:', e);
      toast({ description: t('filePanel:shareLinkFailed'), variant: 'destructive' });
    });
  }, [identity, shareState, t]);


  if (isPending) {
    return (
      <div className="px-4 py-6 text-center">
        <Heading className="sr-only">{t('share.shareConversation')}</Heading>
        <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>{t('common.loading')}</span>
      </div>
    );
  }

  // Without the status the toggle would show a guess, and turning it on would
  // send default permissions over the thread's own.
  if (isError && !shareState) {
    return (
      <div className="px-4 py-6 text-center space-y-2">
        <Heading className="sr-only">{t('share.shareConversation')}</Heading>
        <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>{t('share.statusLoadFailed')}</p>
        <button
          type="button"
          onClick={() => void refetch()}
          className="text-xs hover:underline"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {t('common.retry')}
        </button>
      </div>
    );
  }

  const audiencePicker = !isExternal && identity.canActAsOwner && standing.picker && (
    <AudiencePicker
      {...standing.picker}
      disabled={updating || lockedOut || isIdentityBlocked}
      onChange={(audience) => void handleAudience(audience)}
    />
  );

  const toggleDisabled = updating || lockedOut || (!isShared && isIdentityBlocked);
  // DALTON-HOOK(external-share-guard): hide share URL box while confirming or when caller lacks owner access
  const canShowShareUrl =
    !isExternal && identity.status === 'ready' && !identity.isConfirming && identity.canActAsOwner;

  return (
    <div className="p-4 space-y-3">
      {/* DALTON-HOOK(external-share-guard): show live permission confirmation or retryable error notice */}
      {identity.isConfirming && (
        <div
          role="status"
          className="flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs"
          style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-text-secondary)' }}
        >
          <Loader size={12} className="text-(--color-text-tertiary)" />
          <span>{t('share.confirmingIdentity')}</span>
        </div>
      )}
      {!identity.isConfirming && identity.status === 'error' && (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-xs"
          style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-warning)' }}
        >
          <span>{t('share.identityFailed')}</span>
          <button
            type="button"
            onClick={() => void identity.revalidate()}
            className="shrink-0 hover:underline"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('common.retry')}
          </button>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {isShared ? (
            <Link2 className="h-4 w-4" style={{ color: paused ? 'var(--color-text-tertiary)' : 'var(--color-accent-primary)' }} />
          ) : (
            <Lock className="h-4 w-4" style={{ color: 'var(--color-text-tertiary)' }} />
          )}
          <Heading className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {paused ? t('organization.share.paused')
              : isShared && membersOnly ? t('organization.share.membersLinkEnabled')
                : isShared ? t('share.publicLinkEnabled') : t('share.shareConversation')}
          </Heading>
        </div>
        {/* Toggle — DALTON-HOOK(external-share-guard): external members only see the switch to close an existing share; stopping stays clickable while confirming/error */}
        {(!isExternal || isShared) && (isShared || state !== 'blocked') && (
          <button
            type="button"
            role="switch"
            aria-checked={isShared}
            aria-label={t('share.shareConversation')}
            onClick={() => handleToggleShare(!isShared)}
            disabled={toggleDisabled}
            className="tap-target relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors disabled:cursor-not-allowed"
            style={{
              backgroundColor: isShared ? 'var(--color-accent-primary)' : 'var(--color-border-muted)',
              opacity: toggleDisabled ? 0.5 : 1,
            }}
          >
            <span
              className="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm ring-0 transition"
              style={{ transform: isShared ? 'translateX(16px)' : 'translateX(0)' }}
            />
          </button>
        )}
      </div>

      {!isShared && (
        isExternal ? (
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('share.externalReadOnlyHint')}
          </p>
        ) : (
          <>
            <LinkRuleNote note={standing.note} />
            {audiencePicker}
            {/* DALTON-HOOK(share-redaction): show publishing boundary & hidden item counts before enabling share */}
            <ShareBoundaryNotice allowFiles={permissions.allow_files === true} summary={shareState?.redaction_summary} />
          </>
        )
      )}

      {isShared && shareState && (
        isExternal ? (
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('share.externalStopOnlyHint')}
          </p>
        ) : (
        <>
          {/* Share URL — DALTON-HOOK(external-share-guard): hide URL/copy until identity is ready; disable copy while confirming */}
          {canShowShareUrl && (
          <div className="flex items-center gap-2">
            <div
              className="flex-1 flex items-center px-3 py-1.5 rounded-md text-xs truncate"
              style={{
                backgroundColor: 'var(--color-bg-input)',
                color: 'var(--color-text-secondary)',
              }}
            >
              <span className="truncate">
                {window.location.origin}{shareState.share_url}
              </span>
            </div>
            <button
              onClick={handleCopy}
              disabled={isIdentityBlocked}
              className="p-1.5 rounded-md transition-colors shrink-0 disabled:opacity-50"
              style={{ color: copied ? 'var(--color-success)' : 'var(--color-text-secondary)' }}
              onMouseEnter={(e) => { if (!isIdentityBlocked) e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = ''; }}
              title={t('share.copyLink')}
            >
              {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            </button>
          </div>
          )}
          {/* DALTON-HOOK(share-redaction): audience-specific preview link (visitor view for public, member view for organization) */}
          {canShowShareUrl && !paused && shareState.share_url && (() => {
            const isOrgAudience = permissions.audience === 'organization' || membersOnly;
            const previewUrl = isOrgAudience
              ? shareState.share_url
              : `${shareState.share_url}${shareState.share_url.includes('?') ? '&' : '?'}as=visitor`;
            return (
              <a
                href={previewUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-xs hover:underline"
                style={{ color: 'var(--color-accent-primary)' }}
              >
                <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                <span>{isOrgAudience ? t('share.previewMemberView') : t('share.previewVisitorView')}</span>
              </a>
            );
          })()}

          {paused && <LinkRuleNote note={standing.note} />}
          {audiencePicker}
          {/* DALTON-HOOK(external-share-guard): gate file permissions and library row on confirmed owner/admin identity */}
          {!paused && identity.canActAsOwner && (
          <>
          {/* Permissions */}
          <div className="space-y-2 pt-1">
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('share.permissions')}
            </p>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={permissions.allow_files || false}
                onChange={(e) => handlePermissionChange('allow_files', e.target.checked)}
                disabled={updating || lockedOut || isIdentityBlocked || !identity.isAdmin}
                className="rounded border-gray-300"
              />
              <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {t('share.allowFileBrowsing')}
              </span>
            </label>
            {permissions.allow_files && (
              <label className="flex items-center gap-2 cursor-pointer ml-4">
                <input
                  type="checkbox"
                  checked={permissions.allow_download || false}
                  onChange={(e) => handlePermissionChange('allow_download', e.target.checked)}
                  disabled={updating || lockedOut || isIdentityBlocked || !identity.isAdmin}
                  className="rounded border-gray-300"
                />
                <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('share.allowFileDownload')}
                </span>
              </label>
            )}
            {/* DALTON-HOOK(share-redaction): fixed publishing boundary and DB-only hidden item counts */}
            <ShareBoundaryNotice allowFiles={permissions.allow_files === true} summary={shareState.redaction_summary} />
          </div>

          {/* Info: under "no one" the workspace's line says who opens it. */}
          {!keptIn && (
          <p className="text-xs pt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            {membersOnly ? orgT('organization.share.membersCanView') : t('share.anyoneWithLink')}
          </p>
          )}
          <LinkRuleNote note={standing.note} />
          {/* DALTON-HOOK(external-share-guard): hide organization library publish row until identity is ready */}
          {identity.status === 'ready' && (
            <LibraryPublishRow
              target={{ kind: 'thread', threadId }}
              linkOn={isShared}
              hint={t('organization.library.publishThreadHint')}
            />
          )}
          </>
          )}
        </>
        )
      )}
      {/* DALTON-HOOK(external-share-guard): pass unified identity to workspace shared links list */}
      {workspaceId && <SharedInWorkspace workspaceId={workspaceId} identity={identity} />}
    </div>
  );
}
