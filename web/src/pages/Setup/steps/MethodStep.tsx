import { useState, useCallback, useId } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Ticket, Link2, Code2, Key, Monitor, CheckCircle2, ChevronDown, ChevronUp } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { OptionCard, OptionGroup } from '@/components/ui/option-card';
import { WizardStepHeader } from '@/components/ui/wizard';
import { SetupFooter } from '../SetupFooter';
import { Loader } from '@/components/ui/loader';
import { Input } from '@/components/ui/input';
import { api } from '@/api/client';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { useConfiguredProviders, type ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import { useUser } from '@/hooks/useUser';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { deleteUserApiKey, disconnectCodexOAuth, disconnectClaudeOAuth, getCurrentUser } from '@/pages/Dashboard/utils/api';
import type { AccessType } from '@/components/model/types';
import { isPlatformMode } from '@/config/hostMode';
import { useAllModels } from '@/hooks/useAllModels';
import { modelPrefs, splitPreferenceWrite } from '@/lib/modelPreferences';
import type { PreferencePatch, PreferencesLike } from '@/lib/modelPreferences';
// DALTON-HOOK(team-credentials): team-shared credentials hint & PM disconnect guard
import { isDaltonAuth } from '@/config/daltonAuth';
import { TEAM_SHARED_CREDENTIALS_HINT, useTeamCredentials } from '@/hooks/useTeamCredentials'; // DALTON-HOOK(team-credentials)

// ---------------------------------------------------------------------------
// Method card data
// ---------------------------------------------------------------------------

const ALL_METHODS: Array<{
  id: AccessType;
  icon: typeof Link2;
  titleKey: string;
  descKey: string;
  platformOnly?: boolean;
  ossOnly?: boolean;
}> = [
  {
    id: 'oauth',
    icon: Link2,
    titleKey: 'setup.methodOAuth',
    descKey: 'setup.methodOAuthDesc',
  },
  {
    id: 'coding_plan',
    icon: Code2,
    titleKey: 'setup.methodCodingPlan',
    descKey: 'setup.methodCodingPlanDesc',
  },
  {
    id: 'api_key',
    icon: Key,
    titleKey: 'setup.methodApiKey',
    descKey: 'setup.methodApiKeyDesc',
  },
  {
    id: 'local',
    icon: Monitor,
    titleKey: 'setup.methodLocal',
    descKey: 'setup.methodLocalDesc',
    ossOnly: true,
  },
];

const METHODS = ALL_METHODS.filter((m) => {
  if (m.ossOnly && isPlatformMode) return false;
  if (m.platformOnly && !isPlatformMode) return false;
  return true;
});

// ---------------------------------------------------------------------------
// Configured providers banner
// ---------------------------------------------------------------------------

function ConfiguredBanner({
  providers,
  onRemove,
  canDisconnect = true, // DALTON-HOOK(team-credentials): PM-only disconnect/remove guard
}: {
  providers: ConfiguredProvider[];
  onRemove: (provider: ConfiguredProvider) => void;
  canDisconnect?: boolean;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  if (providers.length === 0) return null;

  return (
    <div
      className="rounded-lg overflow-hidden"
      style={{
        background: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
    >
      {/* Header row */}
      <div className="flex items-center justify-between p-4">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="flex items-center gap-1.5 text-left"
        >
          <CheckCircle2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-success)' }} />
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('setup.providersConnected', { count: providers.length })}
          </span>
          {expanded
            ? <ChevronUp className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
            : <ChevronDown className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
          }
        </button>
      </div>

      {/* Expandable provider list */}
      {expanded && (
        <div
          className="flex flex-col gap-0"
          style={{ borderTop: '1px solid var(--color-border-muted)' }}
        >
          {providers.map((p) => (
            <div
              key={p.provider}
              className="flex items-center justify-between px-4 py-2.5"
              style={{ borderBottom: '1px solid var(--color-border-muted)' }}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm truncate" style={{ color: 'var(--color-text-primary)' }}>
                  {p.display_name}
                </span>
                <span
                  className="inline-flex items-center px-1.5 py-0.5 rounded text-[0.625rem] font-medium shrink-0"
                  style={{
                    background: p.access_type === 'oauth' ? 'var(--color-accent-soft)' : 'var(--color-bg-page)',
                    color: p.access_type === 'oauth' ? 'var(--color-accent-primary)' : 'var(--color-text-tertiary)',
                    border: p.access_type === 'oauth' ? 'none' : '1px solid var(--color-border-default)',
                  }}
                >
                  {p.access_type === 'oauth' ? t('setup.oauthBadge')
                    : p.access_type === 'local' ? t('setup.localBadge')
                    : p.access_type === 'coding_plan' ? t('setup.codingPlanBadge')
                    : t('setup.apiKeyBadge')}
                </span>
              </div>
              {/* DALTON-HOOK(team-credentials): hide remove/disconnect button for non-PM team members */}
              {canDisconnect && (
                <button
                  type="button"
                  onClick={() => onRemove(p)}
                  className="text-xs shrink-0 px-2 py-1 rounded transition-colors hover:opacity-80"
                  style={{ color: 'var(--color-loss)' }}
                >
                  {p.access_type === 'oauth' ? t('setup.disconnectProvider') : t('setup.removeProvider')}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// MethodStep
// ---------------------------------------------------------------------------

export default function MethodStep() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { providers: configuredProviders, hasAny: hasConfigured } = useConfiguredProviders();
  // DALTON-HOOK(team-credentials): team-shared credentials status
  const { isShared: isTeamShared, canDisconnect } = useTeamCredentials();
  const allowDisconnect = !isDaltonAuth || canDisconnect;
  const { user, isLoading: userLoading } = useUser();
  const [localRedeemed, setLocalRedeemed] = useState(false);
  const hasPlatformAccess = (user?.access_tier ?? -1) >= 0 || localRedeemed;
  // Wait for user data before using hasPlatformAccess to avoid flicker
  const canSkip = hasConfigured || (!userLoading && hasPlatformAccess);
  const { preferences } = usePreferences();
  const updatePreferences = useUpdatePreferences();
  const { metadata } = useAllModels();

  const [selected, setSelected] = useState<AccessType | null>(null);
  const headingId = useId();

  // Invitation code state
  const [showInvitation, setShowInvitation] = useState(false);
  const [invitationCode, setInvitationCode] = useState('');
  const [invitationError, setInvitationError] = useState<string | null>(null);
  const [redeemingInvitation, setRedeemingInvitation] = useState(false);

  const handleRemoveProvider = useCallback(async (provider: ConfiguredProvider) => {
    try {
      if (provider.access_type === 'oauth') {
        if (provider.provider === 'codex-oauth') {
          await disconnectCodexOAuth();
          queryClient.invalidateQueries({ queryKey: queryKeys.oauth.codex() });
        } else if (provider.provider === 'claude-oauth') {
          await disconnectClaudeOAuth();
          queryClient.invalidateQueries({ queryKey: queryKeys.oauth.claude() });
        } else {
          // DALTON-HOOK(native-oauth-ui): P0N.3 disconnect Antigravity / Meta Muse
          const { disconnectDaltonOAuth } = await import('@/pages/Settings/utils/daltonOAuthApi');
          await disconnectDaltonOAuth(provider.provider);
          queryClient.invalidateQueries({ queryKey: queryKeys.oauth.all });
        }
      } else {
        // Remove API key
        await deleteUserApiKey(provider.provider).catch(() => {});
        // Clean custom_providers, custom_models, and any model preference
        // fields that reference models from the removed provider.
        const modelPref = modelPrefs(preferences);
        const existingProviders = modelPref.custom_providers ?? [];
        const existingModels = modelPref.custom_models ?? [];
        const isCustom = existingProviders.some(cp => cp.name === provider.provider);
        const remainingProviders = isCustom ? existingProviders.filter(cp => cp.name !== provider.provider) : existingProviders;
        const remainingModels = existingModels.filter(cm => cm.provider !== provider.provider);
        // Collect ALL model names belonging to this provider (custom + built-in)
        const removedModelNames = new Set([
          ...existingModels.filter(cm => cm.provider === provider.provider).map(cm => cm.name),
          ...Object.entries(metadata)
            .filter(([, meta]) => meta?.provider === provider.provider)
            .map(([name]) => name),
        ]);
        const cleanModelPref = (val: unknown) =>
          typeof val === 'string' && removedModelNames.has(val) ? null : undefined;
        // One flat patch: splitPreferenceWrite routes each key to its column,
        // so fallback (model routing) and starred (UI favorites) landing in
        // different places is no longer this call site's problem.
        const prefUpdate: PreferencePatch = {
          custom_providers: remainingProviders.length > 0 ? remainingProviders : null,
          custom_models: remainingModels.length > 0 ? remainingModels : null,
        };
        // Clear subsidiary/preferred model fields if they reference a removed model
        if (cleanModelPref(modelPref.compaction_model) === null) prefUpdate.compaction_model = null;
        if (cleanModelPref(modelPref.summarization_model) === null) prefUpdate.summarization_model = null;
        if (cleanModelPref(modelPref.fetch_model) === null) prefUpdate.fetch_model = null;
        if (cleanModelPref(modelPref.preferred_model) === null) prefUpdate.preferred_model = null;
        if (cleanModelPref(modelPref.preferred_background_model) === null) prefUpdate.preferred_background_model = null;
        const fallback = modelPref.fallback_models ?? [];
        const cleanFallback = fallback.filter(m => !removedModelNames.has(m));
        if (cleanFallback.length !== fallback.length) prefUpdate.fallback_models = cleanFallback.length > 0 ? cleanFallback : null;

        // Profiles are keyed by name only, so one left behind is inherited by
        // whatever answers to that name next. Removing a custom provider
        // re-exposes any built-in its models shadowed, and that built-in would
        // pick up the custom model's effort, guidance, compaction and speed.
        const profiles = modelPref.profiles ?? {};
        const staleProfiles = Object.keys(profiles).filter((m) => removedModelNames.has(m));
        if (staleProfiles.length > 0) {
          prefUpdate.profiles = Object.fromEntries(staleProfiles.map((m) => [m, null]));
        }

        const starredPrefs = (preferences ?? {}) as PreferencesLike;
        const starred = (starredPrefs.other_preference?.starred_models as string[]) ?? [];
        const cleanStarred = starred.filter(m => !removedModelNames.has(m));
        if (cleanStarred.length !== starred.length) prefUpdate.starred_models = cleanStarred.length > 0 ? cleanStarred : null;

        await updatePreferences.mutateAsync(splitPreferenceWrite(prefUpdate));
      }
      queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
      queryClient.invalidateQueries({ queryKey: queryKeys.user.apiKeys() });
    } catch {
      // Silently fail — user can retry
    }
  }, [preferences, updatePreferences, queryClient, metadata]);

  const handleNext = useCallback(() => {
    if (!selected) return;
    navigate('/setup/provider', { state: { method: selected } });
  }, [selected, navigate]);

  const handleSkipToDefaults = useCallback(() => {
    navigate('/setup/defaults');
  }, [navigate]);

  const handleRedeemInvitation = useCallback(async () => {
    if (!invitationCode.trim()) {
      setInvitationError(t('setup.invitationErrorEmpty'));
      return;
    }

    setRedeemingInvitation(true);
    setInvitationError(null);

    try {
      await api.post('/api/auth/invitations/redeem', { code: invitationCode.trim() });
      setLocalRedeemed(true);
      // Bust stale platform tier cache, then refresh user + model access queries
      await getCurrentUser({ refresh_tier: true }).catch(() => {});
      await queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
      navigate('/setup/defaults');
    } catch (e: unknown) {
      const err = e as {
        response?: { status?: number; data?: { detail?: string | { message?: string; type?: string } } };
        message?: string;
      };
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail;

      if (status === 404) {
        setInvitationError(t('setup.invitationErrorInvalid'));
      } else if (status === 410) {
        setInvitationError(t('setup.invitationErrorExpired'));
      } else if (status === 409) {
        // 409 = this user already redeemed the code — they have access.
        // Treat same as successful redemption: grant local access + navigate.
        setLocalRedeemed(true);
        await getCurrentUser({ refresh_tier: true }).catch(() => {});
        await queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
        await queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
        navigate('/setup/defaults');
        return;
      } else if (typeof detail === 'string') {
        setInvitationError(detail);
      } else if (detail && typeof detail === 'object' && 'message' in detail) {
        setInvitationError(detail.message || t('setup.invitationErrorGeneric'));
      } else {
        setInvitationError(t('setup.invitationErrorGeneric'));
      }
    } finally {
      setRedeemingInvitation(false);
    }
  }, [invitationCode, queryClient, navigate, t]);

  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        title={canSkip ? t('setup.addAnotherProviderHeading') : t('setup.methodTitle')}
        helper={canSkip ? t('setup.addMoreProviders') : t('setup.methodSubtitle')}
      />
      <div className="flex flex-col gap-4 sm:gap-6">
      {/* DALTON-HOOK(team-credentials): team-shared credentials hint */}
      {isDaltonAuth && isTeamShared && (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {TEAM_SHARED_CREDENTIALS_HINT}
        </p>
      )}
      {/* Configured providers banner (visible when looping back) */}
      {hasConfigured && (
        <ConfiguredBanner
          providers={configuredProviders}
          onRemove={handleRemoveProvider}
          canDisconnect={allowDisconnect}
        />
      )}

      {/* Invitation-redeemed note (when no other providers configured); the
          way on from it is the footer's skip */}
      {!userLoading && hasPlatformAccess && !hasConfigured && (
        <div
          className="flex items-center gap-2 rounded-lg p-4"
          style={{
            background: 'var(--color-bg-card)',
            border: '1px solid var(--color-border-muted)',
          }}
        >
          <CheckCircle2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-success)' }} />
          <span className="text-sm" style={{ color: 'var(--color-text-primary)' }}>
            {t('setup.invitationRedeemed')}
          </span>
        </div>
      )}

      {/* Method cards: the welcome flow's choice cards, so the two halves of
          onboarding pick the same way */}
      <OptionGroup<AccessType> value={selected} onChange={setSelected} labelledBy={headingId}>
        {METHODS.map((method) => {
          const Icon = method.icon;
          return (
            <OptionCard<AccessType>
              key={method.id}
              value={method.id}
              icon={<Icon className="h-4 w-4" />}
              title={t(method.titleKey)}
              description={t(method.descKey)}
            />
          );
        })}
      </OptionGroup>

      {/* Invitation code section — hide only when user already has platform access */}
      {(userLoading || !hasPlatformAccess) && (
        <>
          {!showInvitation ? (
            <button
              type="button"
              onClick={() => setShowInvitation(true)}
              className="inline-flex items-center gap-1.5 text-sm font-medium transition-colors self-start text-(--color-text-secondary) hover:text-(--color-text-primary)"
            >
              <Ticket className="h-4 w-4" />
              {t('setup.invitationCodeLink')}
            </button>
          ) : (
            <div
              className="flex flex-col gap-3 rounded-lg p-4"
              style={{
                background: 'var(--color-bg-card)',
                border: '1px solid var(--color-border-muted)',
              }}
            >
              <label
                className="text-sm font-medium"
                style={{ color: 'var(--color-text-primary)' }}
              >
                {t('setup.invitationCodeLabel')}
              </label>
              <div className="flex gap-2">
                <Input
                  value={invitationCode}
                  onChange={(e) => {
                    setInvitationCode(e.target.value);
                    setInvitationError(null);
                  }}
                  placeholder={t('setup.invitationCodePlaceholder')}
                  className="flex-1"
                  autoComplete="off"
                  spellCheck={false}
                />
                <Button
                  variant="default"
                  disabled={redeemingInvitation || !invitationCode.trim()}
                  onClick={handleRedeemInvitation}
                  className="shrink-0"
                >
                  {redeemingInvitation ? (
                    <>
                      <span aria-hidden="true" className="mr-1.5 shrink-0">
                        <Loader size={16} className="text-current" />
                      </span>
                      {t('setup.redeeming')}
                    </>
                  ) : (
                    t('setup.redeem')
                  )}
                </Button>
              </div>
              {invitationError && (
                <p className="text-xs" style={{ color: 'var(--color-loss)' }}>
                  {invitationError}
                </p>
              )}
              <button
                type="button"
                onClick={() => {
                  setShowInvitation(false);
                  setInvitationError(null);
                }}
                className="text-xs self-start"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                {t('setup.chooseMethodInstead')}
              </button>
            </div>
          )}
        </>
      )}

      </div>

      <SetupFooter
        skip={canSkip ? { label: t('setup.skipToModels'), onClick: handleSkipToDefaults } : null}
        primary={{ label: t('setup.continue'), onClick: handleNext, disabled: !selected }}
      />
    </div>
  );
}
