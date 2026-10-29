import { useCallback, useId } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { WizardStepHeader } from '@/components/ui/wizard';
import { AntigravityConnect } from '@/components/dalton/AntigravityConnect'; // DALTON-HOOK(native-oauth-ui)
import { DeviceConnect } from '@/components/modelAccess/DeviceConnect';
import { isSubscriptionProvider, subscriptionKey } from '@/api/oauth';
import { ClaudeConnect } from '@/components/modelAccess/ClaudeConnect';
import { useProviderConnected, type LocationState } from './shared';
import { SetupFooter } from '../../SetupFooter';
// DALTON-HOOK(team-credentials): a non-PM member under team sharing cannot
// connect the shared credential (the server refuses every connect step).
import { isDaltonAuth } from '@/config/daltonAuth';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';

/** A subscription sign-in inside the wizard: the shared flow, then on to the
 *  model step (or back to the page that sent the user here). */
export function SubscriptionConnect({ state }: { state: LocationState }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const headingId = useId();
  const { isShared: isTeamShared, canManage } = useTeamCredentials();
  const teamManagedByAdmin = isDaltonAuth && isTeamShared && !canManage;
  const provider = state.provider && isSubscriptionProvider(state.provider) ? state.provider : 'codex-oauth';
  const key = subscriptionKey(provider);
  const displayName = state.displayName ?? provider;
  const done = useCallback(() => {
    connected({ method: 'oauth', provider, displayName, brandKey: state.brandKey ?? provider, justConnected: true });
  }, [connected, provider, displayName, state.brandKey]);

  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        title={t('setup.connectTitle', { provider: t(`modelAccess.${key}.name`) })}
        helper={t(`modelAccess.${key}.intro`)}
      />
      {/* DALTON-HOOK(team-credentials): no connect flow for a non-PM member
          under team sharing — the note says who manages the credential. */}
      {teamManagedByAdmin ? (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('modelAccess.teamManagedByAdmin')}
        </p>
      ) : (
        <>
          {/* DALTON-HOOK(native-oauth-ui): Antigravity signs in with a browser flow, not a device code. */}
          {provider === 'claude-oauth'
            ? <ClaudeConnect onConnected={done} />
            : provider === 'antigravity-oauth'
              ? <AntigravityConnect key={provider} provider={provider} onConnected={done} />
              : <DeviceConnect key={provider} provider={provider} onConnected={done} />}
        </>
      )}
      <SetupFooter
        back={{ label: t('setup.back'), onClick: () => navigate('/setup/provider', { state: { method: 'oauth' } }) }}
        primary={null}
      />
    </div>
  );
}

