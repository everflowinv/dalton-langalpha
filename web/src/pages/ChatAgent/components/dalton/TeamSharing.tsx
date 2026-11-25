/* eslint-disable react-refresh/only-export-components */
import React, { useCallback, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Folder, Lock, Send } from 'lucide-react';
import { api } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
// Direct module imports: the primitives barrel also re-exports the
// confirm dialog, which would pull its animation library onto first load.
import { TOOLBAR_CHIP_CLASS, toolbarChipStyle } from '@/components/dalton/primitives/DaltonToolbarPill';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useLocale } from '@/hooks/useLocale';
import { useUser } from '@/hooks/useUser';
import { shortMonthDay } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import { cn } from '@/lib/utils';

export type TeamAccessMode = 'owner' | 'rw' | 'ro';

export interface TeamWorkspaceItem {
  workspace_id: string;
  user_id: string;
  owner_name: string;
  owner_email: string;
  name: string;
  description?: string | null;
  status?: string;
  is_home?: boolean;
  web_access?: 'rw' | 'ro' | 'hidden';
  cos_access?: 'rw' | 'ro' | 'hidden';
  access?: 'rw' | 'ro' | 'hidden';
  updated_at?: string | null;
  last_activity_at?: string | null;
  created_at?: string | null;
}

export interface TeamMemberGroup {
  user_id: string;
  email: string;
  display_name: string;
  name?: string;
  avatar_url?: string | null;
  workspaces: TeamWorkspaceItem[];
}

export interface TeamWorkspacesResponse {
  members: TeamMemberGroup[];
  caller_tier?: 'admin' | 'internal' | 'external';
  colleague_dispatch_enabled?: boolean;
}

export async function fetchTeamWorkspaces(): Promise<TeamWorkspacesResponse> {
  const { data } = await api.get<TeamWorkspacesResponse>('/api/v1/dalton/workspaces/team');
  return {
    members: Array.isArray(data?.members) ? data.members : [],
    caller_tier: data?.caller_tier,
    colleague_dispatch_enabled: data?.colleague_dispatch_enabled,
  };
}

export function useTeamWorkspaces(enabled = true) {
  const identity = useCallerIdentity();
  const { hasCapability } = useCapabilities();
  return useQuery({
    queryKey: queryKeys.workspaces.team(),
    queryFn: fetchTeamWorkspaces,
    enabled:
      enabled &&
      !identity.isPlatform &&
      (isDaltonAuth || identity.isMultiUserAuth) &&
      identity.isInternal &&
      hasCapability('colleague_sharing'),
    staleTime: 60_000,
    retry: false,
  });
}

export interface TeamReadOnlyState {
  access: TeamAccessMode;
  isColleague: boolean;
  isReadOnly: boolean;
  canWriteFiles: boolean;
  colleagueDispatchEnabled: boolean;
  callerTier?: 'admin' | 'internal' | 'external' | string | null;
  isThreadOnly?: boolean;
  ownerName: string | null;
  ownerEmail: string | null;
}

/**
 * Resolve the caller's 3-state access (`owner` | `rw` | `ro`) on the active workspace/thread.
 * Active only when `isDaltonAuth` is true.
 */
