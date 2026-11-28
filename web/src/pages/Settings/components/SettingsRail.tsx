import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Search, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { isNotFoundError } from '@/api/errors'; // DALTON-HOOK(external-share-guard)
import { useCallerIdentity, type UseCallerIdentityOptions } from '@/hooks/useCallerIdentity'; // DALTON-HOOK(settings-section)
import { useCapabilities } from '@/hooks/useCapabilities'; // DALTON-HOOK(standalone-core)
import { useHasPersonalIntegrations } from '@/lib/dalton/personalIntegrationsApi'; // DALTON-HOOK(personal-integrations)
import { useDaltonCoverageUnits } from '@/lib/dalton/coverage'; // DALTON-HOOK(settings-reorg)
import { useWeixinExternalVisibility } from '../panels/dalton/useWeixinVisibility'; // DALTON-HOOK(weixin-multi-account)
import { isDesktopShell } from '@/lib/desktop';
import { isFeatureEnabled, useFeatures } from '@/hooks/useFeatures';
import { isImeComposing } from '@/lib/ime';
import { matchesAllWords, queryWords } from '@/lib/textMatch';
// DALTON-HOOK(settings-section): filter searchable Dalton settings by live card visibility
import {
  useUserCosSharingQuery,
  type DaltonSettingVisibilityContext,
} from '../panels/dalton/DaltonCosSharingCard';
// DALTON-HOOK(standalone-core): import capability-aware settingLabelKey for rendered search titles
import { GROUPS, SECTIONS, searchableSettings, sectionShown, settingLabelKey, settingSearchText, type Section, type SettingEntry } from '../sections';
import { SidebarReveal } from '@/components/Sidebar/SidebarReveal';

// DALTON-HOOK(settings-section): the command palette filters settings with the same card visibility
// DALTON-HOOK(settings-reorg): unify caller identity via useCallerIdentity without channel-bindings fallback
// DALTON-HOOK(standalone-core): gate user-settings query on confirmed internal identity and colleague_sharing capability
export function useDaltonSettingsVisibility(
  identityOptions?: UseCallerIdentityOptions,
): DaltonSettingVisibilityContext {
  const { capabilities, isLoading: capabilitiesLoading, hasCapability } = useCapabilities();
  const identity = useCallerIdentity(identityOptions);
  const { data, error: cosSharingError } = useUserCosSharingQuery(
    identity.isInternal && hasCapability('colleague_sharing'),
  );
  const cosSharingNotFound = isNotFoundError(cosSharingError);
  // DALTON-HOOK(watchlist): decouple team coverage units gating via useCapabilities('colleague_sharing')
  const hasColleagueSharing = hasCapability('colleague_sharing');
  const { data: coverageData, isLoading: coverageLoading } = useDaltonCoverageUnits(hasColleagueSharing);
  // DALTON-HOOK(weixin-multi-account): delegate external WeChat entry visibility to Dalton hook
  const weixinAllowExternal = useWeixinExternalVisibility(identity.isExternal, hasCapability);
  // DALTON-HOOK(personal-integrations): keep Integrations section visible when historical personal connections exist
  const hasPersonalIntegrations = useHasPersonalIntegrations(identity.status === 'ready');
  const effectiveTier = identity.status === 'ready' ? identity.tier : null;
  return {
    canInvite: identity.canInvite,
    isPm: identity.isPm,
    callerTier: effectiveTier,
    callerTierPending: identity.status === 'loading',
    callerTierError: identity.status === 'error',
    colleagueDispatchEnabled: cosSharingNotFound ? false : data?.colleague_dispatch_enabled,
    cosSharingLoaded: data !== undefined || cosSharingNotFound,
    coverageUnitsEnabled: hasColleagueSharing && coverageData?.enabled === true,
    coverageUnitsPending: hasColleagueSharing && coverageLoading,
    discordChannelsAdmin: identity.isAdmin,
    weixinAllowExternal,
    hasPersonalIntegrations,
    capabilities,
    capabilitiesLoading,
    // DALTON-HOOK(deployment-credentials): expose single-user OSS mode for static login-mode notice
    isSingleUserOss: identity.isSingleUserOss,
  };
}

/**
 * The page's navigation: a search over every setting, then the sections in
 * their groups. While the search has text, its matches take the sections'
 * place, each naming the section it opens; picking one opens that section on
 * the setting itself.
 */
