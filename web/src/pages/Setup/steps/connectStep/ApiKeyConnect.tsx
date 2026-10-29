import { useId } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import { WizardStepHeader } from '@/components/ui/wizard';
import { ApiKeyFormFields, useApiKeyForm } from '@/components/modelAccess/ApiKeyForm';
import { ProviderTitle } from '@/components/modelAccess/KeyProviderPicker';
import { optionName } from '@/components/modelAccess/optionLabels';
import { optionsOf, type KeyOption } from '@/components/modelAccess/providerOptions';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import { useProviderCatalog, type KeyPlace } from '@/hooks/useProviderCatalog';
import { SetupFooter } from '../../SetupFooter';
import { useProviderConnected, type LocationState } from './shared';
// DALTON-HOOK(team-credentials): team-shared credentials hint
import { isDaltonAuth } from '@/config/daltonAuth';
import { TEAM_SHARED_CREDENTIALS_HINT, useTeamCredentials } from '@/hooks/useTeamCredentials'; // DALTON-HOOK(team-credentials)

/**
 * A provider of the user's own is not in the catalog. Its key is edited as a
 * bare entry under its own name, with no default base URL, so the form starts
 * from the one saved with the key.
 */
function ownProviderOption(provider: string, displayName: string): KeyOption {
  return optionsOf({
    provider,
    display_name: displayName,
    access_type: 'api_key',
    brand_key: provider,
    byok_eligible: true,
    base_url: null,
  })[0];
}

/** The wizard's key step: the shared API-key form between the wizard's own header and footer. */
export function ApiKeyConnect({ state }: { state: LocationState }) {
  const { t } = useTranslation();
  const { optionFor, placeOf, isLoading } = useProviderCatalog();
  const provider = state.provider ?? '';

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader size={20} className="text-(--color-text-tertiary)" label={t('common.loading')} />
      </div>
    );
  }

  const option = optionFor(provider) ?? ownProviderOption(provider, state.displayName ?? provider);
  return <KeyForm key={option.provider} state={state} option={option} place={placeOf(provider)} />;
}

function KeyForm({ state, option, place }: { state: LocationState; option: KeyOption; place?: KeyPlace }) {
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const { providers } = useConfiguredProviders();
  const { t } = useTranslation();
  // DALTON-HOOK(team-credentials): team-shared credentials status
  const { isShared: isTeamShared, canManage } = useTeamCredentials();
  // A non-PM member cannot write the shared credentials (the server refuses
  // the save); the form stays read-only with a note instead.
  const teamManagedByAdmin = isDaltonAuth && isTeamShared && !canManage;
  const headingId = useId();

  const method = state.method ?? 'api_key';
  const form = useApiKeyForm(option, providers ?? []);
  const { entry } = option;
  const displayName = place?.group.name ?? entry.display_name;
  const brandKey = entry.brand_key ?? entry.provider;
  // The plan and region were picked on the provider step; this names them
  // where the provider offers more than one.
  const optionLabel = place && place.group.options.length > 1 ? optionName(t, option) : null;

  const handleSave = async () => {
    const savedAs = await form.save();
    if (!savedAs) return;
    const next = { method, provider: savedAs, displayName, brandKey, justConnected: true };
    // A local server has no models until some are picked from its list, so it
    // goes on to the model step whatever page sent the user here; the return
    // then happens from the last step.
    if (form.local) navigate('/setup/models', { state: next });
    else connected(next);
  };

  const handleBack = () => navigate('/setup/provider', { state: { method, group: place?.group.group } });

  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        title={
          <ProviderTitle
            name={displayName}
            markProvider={place?.group.markProvider ?? entry.provider}
            title={t('setup.connectTitle', { provider: displayName })}
            size="md"
          />
        }
        helper={
          form.local
            ? t('setup.localServerDesc')
            : entry.access_type === 'coding_plan'
              ? t('setup.codingPlanDesc')
              : t('setup.apiKeyInputDesc')
        }
      />
      {/* DALTON-HOOK(team-credentials): team-shared credentials hint */}
      {isDaltonAuth && isTeamShared && !teamManagedByAdmin && (
        <p className="text-xs -mt-3 mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
          {TEAM_SHARED_CREDENTIALS_HINT}
        </p>
      )}
      {teamManagedByAdmin && (
        <p className="text-xs -mt-3 mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('modelAccess.teamManagedByAdmin')}
        </p>
      )}
      {optionLabel && (
        <p className="-mt-3 mb-4 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{optionLabel}</p>
      )}
      {/* DALTON-HOOK(team-credentials): hide API key inputs and save action for non-PM members under team sharing */}
      {!teamManagedByAdmin && <ApiKeyFormFields form={form} />}
      <SetupFooter
        back={{ label: t('setup.back'), onClick: handleBack }}
        primary={
          teamManagedByAdmin
            ? null
            : {
                label: form.saving ? t('setup.saving') : t('setup.continue'),
                onClick: handleSave,
                disabled: !form.canSave,
                busy: form.saving,
              }
        }
      />
    </div>
  );
}