export function useTeamReadOnly(
  workspaceId: string | null | undefined,
  workspaceRecord?: {
    user_id?: string | null;
    name?: string | null;
    effective_web_access?: string | null;
    colleague_dispatch_enabled?: boolean | null;
  } | null,
): TeamReadOnlyState {
  const location = useLocation();
  const queryClient = useQueryClient();
  const identity = useCallerIdentity();
  const { user: currentUser } = useUser({ enabled: Boolean(workspaceId) && isDaltonAuth });
  const { data: teamData } = useTeamWorkspaces(Boolean(workspaceId));
  const callerTier = identity.isPlatform
    ? null
    : identity.status === 'ready'
      ? identity.tier
      : 'unknown';

  if (!isDaltonAuth || !workspaceId) {
    return {
      access: 'owner',
      isColleague: false,
      isReadOnly: false,
      canWriteFiles: true,
      colleagueDispatchEnabled: false,
      callerTier,
      isThreadOnly: false,
      ownerName: null,
      ownerEmail: null,
    };
  }

  const navState = (location.state ?? null) as {
    isTeamReadOnly?: boolean;
    teamAccess?: 'rw' | 'ro';
    teamOwnerName?: string;
    teamOwnerEmail?: string;
  } | null;

  const sharingData = queryClient.getQueryData<{ colleague_dispatch_enabled?: boolean }>(
    queryKeys.dalton.sharing(workspaceId),
  );
  const colleagueDispatchEnabled = Boolean(
    workspaceRecord?.colleague_dispatch_enabled ??
      sharingData?.colleague_dispatch_enabled ??
      teamData?.colleague_dispatch_enabled ??
      false,
  );

  const effectiveAccess = workspaceRecord?.effective_web_access ?? null;
  const isThreadOnly = effectiveAccess === 'thread_only';

  const members = teamData?.members ?? [];
  for (const member of members) {
    const matched = member.workspaces.find((ws) => ws.workspace_id === workspaceId);
    if (matched) {
      const display =
        member.display_name ||
        matched.owner_name ||
        member.email ||
        matched.owner_email ||
        '同事';
      const rawAccess =
        (effectiveAccess === 'rw' || effectiveAccess === 'ro' ? effectiveAccess : null) ||
        matched.web_access ||
        matched.access ||
        navState?.teamAccess ||
        'ro';
      const mode: TeamAccessMode = !isThreadOnly && rawAccess === 'rw' ? 'rw' : 'ro';
      return {
        access: mode,
        isColleague: true,
        isReadOnly: mode === 'ro',
        canWriteFiles: mode === 'rw',
        colleagueDispatchEnabled,
        callerTier,
        isThreadOnly,
        ownerName: display,
        ownerEmail: member.email || matched.owner_email || null,
      };
    }
  }

  const wsOwnerId = workspaceRecord?.user_id ?? null;
  const currentUserId = currentUser?.user_id ?? null;
  if (wsOwnerId && currentUserId && wsOwnerId !== currentUserId) {
    const matchedMember = members.find((m) => m.user_id === wsOwnerId);
    const display =
      matchedMember?.display_name ||
      matchedMember?.email ||
      navState?.teamOwnerName ||
      wsOwnerId;
    const rawAccess =
      (effectiveAccess === 'rw' || effectiveAccess === 'ro' ? effectiveAccess : null) ||
      navState?.teamAccess ||
      'ro';
    const mode: TeamAccessMode = !isThreadOnly && rawAccess === 'rw' ? 'rw' : 'ro';
    return {
      access: mode,
      isColleague: true,
      isReadOnly: mode === 'ro',
      canWriteFiles: mode === 'rw',
      colleagueDispatchEnabled,
      callerTier,
      isThreadOnly,
      ownerName: display,
      ownerEmail: matchedMember?.email || navState?.teamOwnerEmail || null,
    };
  }

  if (navState?.isTeamReadOnly) {
    const mode: TeamAccessMode = !isThreadOnly && navState.teamAccess === 'rw' ? 'rw' : 'ro';
    return {
      access: mode,
      isColleague: true,
      isReadOnly: mode === 'ro',
      canWriteFiles: mode === 'rw',
      colleagueDispatchEnabled,
      callerTier,
      isThreadOnly,
      ownerName: navState.teamOwnerName || '同事',
      ownerEmail: navState.teamOwnerEmail || null,
    };
  }

  // Default to read-only (fail-closed) until both the workspace row and current
  // user have loaded and confirmed ownership.
  if (!wsOwnerId || !currentUserId) {
    return {
      access: 'ro',
      isColleague: true,
      isReadOnly: true,
      canWriteFiles: false,
      colleagueDispatchEnabled,
      callerTier,
      isThreadOnly,
      ownerName: navState?.teamOwnerName || '同事',
      ownerEmail: navState?.teamOwnerEmail || null,
    };
  }

  return {
    access: 'owner',
    isColleague: false,
    isReadOnly: false,
    canWriteFiles: true,
    colleagueDispatchEnabled,
    callerTier,
    isThreadOnly: false,
    ownerName: null,
    ownerEmail: null,
  };
}

function formatShortTime(iso: string | null | undefined, locale: string): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return shortMonthDay(date, locale);
}

const CHEVRON_CLASS = 'h-3.5 w-3.5 transition-transform duration-fold';

/**
 * Sidebar collapsible "团队" section below "我的工作区".
 * Grouped by colleague, each colleague group collapsed by default.
 */
