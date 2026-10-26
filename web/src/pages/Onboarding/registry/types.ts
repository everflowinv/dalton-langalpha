/**
 * A versioned feature announcement. Drives the "What's New" modal only — copy
 * is i18n keys under `onboarding.announce.*`. Adding one = this object + i18n
 * keys; no engine changes.
 */
export interface AnnouncementDef {
  /** Stable identifier (React keys, analytics). Unseen-ness is version-granular. */
  key: string;
  /**
   * CalVer ship date — 'YYYY.MM.DD' with an optional '.N' for same-day releases
   * (e.g. '2026.04.26'); just the date the feature ships, no git tag or GitHub
   * release required. The sole driver of unseen-ness: anything newer than the
   * user's lastSeenReleaseVersion shows, so a new entry must be strictly newer
   * than the current max or it will NOT surface (users are already stamped at
   * the max).
   */
  releaseVersion: string;
  modalTitleKey: string;
  modalBodyKey: string;
}

/**
 * Illustration scene rendered in the intro modal's visual panel. Implemented
 * in `engine/introVisuals.tsx` — the Record there is typed against this union,
 * so adding an id without a scene is a compile error.
 */
export type IntroVisualId =
  | 'twoModes'
  | 'workspaceGrid'
  | 'chiefOfStaffAnswer'
  | 'ptcSandbox'
  | 'createWorkspace'
  | 'filePanel'
  | 'memory'
  | 'memo'
  | 'dashboardGrid'
  | 'dashboardCustomize'
  | 'dashboardAttach';

/** One stage of a page intro. Copy is i18n keys under `onboarding.intros.*`. */
export interface PageIntroStepDef {
  /** Stable id — React key + i18n path segment. */
  id: string;
  titleKey: string;
  bodyKey: string;
  visual: IntroVisualId;
}

/**
 * A one-time contextual intro, shown the first time the user lands on a
 * matching page. Multi-stage: each step pairs copy with a mockup scene.
 * Seen-state is per-intro — closing at any step marks the whole intro seen.
 */
export interface PageIntroDef {
  /** Stable id — keys the per-user seen map in prefs. */
  id: string;
  /** Which pages this intro belongs to. */
  matchRoute: (pathname: string) => boolean;
  /** At least one step — the modal indexes steps[0], so empty would crash. */
  steps: [PageIntroStepDef, ...PageIntroStepDef[]];
}

/** A task on the getting-started checklist card. Copy is i18n keys. */
export interface GettingStartedTaskDef {
  id: string;
  titleKey: string;
  descKey: string;
  /** Where clicking the task navigates (router path, or a full URL when external). */
  to: string;
  /** Cross-app destination: opens in a new tab and is stamped done on click. */
  external?: boolean;
  /** Only offered on the hosted platform (e.g. channel integrations). */
  platformOnly?: boolean;
  /** Auto-completes when a visited location matches (stamped into prefs). */
  visitRoute?: (pathname: string, search: string) => boolean;
  /**
   * Derived completion signal — live done state without a prefs stamp.
   * `hasWorkspace`: at least one workspace other than Home exists (stamped
   * once true so the lookup stops re-running). `onboardingCompleted`: the
   * user row says the investing profile is saved (never stamped, so a reset
   * un-checks it).
   */
  doneWhen?: 'hasWorkspace' | 'onboardingCompleted';
}
