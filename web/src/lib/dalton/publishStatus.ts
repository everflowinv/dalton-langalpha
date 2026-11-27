import type { MonitorVisibility } from '@/pages/Monitors/api';

export interface ActiveShareSummary {
  visibility: MonitorVisibility;
  published_seq: number;
}

export interface PublishStatusInput {
  unshareOnly: boolean;
  standingPaused: boolean;
  effectiveVisibility: MonitorVisibility | null;
  activeShare: ActiveShareSummary | null;
  shared: boolean;
  legacyRefused: boolean;
  membersOnly: boolean;
  latestSeq: number;
  visitorsAt: number;
  statusChanged: boolean | null | undefined;
  hasVisibilityDraft: boolean;
}

export interface ResolvedPublishStatus {
  showLinkIcon: boolean;
  titleKey: string;
  titleParams?: Record<string, string | number>;
  hintKind: 'paused' | 'orgMembers' | 'key';
  hintKey?: string;
  hintParams?: Record<string, string | number>;
  publishedVersionSeq: number | null;
}

export function visibilityHintKey(visibility: MonitorVisibility): string {
  switch (visibility) {
    case 'own':
      return 'publish:privateHint';
    case 'members':
      return 'publish:membersHint';
    case 'company':
      return 'publish:companyHint';
    case 'public':
      return 'publish:sharedHint';
  }
}

export function visibilityTitleKey(visibility: MonitorVisibility, isPublished: boolean): string {
  if (visibility === 'own') {
    return isPublished ? 'monitors:visibility.own' : 'publish:private';
  }
  return `monitors:visibility.${visibility}`;
}

export function resolvePublishStatus(input: PublishStatusInput): ResolvedPublishStatus {
  const {
    unshareOnly,
    standingPaused,
    effectiveVisibility,
    activeShare,
    shared,
    legacyRefused,
    membersOnly,
    latestSeq,
    visitorsAt,
    statusChanged,
    hasVisibilityDraft,
  } = input;

  const displayedVisibility: MonitorVisibility | null =
    (!unshareOnly ? effectiveVisibility : null) ?? activeShare?.visibility ?? null;
  const isPublished = Boolean(activeShare);
  const publishedSeq = activeShare ? (activeShare.published_seq || latestSeq || null) : null;

  if (standingPaused) {
    return {
      showLinkIcon: shared || isPublished || (displayedVisibility !== null && displayedVisibility !== 'own'),
      titleKey: displayedVisibility
        ? visibilityTitleKey(displayedVisibility, isPublished)
        : shared
          ? (visitorsAt > 0 ? 'publish:publishedAt' : 'publish:published')
          : 'publish:private',
      titleParams: !displayedVisibility && shared && visitorsAt > 0 ? { seq: visitorsAt } : undefined,
      hintKind: 'paused',
      publishedVersionSeq: null,
    };
  }

  // Upstream legacy shared artifact with no Dalton monitor/document record and no visibility override yet.
  if (shared && !activeShare && !hasVisibilityDraft) {
    const titleKey = visitorsAt > 0 ? 'publish:publishedAt' : 'publish:published';
    const titleParams = visitorsAt > 0 ? { seq: visitorsAt } : undefined;
    if (legacyRefused) {
      return {
        showLinkIcon: true,
        titleKey,
        titleParams,
        hintKind: 'key',
        hintKey: 'publish:legacyRefused',
        publishedVersionSeq: null,
      };
    }
    if (visitorsAt === 0 || statusChanged == null) {
      return {
        showLinkIcon: true,
        titleKey,
        titleParams,
        hintKind: membersOnly ? 'orgMembers' : 'key',
        hintKey: membersOnly ? undefined : 'publish:sharedHint',
        publishedVersionSeq: null,
      };
    }
    return {
      showLinkIcon: true,
      titleKey,
      titleParams,
      hintKind: 'key',
      hintKey: 'publish:changedSince',
      hintParams: { seq: visitorsAt },
      publishedVersionSeq: null,
    };
  }

  if (displayedVisibility !== null) {
    const showPrivateWithVersions =
      displayedVisibility === 'own' && !isPublished && latestSeq > 0;
    return {
      showLinkIcon: displayedVisibility !== 'own' || shared,
      titleKey: visibilityTitleKey(displayedVisibility, isPublished),
      hintKind: 'key',
      hintKey: showPrivateWithVersions
        ? 'publish:privateWithVersions'
        : visibilityHintKey(displayedVisibility),
      publishedVersionSeq: isPublished ? publishedSeq : null,
    };
  }

  return {
    showLinkIcon: shared,
    titleKey: 'publish:private',
    hintKind: 'key',
    hintKey: latestSeq > 0 ? 'publish:privateWithVersions' : 'publish:privateHint',
    publishedVersionSeq: null,
  };
}