export function TeamWorkspacesSection({
  currentWorkspaceId,
  onNavigate,
}: {
  currentWorkspaceId?: string | null;
  onNavigate?: () => void;
}): React.ReactElement | null {
  const { t } = useTranslation();
  const locale = useLocale();
  const navigate = useNavigate();
  const { data } = useTeamWorkspaces();
  const identity = useCallerIdentity();
  const { hasCapability } = useCapabilities();
  const [sectionExpanded, setSectionExpanded] = useState(true);
  const [expandedMembers, setExpandedMembers] = useState<Set<string>>(() => new Set());

  const toggleMember = useCallback((userId: string) => {
    setExpandedMembers((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) {
        next.delete(userId);
      } else {
        next.add(userId);
      }
      return next;
    });
  }, []);

  if (!isDaltonAuth) {
    return null;
  }

  // Render only for confirmed internal/admin callers: external members are
  // fully isolated, and while the tier is still loading (or the query failed)
  // the section stays hidden rather than flashing a placeholder.
  if (
    !hasCapability('colleague_sharing') ||
    !identity.isInternal ||
    !data
  ) {
    return null;
  }

  const members = data?.members ?? [];

  return (
    <div
      className="sidebar-team-section shrink-0 mt-2 pt-2 pb-2 rule-t"
      data-testid="team-workspaces-section"
    >
      <div className="sidebar-section-header">
        <button
          type="button"
          className="sidebar-section-title"
          onClick={() => setSectionExpanded((v) => !v)}
          aria-expanded={sectionExpanded}
          data-testid="team-section-toggle"
        >
          {t('dalton.sharing.teamSectionTitle')}
        </button>
        <div className="sidebar-section-actions">
          <button
            type="button"
            className="nav-panel-dismiss-btn sidebar-section-action-btn tap-target"
            onClick={() => setSectionExpanded((v) => !v)}
            aria-label={t('dalton.sharing.teamSectionTitle')}
          >
            <ChevronRight
              className={cn(CHEVRON_CLASS, sectionExpanded && 'rotate-90')}
              style={{ color: 'var(--color-text-tertiary)' }}
            />
          </button>
        </div>
      </div>

      {sectionExpanded && (
        <div className="flex flex-col py-1" data-testid="team-members-list">
          {members.length === 0 ? (
            <div
              className="px-3 py-1.5 text-xs"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {t('dalton.sharing.teamEmpty')}
            </div>
          ) : (
            members.map((member) => {
              const isExpanded = expandedMembers.has(member.user_id);
              const displayLabel = member.display_name || member.email;
              return (
                <div
                  key={member.user_id}
                  className="flex flex-col"
                  data-testid={`team-member-group-${member.user_id}`}
                >
                  <button
                    type="button"
                    onClick={() => toggleMember(member.user_id)}
                    aria-expanded={isExpanded}
                    data-testid={`team-member-toggle-${member.user_id}`}
                    className="nav-panel-row w-full text-left"
                    style={{ paddingLeft: 10 }}
                  >
                    <ChevronRight
                      className={cn(CHEVRON_CLASS, 'shrink-0', isExpanded && 'rotate-90')}
                      style={{ color: 'var(--color-text-tertiary)' }}
                    />
                    <span
                      className="text-sm font-medium truncate"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {displayLabel}
                    </span>
                    <span
                      className="ml-auto text-[11px] shrink-0 tabular-nums"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {member.workspaces.length}
                    </span>
                  </button>

                  {isExpanded && (
                    <div
                      className="flex flex-col"
                      data-testid={`team-member-workspaces-${member.user_id}`}
                    >
                      {member.workspaces.map((ws) => {
                        const active = currentWorkspaceId === ws.workspace_id;
                        const timeLabel = formatShortTime(ws.last_activity_at || ws.updated_at, locale);
                        const wsAccess = (ws.web_access || ws.access) === 'rw' ? 'rw' : 'ro';
                        const accessBadge =
                          wsAccess === 'rw'
                            ? t('dalton.sharing.levelRw')
                            : t('dalton.sharing.levelRo');
                        return (
                          <button
                            key={ws.workspace_id}
                            type="button"
                            data-testid={`team-workspace-item-${ws.workspace_id}`}
                            onClick={() => {
                              navigate(`/chat/${ws.workspace_id}`, {
                                state: {
                                  workspaceName: ws.name,
                                  workspaceStatus: 'stopped',
                                  isTeamReadOnly: true,
                                  teamAccess: wsAccess,
                                  teamOwnerName: displayLabel,
                                  teamOwnerEmail: member.email,
                                },
                              });
                              onNavigate?.();
                            }}
                            className={cn(
                              'nav-panel-row w-full text-left',
                              active && 'nav-panel-row-active',
                            )}
                            style={{ paddingLeft: 24 }}
                          >
                            <Folder
                              className="h-4 w-4 shrink-0"
                              style={{ color: 'var(--color-text-tertiary)' }}
                            />
                            <span
                              className="text-sm font-medium truncate min-w-0 flex-1"
                              style={{
                                color: active
                                  ? 'var(--color-text-primary)'
                                  : 'var(--color-text-tertiary)',
                              }}
                            >
                              {ws.name}
                            </span>
                            <span className="flex items-center gap-1.5 shrink-0 ml-auto">
                              <span
                                data-testid={`team-workspace-access-${ws.workspace_id}`}
                                data-bracket="chip"
                                className="text-[10px] px-1.5 py-0.5 rounded-full border"
                                style={{
                                  borderColor: 'var(--color-border-muted)',
                                  color: 'var(--color-text-tertiary)',
                                }}
                              >
                                {accessBadge}
                              </span>
                              {timeLabel && (
                                <span
                                  className="text-[11px] shrink-0 tabular-nums"
                                  style={{ color: 'var(--color-text-tertiary)' }}
                                >
                                  {timeLabel}
                                </span>
                              )}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Top banner shown when viewing a teammate's workspace or thread.
 */
export function TeamReadOnlyBanner({
  ownerName,
  access = 'ro',
  compact = false,
}: {
  ownerName: string | null;
  access?: TeamAccessMode;
  compact?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const modeLabel =
    access === 'rw' ? t('dalton.sharing.levelRw') : t('dalton.sharing.levelRo');
  const label = t('dalton.sharing.bannerLabel', {
    mode: modeLabel,
    name: ownerName || t('dalton.sharing.colleagueFallback'),
  });
  return (
    <div
      data-testid="team-readonly-banner"
      data-compact={compact ? 'true' : undefined}
      role="status"
      title={label}
      className={cn(
        TOOLBAR_CHIP_CLASS,
        compact && 'h-6 max-w-full min-w-0 px-2 text-[11px]',
      )}
      style={toolbarChipStyle('neutral')}
    >
      <Lock className="h-3 w-3 shrink-0" />
      <span className={compact ? 'truncate' : undefined}>{label}</span>
    </div>
  );
}

/**
 * Disabled composer replacing the writable ChatInput when viewing a teammate's workspace/thread
 * or when viewing a thread dispatched by a colleague.
 */
export function TeamReadOnlyComposer({
  ownerName,
  access = 'ro',
  dispatchedByName,
  revoked = false,
  colleagueDispatchEnabled = true,
}: {
  ownerName: string | null;
  access?: TeamAccessMode;
  dispatchedByName?: string | null;
  revoked?: boolean;
  colleagueDispatchEnabled?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const name = ownerName || t('dalton.sharing.colleagueFallback');
  const label = revoked
    ? t('dalton.sharing.revokedThreadReadonlyHint')
    : dispatchedByName
      ? t('dalton.sharing.dispatchedThreadReadonlyHint', { name: dispatchedByName })
      : access === 'rw'
        ? colleagueDispatchEnabled
          ? t('dalton.sharing.rwDispatchReadonlyHint', { name })
          : t('dalton.sharing.rwReadonlyHint', { name })
        : t('dalton.sharing.roReadonlyHint', { name });
  const buttonTitle =
    access === 'rw' && !colleagueDispatchEnabled
      ? t('dalton.sharing.rwSendDisabledTitle')
      : t('dalton.sharing.readonlySendTitle');
  return (
    <div
      data-testid="team-readonly-composer"
      className="w-full rounded-[var(--cs-composer-radius,var(--radius-xl))] border px-3 py-2.5 flex items-center justify-between gap-2 opacity-75"
      style={{
        borderColor: 'var(--color-border-muted)',
        backgroundColor: 'var(--color-bg-input)',
      }}
    >
      <Input
        type="text"
        disabled
        readOnly
        value=""
        placeholder={label}
        aria-label={t('dalton.sharing.readonlyInputLabel')}
        className="flex-1 h-auto bg-transparent text-xs border-0 px-0 py-0 shadow-none focus-visible:ring-0 disabled:opacity-100 cursor-not-allowed"
        style={{ color: 'var(--color-text-tertiary)' }}
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        disabled
        aria-label={t('dalton.sharing.sendLabel')}
        title={buttonTitle}
        className="h-auto w-auto p-1.5 rounded-md cursor-not-allowed opacity-50"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        <Send className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}