export function SettingsRail({ section }: { section: Section }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const listId = useId();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const [focused, setFocused] = useState(false);
  // A phone folds the field into an icon beside the pills; this is it opened.
  const [searchOpen, setSearchOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (searchOpen) inputRef.current?.focus();
  }, [searchOpen]);

  // `/` jumps to the search from anywhere on the page but a field being typed in.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      const el = document.activeElement as HTMLElement | null;
      if (el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) return;
      e.preventDefault();
      // On a phone the field is folded away until opened; opening it focuses it.
      setSearchOpen(true);
      inputRef.current?.focus();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  // DALTON-HOOK(settings-section): filter searchable settings by card-level permissions and feature flags
  const daltonVisibility = useDaltonSettingsVisibility();
  const features = useFeatures().data;
  const entries = useMemo(
    () => searchableSettings(isDesktopShell(), daltonVisibility, (key) => isFeatureEnabled(features, key)),
    [daltonVisibility, features],
  );
  const words = queryWords(query);
  const matches = words.length === 0
    ? []
    : entries.filter((entry) => matchesAllWords(settingSearchText(entry, t, daltonVisibility), words));
  const searching = words.length > 0;
  const shownActive = Math.min(active, Math.max(matches.length - 1, 0));

  /** Opens the setting's section on it. Picked from the keyboard, focus
   *  follows to its control, so the next key acts on what was found. */
  const open = (entry: SettingEntry, fromKeyboard: boolean) => {
    setQuery('');
    setSearchOpen(false);
    navigate(`?tab=${entry.section}#${entry.anchor}`, { state: { focusSetting: fromKeyboard } });
  };

  // On a phone the sections are a row of pills wider than the screen; the one
  // the page is on is brought into view rather than left past the edge.
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const current = navRef.current?.querySelector<HTMLElement>('[aria-current="page"]');
    current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [section, searching]);

  return (
    <div className={cn('page-rail settings-rail', (searchOpen || searching) && 'is-searching')}>
      {/* The search keeps a usable width: beside the hidden sidebar's toggle
          where they fit, under it where they do not (the desktop app's back
          and forward take more room). */}
      <div className="flex flex-wrap items-center gap-2">
        <SidebarReveal titlebar />
        <div className="page-search settings-search min-w-0 flex-[1_1_160px]">
          <Search className="settings-search-icon" aria-hidden />
          <input
            ref={inputRef}
            type="search"
            className="page-search-input"
            role="combobox"
            aria-expanded={searching}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={searching && matches.length > 0 ? `${listId}-${shownActive}` : undefined}
            aria-label={t('settings:search.label')}
            placeholder={t('settings:search.placeholder')}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            onFocus={() => setFocused(true)}
            onBlur={() => {
              setFocused(false);
              if (!query) setSearchOpen(false);
            }}
            onKeyDown={(e) => {
              // Keys that commit an IME composition edit the query; they pick nothing.
              if (isImeComposing(e)) return;
              if (e.key === 'Escape') {
                e.preventDefault();
                if (query) setQuery('');
                else e.currentTarget.blur();
              } else if (e.key === 'ArrowDown' && matches.length > 0) {
                e.preventDefault();
                setActive((shownActive + 1) % matches.length);
              } else if (e.key === 'ArrowUp' && matches.length > 0) {
                e.preventDefault();
                setActive((shownActive - 1 + matches.length) % matches.length);
              } else if (e.key === 'Enter' && matches[shownActive]) {
                e.preventDefault();
                open(matches[shownActive], true);
              }
            }}
          />
          {!query && !focused && <kbd className="settings-search-key" aria-hidden>/</kbd>}
          {query && (
            <button
              type="button"
              // DALTON-HOOK(mobile-web): expand coarse-pointer touch target on settings search clear button
              className="settings-search-clear tap-target"
              aria-label={t('settings:search.clear')}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => setQuery('')}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {searching ? (
        <div id={listId} role="listbox" aria-label={t('settings:search.results')} className="settings-results">
          {matches.length === 0 ? (
            <p className="settings-results-empty">{t('settings:search.empty')}</p>
          ) : (
            matches.map((entry, index) => {
              const Icon = SECTIONS[entry.section].icon;
              return (
                <div
                  key={entry.anchor}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={index === shownActive}
                  className={cn('settings-result', index === shownActive && 'is-active')}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => open(entry, false)}
                >
                  <Icon className="page-rail-icon" aria-hidden />
                  <span className="min-w-0">
                    {/* DALTON-HOOK(standalone-core): display capability-aware title so disabled channels never appear */}
                    <span className="settings-result-name">{t(settingLabelKey(entry, daltonVisibility))}</span>
                    <span className="settings-result-section">{t(SECTIONS[entry.section].labelKey)}</span>
                  </span>
                </div>
              );
            })
          )}
        </div>
      ) : (
        <nav ref={navRef} className="settings-rail-nav" aria-label={t('settings:railLabel')}>
          <button
            type="button"
            // DALTON-HOOK(mobile-web): expand coarse-pointer touch target on settings search toggle button
            className="settings-search-toggle tap-target"
            aria-label={t('settings:search.label')}
            onClick={() => setSearchOpen(true)}
          >
            <Search aria-hidden />
          </button>
          {/* DALTON-HOOK(hide-experiments): omit empty groups when all their sections are hidden */}
          {/* DALTON-HOOK(settings-reorg): pass daltonVisibility to sectionShown for tier & coverage filtering */}
          {GROUPS.map((group) => {
            const visible = group.sections.filter((s) => sectionShown(s, daltonVisibility));
            if (visible.length === 0) return null;
            return (
            <div key={group.labelKey} className="settings-rail-group">
              <div className="page-rail-label open-style:label-voice">{t(group.labelKey)}</div>
              {visible.map((s) => {
                const Icon = SECTIONS[s].icon;
                const current = s === section;
                return (
                  <Link
                    key={s}
                    to={`?tab=${s}`}
                    // DALTON-HOOK(mobile-web): expand coarse-pointer touch target on section pills
                    className={cn('page-rail-link tap-target', current && 'is-active')}
                    aria-current={current ? 'page' : undefined}
                  >
                    <Icon className="page-rail-icon" aria-hidden />
                    <span className={cn('page-rail-name', current && 'rail-prompt')}>{t(SECTIONS[s].labelKey)}</span>
                  </Link>
                );
              })}
            </div>
            );
          })}
        </nav>
      )}
    </div>
  );
}
