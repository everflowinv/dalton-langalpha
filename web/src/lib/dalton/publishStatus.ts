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
  pendingVisibility: MonitorVisibility | null;
  pendingHintKey: string | null;
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

  const activeVisibility: MonitorVisibility | null = activeShare?.visibility ?? null;
  const isPublished = Boolean(activeShare);
  const publishedSeq = activeShare ? (activeShare.published_seq || latestSeq || null) : null;
  const currentEffectiveVisibility: MonitorVisibility =
    activeVisibility ?? (shared ? (membersOnly ? 'company' : 'public') : 'own');
  const hasUnsavedChange =
    !unshareOnly &&
    !standingPaused &&
    effectiveVisibility !== null &&
    effectiveVisibility !== currentEffectiveVisibility &&
    (Boolean(activeShare) || !shared || hasVisibilityDraft);
  const pendingVisibility: MonitorVisibility | null = hasUnsavedChange ? effectiveVisibility : null;
  const pendingHintKey: string | null = pendingVisibility
    ? visibilityHintKey(pendingVisibility)
    : null;

  if (standingPaused) {
    return {
      showLinkIcon: shared || (activeVisibility !== null && activeVisibility !== 'own'),
      titleKey: activeVisibility
        ? visibilityTitleKey(activeVisibility, isPublished)
        : shared
          ? (visitorsAt > 0 ? 'publish:publishedAt' : 'publish:published')
          : 'publish:private',
      titleParams: !activeVisibility && shared && visitorsAt > 0 ? { seq: visitorsAt } : undefined,
      hintKind: 'paused',
      publishedVersionSeq: null,
      pendingVisibility: null,
      pendingHintKey: null,
    };
  }

  // Upstream legacy shared artifact with no Dalton monitor/document record:
  // always reflect the currently effective share state, and surface any unsaved
  // draft selection separately as a pending "after saving" preview.
  if (shared && !activeShare) {
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
        pendingVisibility,
        pendingHintKey,
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
        pendingVisibility,
        pendingHintKey,
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
      pendingVisibility,
      pendingHintKey,
    };
  }

  if (activeVisibility !== null) {
    return {
      showLinkIcon: activeVisibility !== 'own' || shared,
      titleKey: visibilityTitleKey(activeVisibility, isPublished),
      hintKind: 'key',
      hintKey: visibilityHintKey(activeVisibility),
      publishedVersionSeq: publishedSeq,
      pendingVisibility,
      pendingHintKey,
    };
  }

  return {
    showLinkIcon: false,
    titleKey: 'publish:private',
    hintKind: 'key',
    hintKey: latestSeq > 0 ? 'publish:privateWithVersions' : 'publish:privateHint',
    publishedVersionSeq: null,
    pendingVisibility,
    pendingHintKey,
  };
}
