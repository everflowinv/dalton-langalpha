import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Search, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { isDaltonAuth } from '@/config/daltonAuth'; // DALTON-HOOK(settings-section)
import { useAuth } from '@/contexts/AuthContext'; // DALTON-HOOK(settings-section)
import { useCapabilities } from '@/hooks/useCapabilities'; // DALTON-HOOK(standalone-core)
import { useTeamCredentials } from '@/hooks/useTeamCredentials'; // DALTON-HOOK(settings-section)
import { useDaltonCoverageUnits } from '@/lib/dalton/coverage'; // DALTON-HOOK(settings-reorg)
import { queryKeys } from '@/lib/queryKeys'; // DALTON-HOOK(settings-section)
import { fetchDaltonChannelBindings } from '../utils/daltonApi'; // DALTON-HOOK(settings-section)
import { isDesktopShell } from '@/lib/desktop';
import { isImeComposing } from '@/lib/ime';
import { matchesAllWords, queryWords } from '@/lib/textMatch';
// DALTON-HOOK(settings-section): filter searchable Dalton settings by live card visibility
import {
  useUserCosSharingQuery,
  type DaltonSettingVisibilityContext,
} from '../panels/dalton/DaltonCosSharingCard';
// DALTON-HOOK(standalone-core): import capability-aware settingLabelKey for rendered search titles
import { GROUPS, SECTIONS, searchableSettings, sectionShown, settingLabelKey, settingSearchText, type Section, type SettingEntry } from '../sections';

// DALTON-HOOK(settings-section): the command palette filters settings with the same card visibility
// DALTON-HOOK(settings-reorg): fail closed on callerTierError without cos-sharing fallback; keep OSS admin & coverage parity
// DALTON-HOOK(standalone-core): inject live deployment capabilities and skip channel-bindings fetch when channels are disabled
export function useDaltonSettingsVisibility(): DaltonSettingVisibilityContext {
  const { userId } = useAuth();
  const { capabilities, isLoading: capabilitiesLoading, hasCapability } = useCapabilities();
  const { canInvite, isPm, callerTier, callerTierPending, callerTierError } = useTeamCredentials();
  const { data } = useUserCosSharingQuery(isDaltonAuth);
  const { data: coverageData, isLoading: coverageLoading } = useDaltonCoverageUnits(true);
  const channelsEnabled = hasCapability([
    'channel_gateway',
    'channel_feishu',
    'channel_discord',
    'channel_weixin',
  ]);
  const { data: bindingsData, isLoading: bindingsLoading } = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    enabled: Boolean(userId) && channelsEnabled,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const fallbackTier = bindingsData?.user_tier ?? null;
  const daltonResolvedTier =
    !callerTierError && callerTier ? callerTier : fallbackTier;
  const waitingOnBindingsFallback =
    isDaltonAuth &&
    Boolean(userId) &&
    channelsEnabled &&
    bindingsLoading &&
    (callerTierError || (!callerTierPending && !callerTier));
  const effectiveTier = isDaltonAuth
    ? daltonResolvedTier
    : (isPm ? 'admin' : null);
  const discordChannelsAdmin = isDaltonAuth
    ? daltonResolvedTier === 'admin' &&
      (fallbackTier === null || fallbackTier === 'admin')
    : fallbackTier === 'admin';
  return {
    canInvite,
    isPm,
    callerTier: effectiveTier,
    callerTierPending: isDaltonAuth ? callerTierPending || waitingOnBindingsFallback : false,
    callerTierError: isDaltonAuth ? callerTierError && fallbackTier === null : false,
    colleagueDispatchEnabled: data?.colleague_dispatch_enabled,
    cosSharingLoaded: data !== undefined,
    coverageUnitsEnabled: coverageData?.enabled === true,
    coverageUnitsPending: coverageLoading,
    discordChannelsAdmin,
    capabilities,
    capabilitiesLoading,
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

  // DALTON-HOOK(settings-section): filter searchable settings by card-level permissions
  const daltonVisibility = useDaltonSettingsVisibility();
  const entries = useMemo(
    () => searchableSettings(isDesktopShell(), daltonVisibility),
    [daltonVisibility],
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
      <div className="page-search settings-search">
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
          aria-label={t('settings.search.label')}
          placeholder={t('settings.search.placeholder')}
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
            className="settings-search-clear"
            aria-label={t('settings.search.clear')}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => setQuery('')}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {searching ? (
        <div id={listId} role="listbox" aria-label={t('settings.search.results')} className="settings-results">
          {matches.length === 0 ? (
            <p className="settings-results-empty">{t('settings.search.empty')}</p>
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
        <nav ref={navRef} className="settings-rail-nav" aria-label={t('settings.railLabel')}>
          <button
            type="button"
            className="settings-search-toggle"
            aria-label={t('settings.search.label')}
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
                    className={cn('page-rail-link', current && 'is-active')}
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
