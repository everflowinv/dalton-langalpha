import { useState, useCallback, useId, useMemo } from 'react';
import { useNavigate, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import { WizardStepHeader } from '@/components/ui/wizard';
import { SetupFooter } from '../SetupFooter';
import { useProviderCatalog } from '@/hooks/useProviderCatalog';
import { usePreferences } from '@/hooks/usePreferences';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import type { AccessType } from '@/components/model/types';
import { modelPrefs } from '@/lib/modelPreferences';
import { SUBSCRIPTIONS, subscriptionKey } from '@/api/oauth';
import { KeyOptionFields, KeyProviderList, ProviderTitle } from '@/components/modelAccess/KeyProviderPicker';
import { ProviderChoices } from '@/components/modelAccess/ProviderChoices';
import { QuietNote } from '@/components/modelAccess/parts';
import { useKeyOption } from '@/components/modelAccess/useKeyOption';
import { NO_FILTER, type KeyFilter, type KeyOption, type ProviderGroup } from '@/components/modelAccess/providerOptions';
import { useProviderConnected, type LocationState } from './connectStep/shared';

type Method = Exclude<AccessType, 'coding_plan'>;

/**
 * A provider with more than one plan or region, before its key: the same
 * choice the Settings dialog offers, with the wizard's own buttons.
 */
function KeyOptionsView({
  group,
  configured,
  onBack,
  onContinue,
}: {
  group: ProviderGroup;
  configured: ReadonlySet<string>;
  onBack: () => void;
  onContinue: (option: KeyOption) => void;
}) {
  const { t } = useTranslation();
  const headingId = useId();
  const choice = useKeyOption(group, configured);
  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        title={<ProviderTitle name={group.name} markProvider={group.markProvider} title={t('setup.connectTitle', { provider: group.name })} size="md" />}
        helper={t('setup.providerOptionsHelper')}
      />
      <KeyOptionFields choice={choice} />
      <SetupFooter
        back={{ label: t('setup.back'), onClick: onBack }}
        primary={{ label: t('setup.continue'), onClick: () => onContinue(choice.option) }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ProviderStep — Step 2: the provider, as Settings lists them
// ---------------------------------------------------------------------------

export default function ProviderStep() {
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const location = useLocation();
  const { t } = useTranslation();
  const headingId = useId();

  // A coding plan is now a plan on the provider's own step; an older link
  // that still names it lands on the key providers.
  const routed = location.state as { method?: AccessType; group?: string } | null;
  const asked = routed?.method ?? 'api_key';
  const method: Method = asked === 'coding_plan' ? 'api_key' : asked;

  const { keyGroups, localEntries, isLoading } = useProviderCatalog();
  const { preferences } = usePreferences();
  const { providers } = useConfiguredProviders();
  // What already works. A subscription that has to sign in again runs no turn,
  // so choosing it goes through its sign-in like a new one.
  const configuredSet = useMemo(
    () => new Set(providers.filter((p) => p.state !== 'needs_reauth').map((p) => p.provider)),
    [providers],
  );
  const [filter, setFilter] = useState<KeyFilter>(NO_FILTER);
  // Back from a key opens its provider's plans again, as in Settings.
  const [pickedGroup, setPickedGroup] = useState<string | null>(routed?.group ?? null);
  const picked = keyGroups.find((g) => g.group === pickedGroup && g.options.length > 1) ?? null;

  // The user's own providers, which the key method also offers.
  const userCustomProviders = useMemo(() => {
    if (!preferences) return [];
    const cp = modelPrefs(preferences).custom_providers;
    return Array.isArray(cp) ? cp : [];
  }, [preferences]);

  const handleBack = useCallback(() => navigate('/setup/method'), [navigate]);

  // Already connected: back to the page that sent the user here, or straight
  // on to its models. Otherwise on to its key or sign-in, which reads the
  // rest of what it needs from the catalog by the provider's name.
  const goOn = useCallback((state: LocationState & { provider: string }) => {
    if (configuredSet.has(state.provider)) connected(state);
    else navigate('/setup/connect', { state });
  }, [configuredSet, connected, navigate]);

  const openKeyOption = useCallback((group: ProviderGroup, option: KeyOption) => {
    goOn({
      method: 'api_key',
      provider: option.provider,
      displayName: group.name,
      brandKey: option.entry.brand_key ?? option.entry.provider,
    });
  }, [goOn]);

  const pickKeyGroup = useCallback((group: ProviderGroup) => {
    if (group.options.length === 1) openKeyOption(group, group.options[0]);
    else setPickedGroup(group.group);
  }, [openKeyOption]);

  // A definition saved before its key is not a connection: without a key it
  // goes on to model management without consuming the return, which then
  // happens from the Done step. Settings lists only connections, so its models
  // have never been picked.
  const pickCustom = useCallback((name: string) => {
    const customState = { method, provider: name, displayName: name, brandKey: name };
    if (configuredSet.has(name)) connected(customState);
    else navigate('/setup/models', { state: { ...customState, justConnected: true } });
  }, [method, configuredSet, connected, navigate]);

  const addCustom = useCallback(() => {
    navigate('/setup/connect', { state: { method, isCustom: true } });
  }, [navigate, method]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader size={20} className="text-(--color-text-tertiary)" label={t('common.loading')} />
      </div>
    );
  }

  if (picked) {
    return (
      <KeyOptionsView
        key={picked.group}
        group={picked}
        configured={configuredSet}
        onBack={() => setPickedGroup(null)}
        onContinue={(option) => openKeyOption(picked, option)}
      />
    );
  }

  const customLink = (
    <button
      type="button"
      onClick={addCustom}
      className="self-start text-xs underline underline-offset-2 hover:opacity-80"
      style={{ color: 'var(--color-text-secondary)' }}
    >
      {t('modelAccess.customProvider')}
    </button>
  );

  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        title={t('setup.providerTitle')}
        helper={
          method === 'oauth' ? t('setup.providerSubtitleOAuth')
            : method === 'local' ? t('setup.providerSubtitleLocal')
            : t('setup.providerSubtitleApiKey')
        }
      />
      <div className="flex flex-col gap-4">
        {method === 'oauth' && (
          <ProviderChoices
            entries={SUBSCRIPTIONS.map((s) => ({
              provider: s.provider,
              name: t(`modelAccess.${s.key}.name`),
              note: t(`modelAccess.${s.key}.pickNote`),
            }))}
            configured={configuredSet}
            onPick={(p) => goOn({ method: 'oauth', provider: p, displayName: t(`modelAccess.${subscriptionKey(p)}.name`), brandKey: p })}
          />
        )}

        {method === 'api_key' && (
          <>
            <KeyProviderList
              groups={keyGroups}
              filter={filter}
              onFilterChange={setFilter}
              configured={configuredSet}
              onPick={pickKeyGroup}
              compact
            />
            {userCustomProviders.length > 0 && (
              <section className="flex flex-col gap-1">
                <h4 className="px-3 text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('setup.yourProviders')}
                </h4>
                <ProviderChoices
                  entries={userCustomProviders.map((cp) => ({ provider: cp.name, name: cp.name }))}
                  configured={configuredSet}
                  onPick={pickCustom}
                />
              </section>
            )}
            {customLink}
          </>
        )}

        {method === 'local' && (
          <>
            {localEntries.length === 0 ? (
              <QuietNote>{t('setup.noProviders')}</QuietNote>
            ) : (
              <ProviderChoices
                entries={localEntries.map((e) => ({ provider: e.provider, name: e.display_name }))}
                configured={configuredSet}
                onPick={(p) => {
                  const entry = localEntries.find((e) => e.provider === p);
                  if (entry) goOn({ method: 'local', provider: p, displayName: entry.display_name, brandKey: entry.brand_key ?? p });
                }}
              />
            )}
            {customLink}
          </>
        )}
      </div>

      <SetupFooter back={{ label: t('setup.back'), onClick: handleBack }} primary={null} />
    </div>
  );
}
