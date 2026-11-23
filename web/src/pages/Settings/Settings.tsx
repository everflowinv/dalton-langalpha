import { useEffect, useRef } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router';
import { useScrollMemory } from '@/lib/scrollMemory';
import { useUser } from '@/hooks/useUser';
import { usePreferences } from '@/hooks/usePreferences';
import { useTranslation } from 'react-i18next';
import { BRAND_NAME } from '@/config/brand'; // DALTON-HOOK(brand-name)
import { isDaltonAuth } from '@/config/daltonAuth'; // DALTON-HOOK(settings-reorg)
import { SwapCell } from '@/components/ui/crossfade';
import { SkeletonBar } from '@/components/ui/skeleton';
import { AccountPanel } from './panels/AccountPanel';
import { InvestingProfilePanel } from './panels/InvestingProfilePanel';
import { GeneralPanel } from './panels/GeneralPanel';
import { ChatPanel } from './panels/ChatPanel';
import { ComputersPanel } from './panels/ComputersPanel';
import { ModelsPanel } from './panels/ModelsPanel';
import { IntegrationsPanel } from './panels/IntegrationsPanel';
// DALTON-HOOK(settings-reorg): consolidated Dalton settings panels & permission guards
import { DaltonBudgetPanel } from './panels/dalton/DaltonBudgetPanel';
import { DaltonCoveragePanel } from './panels/dalton/DaltonCoveragePanel';
import { DaltonTeamModelsPanel } from './panels/dalton/DaltonTeamModelsPanel';
import { DaltonTeamMembersPanel } from './panels/dalton/DaltonTeamMembersPanel';
import { DaltonTeamOpsPanel } from './panels/dalton/DaltonTeamOpsPanel';
import { DaltonInlineNotice } from '@/components/dalton/primitives';
import { SettingsRail, useDaltonSettingsVisibility } from './components/SettingsRail';
import ComputersDialogHost from '@/pages/ChatAgent/components/ComputersDialogHost';
import { useCloseComputersOnLeave } from '@/pages/ChatAgent/hooks/computerPanelStore';
import {
  readSection,
  resolveRequestedSection,
  resolveScrollAnchor,
  SECTIONS,
  tierSatisfies,
  type Section,
  type SectionMeta,
} from './sections';
import '@/styles/pageRail.css';
import './Settings.css';

/** A section's rows while the account is read on a cold load: a name and a
 *  line under it on the left, the control on the right, as the rows lay theirs
 *  out. */
function SettingsSkeleton() {
  const { t } = useTranslation();
  return (
    <div className="settings-card" aria-busy="true">
      <span className="sr-only">{t('common.loading')}</span>
      {[30, 22, 36, 26].map((width) => (
        <div key={width} className="settings-row" aria-hidden="true">
          <div className="settings-row-text">
            <div className="text-[0.8125rem]"><SkeletonBar style={{ width: `${width}%` }} /></div>
            <div className="mt-1 text-xs"><SkeletonBar style={{ width: `${width + 25}%` }} /></div>
          </div>
          <SkeletonBar className="h-7 w-28 rounded-md" />
        </div>
      ))}
    </div>
  );
}

// DALTON-HOOK(settings-reorg): render consolidated Personal + Team Management panels
function SectionBody({ section }: { section: Section }) {
  switch (section) {
    case 'account': return <AccountPanel />;
    case 'preferences': return <InvestingProfilePanel />;
    case 'general': return <GeneralPanel />;
    case 'chat': return <ChatPanel />;
    case 'computers': return <ComputersPanel />;
    case 'model': return <ModelsPanel />;
    case 'budget': return <DaltonBudgetPanel />;
    case 'integrations': return <IntegrationsPanel />;
    case 'coverage': return <DaltonCoveragePanel />;
    case 'teamModels': return <DaltonTeamModelsPanel />;
    case 'teamMembers': return <DaltonTeamMembersPanel />;
    case 'teamChannels': return <DaltonTeamOpsPanel />;
    // DALTON-HOOK(hide-trading): Dalton hides live brokerage trading section
    case 'trading': return <AccountPanel />;
    // DALTON-HOOK(hide-experiments): Dalton hides experimental labs section
    case 'experiments': return <AccountPanel />;
  }
}

/**
 * A rail of sections beside one content column, as Plugins lays out. The URL
 * is the whole state: `?tab=` the section, and a hash the setting the search
 * jumped to, which is brought into view and marked for a moment.
 */
function Settings() {
  useCloseComputersOnLeave('settings');
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { hash } = location;
  const locationState = location.state as { focusSetting?: boolean; deniedMessage?: string } | null;
  const focusSetting = locationState?.focusSetting === true;
  const deniedMessage = locationState?.deniedMessage;
  const { isLoading: isUserLoading } = useUser();
  const { isLoading: isPrefsLoading } = usePreferences();
  const daltonVisibility = useDaltonSettingsVisibility();
  const { t } = useTranslation();

  // DALTON-HOOK(settings-reorg): hold skeleton while tier/coverage permission resolves; redirect unauthorized direct visits cleanly using unified visibility
  const requestedSection = resolveRequestedSection(searchParams, hash);
  const requestedMeta: SectionMeta | undefined = requestedSection ? SECTIONS[requestedSection] : undefined;
  const awaitingTier = Boolean(requestedMeta?.minTier && daltonVisibility.callerTierPending);
  const awaitingCoverage = Boolean(requestedMeta?.coverageUnitsOnly && daltonVisibility.coverageUnitsPending);
  const unauthorizedDaltonAuth = Boolean(requestedMeta?.daltonAuthOnly && !isDaltonAuth);
  const unauthorizedTier = Boolean(
    requestedMeta?.minTier &&
    !daltonVisibility.callerTierPending &&
    !tierSatisfies(daltonVisibility.callerTier, requestedMeta.minTier),
  );
  const disabledCoverage = Boolean(
    requestedMeta?.coverageUnitsOnly &&
    !daltonVisibility.coverageUnitsPending &&
    daltonVisibility.coverageUnitsEnabled !== true,
  );
  const accessDenied = unauthorizedTier || unauthorizedDaltonAuth;

  const section = readSection(searchParams, hash, daltonVisibility);
  const displaySection =
    (awaitingTier || awaitingCoverage) && requestedSection ? requestedSection : section;
  const pageRef = useRef<HTMLDivElement>(null);
  useScrollMemory(pageRef, `page:settings:${displaySection}`);
  const isLoading = isUserLoading || isPrefsLoading || awaitingTier || awaitingCoverage;

  // DALTON-HOOK(settings-reorg): replace unauthorized deep link onto account tab with OSS-specific or tier-specific notice
  useEffect(() => {
    if (!accessDenied && !disabledCoverage) return;
    const message = unauthorizedDaltonAuth
      ? t('settings.daltonAuthOnly')
      : unauthorizedTier
        ? t(
            requestedMeta?.minTier === 'admin'
              ? 'settings.adminRequired'
              : 'settings.internalRequired',
          )
        : undefined;
    navigate('?tab=account', {
      replace: true,
      state: message ? { deniedMessage: message } : undefined,
    });
  }, [
    accessDenied,
    disabledCoverage,
    unauthorizedDaltonAuth,
    unauthorizedTier,
    requestedMeta?.minTier,
    navigate,
    t,
  ]);

  // The setting is brought into view as soon as it is drawn: a section that
  // waits on its own query (Experiments, Trading permission) draws it later. Keyed on
  // the navigation, not the hash alone, so finding the same setting twice
  // marks it twice.
  useEffect(() => {
    const anchor = resolveScrollAnchor(hash); // DALTON-HOOK(settings-reorg)
    const page = pageRef.current;
    if (isLoading || !anchor || !page) return;
    let marked: HTMLElement | null = null;
    const mark = (target: HTMLElement) => {
      marked = target;
      target.scrollIntoView?.({ block: 'center' });
      if (focusSetting) {
        // The row itself where it is the control, else the control it names
        // as its own, else in a group of choices the one picked now, else
        // the first control. A body with none of its
        // own, like the investing answers, hands focus to its group's action.
        const control = 'input, select, button, a[href], [tabindex]:not([tabindex="-1"])';
        ((target.matches(control) ? target : null)
          ?? target.querySelector<HTMLElement>('[data-search-focus]')
          ?? target.querySelector<HTMLElement>('input:checked')
          ?? target.querySelector<HTMLElement>(control)
          ?? target.closest('.settings-group')?.querySelector<HTMLElement>(control))
          ?.focus({ preventScroll: true });
      }
      // Off and on again with a layout between, so a repeat restarts the mark.
      target.removeAttribute('data-found');
      void target.offsetWidth;
      target.setAttribute('data-found', '');
    };
    const drawn = document.getElementById(anchor);
    const observer = drawn ? null : new MutationObserver(() => {
      const target = document.getElementById(anchor);
      if (!target) return;
      observer?.disconnect();
      mark(target);
    });
    if (drawn) mark(drawn);
    else observer?.observe(page, { childList: true, subtree: true });
    return () => {
      observer?.disconnect();
      marked?.removeAttribute('data-found');
    };
  }, [location.key, hash, section, isLoading, focusSetting]);

  return (
    <div className="settings-page">
      {/* Doubles as the window titlebar in the desktop shell; inert
          elsewhere. Above the rail and the scroll port rather than inside
          them, so it stays put once the page is scrolled. */}
      <div className="chrome-drag-strip" aria-hidden="true" />
      <div className="settings-body">
        <SettingsRail section={displaySection} />
        <div ref={pageRef} className="settings-scroll">
          <div className="settings-column">
            <header className="settings-head">
              <h1 className="page-head-title">{t(SECTIONS[displaySection].labelKey)}</h1>
              <p className="settings-head-def">{t(SECTIONS[displaySection].descKey, { brand: BRAND_NAME })}</p> {/* DALTON-HOOK(brand-name) */}
            </header>
            {/* DALTON-HOOK(settings-reorg): keep skeleton while resolving tier and never mount unauthorized sections */}
            {deniedMessage && (
              <DaltonInlineNotice
                tone="warning"
                testId="settings-permission-denied"
                className="mb-4"
              >
                {deniedMessage}
              </DaltonInlineNotice>
            )}
            <SwapCell as="div" swapKey={isLoading ? 'loading' : 'ready'}>
              {/* DALTON-HOOK(settings-reorg): guard unauthorized or disabled section body before mount */}
              {isLoading || accessDenied || disabledCoverage ? (
                <SettingsSkeleton />
              ) : (
                <div key={section} className="settings-content">
                  <SectionBody section={section} />
                </div>
              )}
            </SwapCell>
          </div>
        </div>
      </div>
      {/* The Computers panel, opened from the Computers section's rows, and
          Change spec, opened over Claude Code's view in Model access, so the
          user stays where they started while the computer changes. */}
      <ComputersDialogHost overSheet planInPlace />
    </div>
  );
}

export default Settings;
