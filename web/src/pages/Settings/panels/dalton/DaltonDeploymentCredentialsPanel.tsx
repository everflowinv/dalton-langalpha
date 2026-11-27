import { useId, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, ExternalLink, FlaskConical, ShieldAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Loader } from '@/components/ui/loader';
import { ToggleSwitch } from '@/components/ui/switch';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
  type DaltonStatusTone,
} from '@/components/dalton/primitives';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useCapabilities } from '@/hooks/useCapabilities';
import {
  ADMIN_INTEGRATIONS_HELP_URL,
  fetchDeploymentSettings,
  parseDeploymentSettingsError,
  testDeploymentCredentials,
  updateDeploymentSettings,
  type CredentialSource,
  type DeploymentCredentialProbeResponse,
  type DeploymentSettingField,
  type DeploymentSettingsResponse,
  type ParsedDeploymentSettingsError,
  type ProbeTarget,
} from '@/lib/dalton/deploymentCredentials';
import { queryKeys } from '@/lib/queryKeys';

const GOOGLE_ID_KEY = 'DALTON_GOOGLE_OAUTH_CLIENT_ID';
const GOOGLE_SECRET_KEY = 'DALTON_GOOGLE_OAUTH_CLIENT_SECRET';
const FEISHU_APP_ID_KEY = 'DALTON_FEISHU_APP_ID';
const FEISHU_APP_SECRET_KEY = 'DALTON_FEISHU_APP_SECRET';
const FEISHU_ENCRYPT_KEY = 'DALTON_FEISHU_ENCRYPT_KEY';
const FEISHU_VERIFY_TOKEN_KEY = 'DALTON_FEISHU_VERIFICATION_TOKEN';

const SOURCE_TONE: Record<CredentialSource, DaltonStatusTone> = {
  env: 'accent',
  saved: 'success',
  vault_readonly: 'warning',
  vault_error: 'danger',
  unset: 'neutral',
};

function sourceBadgeLabel(
  source: CredentialSource,
  t: (key: string) => string,
): string {
  switch (source) {
    case 'env':
      return t('settings:dalton.deploymentCredentials.sourceEnv');
    case 'saved':
      return t('settings:dalton.deploymentCredentials.sourceSaved');
    case 'vault_readonly':
      return t('settings:dalton.deploymentCredentials.sourceVaultReadonly');
    case 'vault_error':
      return t('settings:dalton.deploymentCredentials.sourceVaultError');
    case 'unset':
      return t('settings:dalton.deploymentCredentials.sourceUnset');
  }
}

function redirectReasonMessage(
  reason: string | null,
  t: (key: string) => string,
): string {
  switch (reason) {
    case 'raw_ip_not_allowed':
      return t('settings:dalton.deploymentCredentials.googleReasonRawIp');
    case 'private_tld_not_allowed':
      return t('settings:dalton.deploymentCredentials.googleReasonPrivateTld');
    case 'https_required':
    default:
      return t('settings:dalton.deploymentCredentials.googleReasonHttpsRequired');
  }
}

function formatSaveErrorMessage(
  parsed: ParsedDeploymentSettingsError,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (parsed.status === 409) {
    if (parsed.code === 'revision_conflict') {
      return parsed.currentRevision !== undefined
        ? t('settings:dalton.deploymentCredentials.errRevisionConflictWithRev', {
            revision: parsed.currentRevision,
          })
        : t('settings:dalton.deploymentCredentials.errRevisionConflict');
    }
    if (parsed.code === 'env_locked') {
      return t('settings:dalton.deploymentCredentials.errEnvLocked');
    }
    if (parsed.code === 'feishu_bot_readonly_in_batch1') {
      return t('settings:dalton.deploymentCredentials.errFeishuVaultReadonly');
    }
  }
  if (parsed.status === 422) {
    if (parsed.reasons.includes('incomplete_credential_group')) {
      return t('settings:dalton.deploymentCredentials.errIncompleteGroup');
    }
    if (parsed.reasons.includes('value_too_long')) {
      return t('settings:dalton.deploymentCredentials.errValueTooLong');
    }
    if (parsed.reasons.includes('unknown_setting')) {
      return t('settings:dalton.deploymentCredentials.errUnknownSetting');
    }
    return t('settings:dalton.deploymentCredentials.errInvalidFormat');
  }
  if (parsed.status === 503 && parsed.code === 'encryption_key_unconfigured') {
    return t('settings:dalton.deploymentCredentials.errEncryptionUnconfigured');
  }
  if (parsed.status === 403 && parsed.code === 'login_mode_required') {
    return t('settings:dalton.deploymentCredentials.ossTitle');
  }
  return t('settings:dalton.deploymentCredentials.errSaveGeneric');
}

function formatProbeMessage(
  result: DeploymentCredentialProbeResponse,
  t: (key: string) => string,
): { tone: 'success' | 'error' | 'warning'; text: string } {
  if (result.ok && result.verdict === 'valid') {
    return {
      tone: 'success',
      text: t('settings:dalton.deploymentCredentials.probeValid'),
    };
  }
  if (result.verdict === 'network_error') {
    return {
      tone: 'error',
      text: t('settings:dalton.deploymentCredentials.probeNetworkError'),
    };
  }
  if (result.verdict === 'rate_limited') {
    return {
      tone: 'warning',
      text: t('settings:dalton.deploymentCredentials.probeRateLimited'),
    };
  }
  if (result.verdict === 'format_invalid') {
    if (result.detail_code === 'vault_error') {
      return {
        tone: 'error',
        text: t('settings:dalton.deploymentCredentials.probeVaultError'),
      };
    }
    if (result.detail_code === 'incomplete_credential_group') {
      return {
        tone: 'warning',
        text: t('settings:dalton.deploymentCredentials.probeIncomplete'),
      };
    }
    return {
      tone: 'warning',
      text: t('settings:dalton.deploymentCredentials.probeFormatInvalid'),
    };
  }
  // invalid_credentials
  if (result.detail_code === 'feishu_invalid_param') {
    return {
      tone: 'error',
      text: t('settings:dalton.deploymentCredentials.probeFeishuInvalidParam'),
    };
  }
  if (result.detail_code === 'feishu_app_unavailable') {
    return {
      tone: 'error',
      text: t('settings:dalton.deploymentCredentials.probeFeishuAppUnavailable'),
    };
  }
  if (result.detail_code === 'feishu_invalid_secret') {
    return {
      tone: 'error',
      text: t('settings:dalton.deploymentCredentials.probeFeishuInvalidSecret'),
    };
  }
  return {
    tone: 'error',
    text: t('settings:dalton.deploymentCredentials.probeInvalidCredentials'),
  };
}

function CopyableUrlRow({
  label,
  url,
  testId,
}: {
  label: string;
  url: string;
  testId: string;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!url) return;
    try {
      await navigator.clipboard?.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-[var(--color-text-secondary)]">{label}</div>
      <div className="flex items-center gap-2">
        <code
          data-testid={testId}
          className="flex-1 truncate rounded-md border border-[var(--color-border-muted)] bg-[var(--color-bg-elevated)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-text-primary)] select-all"
        >
          {url || t('settings:dalton.deploymentCredentials.callbackUrlUnset')}
        </code>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!url}
          onClick={() => { void handleCopy(); }}
          data-testid={`${testId}-copy`}
          className="h-8 shrink-0 gap-1.5 text-xs"
        >
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          <span>
            {copied
              ? t('settings:dalton.deploymentCredentials.copied')
              : t('settings:dalton.deploymentCredentials.copy')}
          </span>
        </Button>
      </div>
    </div>
  );
}

/** Static guidance view shown in passwordless OSS mode (`DALTON_AUTH_MODE=oss`) without firing any API call. */
function OssLoginModeRequiredView() {
  const { t } = useTranslation();
  return (
    <div id="dalton-deployment-credentials" className="space-y-6" data-testid="deployment-credentials-oss-notice">
      <DaltonSettingsCard
        title={t('settings:dalton.deploymentCredentials.ossTitle')}
        description={t('settings:dalton.deploymentCredentials.ossDesc')}
      >
        <div className="space-y-4">
          <DaltonInlineNotice tone="warning" testId="deployment-credentials-oss-banner">
            <div className="flex items-start gap-2">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{t('settings:dalton.deploymentCredentials.ossBanner')}</span>
            </div>
          </DaltonInlineNotice>
          <div className="space-y-2 text-xs text-[var(--color-text-secondary)]">
            <p className="font-medium text-[var(--color-text-primary)]">
              {t('settings:dalton.deploymentCredentials.ossStepsHeading')}
            </p>
            <pre className="overflow-x-auto rounded-md border border-[var(--color-border-muted)] bg-[var(--color-bg-elevated)] p-3 font-mono text-xs text-[var(--color-text-primary)]">
{`bash scripts/standalone/init.sh --domain research.example.com --tls-mode auto`}
            </pre>
            <p>{t('settings:dalton.deploymentCredentials.ossLocalLoopbackHint')}</p>
          </div>
          <div>
            <a
              href={ADMIN_INTEGRATIONS_HELP_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-xs font-medium text-[var(--color-accent-primary)] hover:underline"
            >
              <span>{t('settings:dalton.deploymentCredentials.helpGuideLink')}</span>
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </div>
        </div>
      </DaltonSettingsCard>
    </div>
  );
}

function resolveGroupSource(
  fields: readonly DeploymentSettingField[],
  groupName: string,
): CredentialSource {
  const match = fields.find((f) => f.credential_group === groupName);
  return match?.source ?? 'unset';
}

function findField(
  fields: readonly DeploymentSettingField[],
  name: string,
): DeploymentSettingField | undefined {
  return fields.find((f) => f.name === name);
}

/** Lazy-loaded Settings -> Team Management -> Integration Credentials panel (§2.2, §5). */
export default function DaltonDeploymentCredentialsPanel() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const identity = useCallerIdentity();
  const { profile } = useCapabilities();
  const baseId = useId();

  const isOssMode =
    identity.isSingleUserOss ||
    identity.authMode === 'oss' ||
    (!isDaltonAuth && identity.status === 'ready' && !identity.isMultiUserAuth);

  const canFetchSettings =
    !isOssMode && identity.status === 'ready' && identity.isAdmin;

  const settingsQuery = useQuery<DeploymentSettingsResponse>({
    queryKey: queryKeys.dalton.deploymentSettings(),
    queryFn: fetchDeploymentSettings,
    enabled: canFetchSettings,
    retry: false,
  });

  // Draft values keyed by env name (undefined means untouched; user edits populate string).
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [statusNotice, setStatusNotice] = useState<{
    tone: 'success' | 'error' | 'warning' | 'info';
    text: string;
    showRefresh?: boolean;
  } | null>(null);
  const [probeResults, setProbeResults] = useState<
    Partial<Record<ProbeTarget, DeploymentCredentialProbeResponse>>
  >({});
  const [probingTarget, setProbingTarget] = useState<ProbeTarget | null>(null);

  const saveMutation = useMutation({
    mutationFn: (payload: Parameters<typeof updateDeploymentSettings>[0]) =>
      updateDeploymentSettings(payload),
    onSuccess: (res, variables) => {
      setDrafts((prev) => {
        const next = { ...prev };
        for (const key of Object.keys(variables.values)) {
          delete next[key];
        }
        return next;
      });
      if (res.pending_apply) {
        setStatusNotice({
          tone: 'warning',
          text: t('settings:dalton.deploymentCredentials.savePendingApply', {
            revision: res.revision,
          }),
        });
      } else {
        setStatusNotice({
          tone: 'success',
          text: t('settings:dalton.deploymentCredentials.saveSuccess', {
            revision: res.revision,
          }),
        });
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.deploymentSettings() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.capabilities() });
    },
    onError: (err) => {
      const parsed = parseDeploymentSettingsError(err);
      setStatusNotice({
        tone: 'error',
        text: formatSaveErrorMessage(parsed, t),
        showRefresh: parsed.status === 409 && parsed.code === 'revision_conflict',
      });
    },
  });

  if (isOssMode) {
    return <OssLoginModeRequiredView />;
  }

  if (identity.status === 'loading' || settingsQuery.isPending) {
    return (
      <div id="dalton-deployment-credentials" className="space-y-6">
        <DaltonSettingsCard title={t('settings:section.deploymentCredentials')}>
          <DaltonLoadingRow label={t('common.loading')} />
        </DaltonSettingsCard>
      </div>
    );
  }

  if (settingsQuery.isError || !settingsQuery.data) {
    const parsed = parseDeploymentSettingsError(settingsQuery.error);
    if (parsed.status === 403 && parsed.code === 'login_mode_required') {
      return <OssLoginModeRequiredView />;
    }
    return (
      <div id="dalton-deployment-credentials" className="space-y-6">
        <DaltonSettingsCard
          title={t('settings:section.deploymentCredentials')}
          action={
            <DaltonRefreshButton
              onClick={() => { void settingsQuery.refetch(); }}
              busy={settingsQuery.isFetching}
            />
          }
        >
          <DaltonInlineNotice tone="error" testId="deployment-credentials-load-error">
            {t('settings:dalton.deploymentCredentials.loadError')}
          </DaltonInlineNotice>
        </DaltonSettingsCard>
      </div>
    );
  }

  const data = settingsQuery.data;
  const fields = data.fields;

  const googleIdField = findField(fields, GOOGLE_ID_KEY);
  const googleSecretField = findField(fields, GOOGLE_SECRET_KEY);
  const feishuIdField = findField(fields, FEISHU_APP_ID_KEY);
  const feishuSecretField = findField(fields, FEISHU_APP_SECRET_KEY);
  const feishuEncryptField = findField(fields, FEISHU_ENCRYPT_KEY);
  const feishuVerifyField = findField(fields, FEISHU_VERIFY_TOKEN_KEY);

  const googleSource = resolveGroupSource(fields, 'google_oauth');
  const feishuAppSource = resolveGroupSource(fields, 'feishu_app');
  const feishuEventSource = resolveGroupSource(fields, 'feishu_event');

  const googleReadonly = googleSource === 'env';
  const feishuAppReadonly =
    feishuAppSource === 'env' ||
    feishuAppSource === 'vault_readonly' ||
    feishuAppSource === 'vault_error';
  const feishuEventReadonly = feishuEventSource === 'env';

  const booleanFields = fields.filter(
    (f) => f.input_kind === 'boolean' || f.input_kind === 'boolean_one',
  );

  const effectiveProfile = data.profile ?? profile;
  const profileLabel =
    effectiveProfile === 'core'
      ? t('settings:dalton.deploymentCredentials.profileCore')
      : t('settings:dalton.deploymentCredentials.profileManaged');

  const getTextValue = (field: DeploymentSettingField | undefined): string => {
    if (!field) return '';
    if (Object.hasOwn(drafts, field.name)) return drafts[field.name];
    return field.value ?? '';
  };

  const getSecretDraft = (name: string): string => drafts[name] ?? '';

  const setDraft = (name: string, val: string) => {
    setDrafts((prev) => ({ ...prev, [name]: val }));
  };

  const handleSaveGoogle = () => {
    setStatusNotice(null);
    const values: Record<string, string | null> = {};
    const idVal = getTextValue(googleIdField).trim();
    const secretVal = getSecretDraft(GOOGLE_SECRET_KEY).trim();
    values[GOOGLE_ID_KEY] = idVal || null;
    if (secretVal) {
      values[GOOGLE_SECRET_KEY] = secretVal;
    } else if (!googleSecretField?.configured) {
      values[GOOGLE_SECRET_KEY] = null;
    }
    saveMutation.mutate({ expected_revision: data.revision, values });
  };

  const handleClearGoogle = () => {
    setStatusNotice(null);
    saveMutation.mutate({
      expected_revision: data.revision,
      values: { [GOOGLE_ID_KEY]: null, [GOOGLE_SECRET_KEY]: null },
    });
  };

  const handleSaveFeishuApp = () => {
    setStatusNotice(null);
    const values: Record<string, string | null> = {};
    const idVal = getTextValue(feishuIdField).trim();
    const secretVal = getSecretDraft(FEISHU_APP_SECRET_KEY).trim();
    values[FEISHU_APP_ID_KEY] = idVal || null;
    if (secretVal) {
      values[FEISHU_APP_SECRET_KEY] = secretVal;
    } else if (!feishuSecretField?.configured) {
      values[FEISHU_APP_SECRET_KEY] = null;
    }
    saveMutation.mutate({ expected_revision: data.revision, values });
  };

  const handleClearFeishuApp = () => {
    setStatusNotice(null);
    saveMutation.mutate({
      expected_revision: data.revision,
      values: { [FEISHU_APP_ID_KEY]: null, [FEISHU_APP_SECRET_KEY]: null },
    });
  };

  const handleSaveFeishuEvent = () => {
    setStatusNotice(null);
    const values: Record<string, string | null> = {};
    const encVal = getSecretDraft(FEISHU_ENCRYPT_KEY).trim();
    const verifyVal = getSecretDraft(FEISHU_VERIFY_TOKEN_KEY).trim();
    if (encVal) values[FEISHU_ENCRYPT_KEY] = encVal;
    if (verifyVal) values[FEISHU_VERIFY_TOKEN_KEY] = verifyVal;
    if (Object.keys(values).length === 0) return;
    saveMutation.mutate({ expected_revision: data.revision, values });
  };

  const handleClearFeishuEvent = () => {
    setStatusNotice(null);
    saveMutation.mutate({
      expected_revision: data.revision,
      values: { [FEISHU_ENCRYPT_KEY]: null, [FEISHU_VERIFY_TOKEN_KEY]: null },
    });
  };

  const handleRunProbe = async (target: ProbeTarget) => {
    setProbingTarget(target);
    try {
      const probeDraft: Record<string, string | null> = {};
      if (target === 'google_oauth') {
        const draftId = drafts[GOOGLE_ID_KEY]?.trim();
        const draftSecret = drafts[GOOGLE_SECRET_KEY]?.trim();
        if (draftId) probeDraft[GOOGLE_ID_KEY] = draftId;
        if (draftSecret) probeDraft[GOOGLE_SECRET_KEY] = draftSecret;
      } else {
        const draftId = drafts[FEISHU_APP_ID_KEY]?.trim();
        const draftSecret = drafts[FEISHU_APP_SECRET_KEY]?.trim();
        if (draftId) probeDraft[FEISHU_APP_ID_KEY] = draftId;
        if (draftSecret) probeDraft[FEISHU_APP_SECRET_KEY] = draftSecret;
      }
      const res = await testDeploymentCredentials(target, probeDraft);
      setProbeResults((prev) => ({ ...prev, [target]: res }));
    } catch (err) {
      const parsed = parseDeploymentSettingsError(err);
      setStatusNotice({
        tone: 'error',
        text: formatSaveErrorMessage(parsed, t),
      });
    } finally {
      setProbingTarget(null);
    }
  };

  const handleToggleBoolean = (field: DeploymentSettingField, nextChecked: boolean) => {
    setStatusNotice(null);
    const encoded =
      field.input_kind === 'boolean_one'
        ? nextChecked
          ? '1'
          : null
        : nextChecked
          ? 'true'
          : 'false';
    saveMutation.mutate({
      expected_revision: data.revision,
      values: { [field.name]: encoded },
    });
  };

  const handleResetBoolean = (field: DeploymentSettingField) => {
    setStatusNotice(null);
    saveMutation.mutate({
      expected_revision: data.revision,
      values: { [field.name]: null },
    });
  };

  const isZh = i18n.language.startsWith('zh');
  const googleProbe = probeResults.google_oauth;
  const feishuProbe = probeResults.feishu_app;

  return (
    <div id="dalton-deployment-credentials" className="space-y-6" data-testid="deployment-credentials-panel">
      {/* Top status bar: deployment profile, encryption readiness, revision */}
      <DaltonSettingsCard
        title={t('settings:dalton.deploymentCredentials.statusTitle')}
        description={t('settings:dalton.deploymentCredentials.statusDesc')}
        action={
          <div className="flex items-center gap-2">
            <a
              href={ADMIN_INTEGRATIONS_HELP_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-[var(--color-accent-primary)] hover:underline"
            >
              <span>{t('settings:dalton.deploymentCredentials.helpGuideShort')}</span>
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
            <DaltonRefreshButton
              onClick={() => {
                setStatusNotice(null);
                void settingsQuery.refetch();
              }}
              busy={settingsQuery.isFetching}
              testId="deployment-credentials-refresh"
            />
          </div>
        }
      >
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <DaltonStatusBadge
            tone="neutral"
            label={`${t('settings:dalton.deploymentCredentials.profileLabel')}：${profileLabel}`}
            testId="deployment-credentials-profile-badge"
          />
          <DaltonStatusBadge
            tone={data.encryption_configured ? 'success' : 'danger'}
            dot
            label={
              data.encryption_configured
                ? t('settings:dalton.deploymentCredentials.encryptionReady')
                : t('settings:dalton.deploymentCredentials.encryptionMissing')
            }
            testId="deployment-credentials-encryption-badge"
          />
          <DaltonStatusBadge
            tone="neutral"
            label={`v${data.revision}`}
            testId="deployment-credentials-revision-badge"
          />
        </div>
        {statusNotice && (
          <div className="mt-3">
            <DaltonInlineNotice
              tone={statusNotice.tone}
              testId="deployment-credentials-notice"
              action={
                statusNotice.showRefresh ? (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() => {
                      setStatusNotice(null);
                      void settingsQuery.refetch();
                    }}
                  >
                    {t('settings:dalton.deploymentCredentials.reloadLatest')}
                  </Button>
                ) : undefined
              }
            >
              {statusNotice.text}
            </DaltonInlineNotice>
          </div>
        )}
      </DaltonSettingsCard>

      {/* Group 1: Google OAuth 2.0 client credentials */}
      <DaltonSettingsCard
        id="deployment-credentials-google"
        testId="google-oauth-card"
        title={t('settings:dalton.deploymentCredentials.googleTitle')}
        description={t('settings:dalton.deploymentCredentials.googleDesc')}
        action={
          <DaltonStatusBadge
            tone={SOURCE_TONE[googleSource]}
            dot
            label={sourceBadgeLabel(googleSource, t)}
            testId="google-oauth-source-badge"
          />
        }
      >
        <div className="space-y-4">
          {!data.google_redirect_uri_eligible && (
            <DaltonInlineNotice tone="warning" testId="google-redirect-uri-ineligible-banner">
              <div className="space-y-1">
                <div className="font-medium">
                  {t('settings:dalton.deploymentCredentials.googleIneligibleTitle')}
                </div>
                <div>{redirectReasonMessage(data.google_redirect_uri_reason, t)}</div>
              </div>
            </DaltonInlineNotice>
          )}

          <CopyableUrlRow
            label={t('settings:dalton.deploymentCredentials.googleCallbackLabel')}
            url={data.redirect_uris.google}
            testId="google-redirect-uri"
          />

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label
                htmlFor={`${baseId}-google-id`}
                className="text-xs font-medium text-[var(--color-text-secondary)]"
              >
                {isZh
                  ? (googleIdField?.label_zh ?? 'Google OAuth 客户端 ID')
                  : (googleIdField?.label_en ?? 'Google OAuth Client ID')}
              </label>
              <Input
                id={`${baseId}-google-id`}
                type="text"
                value={getTextValue(googleIdField)}
                disabled={googleReadonly || saveMutation.isPending}
                onChange={(e) => setDraft(GOOGLE_ID_KEY, e.target.value)}
                placeholder="1234567890-xxxx.apps.googleusercontent.com"
                autoComplete="off"
                data-testid="input-google-client-id"
              />
            </div>
            <div className="space-y-1">
              <label
                htmlFor={`${baseId}-google-secret`}
                className="text-xs font-medium text-[var(--color-text-secondary)]"
              >
                {isZh
                  ? (googleSecretField?.label_zh ?? 'Google OAuth 客户端密钥')
                  : (googleSecretField?.label_en ?? 'Google OAuth Client Secret')}
              </label>
              <Input
                id={`${baseId}-google-secret`}
                type="password"
                value={getSecretDraft(GOOGLE_SECRET_KEY)}
                disabled={googleReadonly || saveMutation.isPending}
                onChange={(e) => setDraft(GOOGLE_SECRET_KEY, e.target.value)}
                placeholder={
                  googleReadonly
                    ? '••••••••'
                    : googleSecretField?.configured
                      ? t('settings:dalton.deploymentCredentials.secretSavedPlaceholder')
                      : t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder')
                }
                autoComplete="new-password"
                data-testid="input-google-client-secret"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              disabled={googleReadonly || saveMutation.isPending}
              onClick={handleSaveGoogle}
              data-testid="save-google-oauth-btn"
            >
              {t('settings:dalton.deploymentCredentials.saveGroup')}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={probingTarget !== null || saveMutation.isPending}
              onClick={() => { void handleRunProbe('google_oauth'); }}
              data-testid="test-google-oauth-btn"
              className="gap-1.5"
            >
              {probingTarget === 'google_oauth' ? (
                <Loader size={12} label={t('settings:dalton.deploymentCredentials.testing')} />
              ) : (
                <FlaskConical className="h-3.5 w-3.5" />
              )}
              <span>{t('settings:dalton.deploymentCredentials.testConnectivity')}</span>
            </Button>
            {googleSource === 'saved' && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={saveMutation.isPending}
                onClick={handleClearGoogle}
                data-testid="clear-google-oauth-btn"
              >
                {t('settings:dalton.deploymentCredentials.clearGroup')}
              </Button>
            )}
          </div>

          {googleProbe && (
            <DaltonInlineNotice
              tone={formatProbeMessage(googleProbe, t).tone}
              testId="google-oauth-probe-result"
            >
              {formatProbeMessage(googleProbe, t).text}
            </DaltonInlineNotice>
          )}
        </div>
      </DaltonSettingsCard>

      {/* Group 2: Feishu Open Platform App & Event Verification Credentials */}
      <DaltonSettingsCard
        id="deployment-credentials-feishu"
        testId="feishu-credentials-card"
        title={t('settings:dalton.deploymentCredentials.feishuTitle')}
        description={t('settings:dalton.deploymentCredentials.feishuDesc')}
        action={
          <DaltonStatusBadge
            tone={SOURCE_TONE[feishuAppSource]}
            dot
            label={sourceBadgeLabel(feishuAppSource, t)}
            testId="feishu-app-source-badge"
          />
        }
      >
        <div className="space-y-5">
          <div className="space-y-3">
            <CopyableUrlRow
              label={t('settings:dalton.deploymentCredentials.feishuCallbackLabel')}
              url={data.redirect_uris.feishu}
              testId="feishu-redirect-uri"
            />
            <CopyableUrlRow
              label={t('settings:dalton.deploymentCredentials.feishuEventsLabel')}
              url={data.feishu_events_url}
              testId="feishu-events-url"
            />
          </div>

          {feishuAppSource === 'vault_readonly' && (
            <DaltonInlineNotice tone="warning" testId="feishu-vault-readonly-notice">
              {t('settings:dalton.deploymentCredentials.feishuVaultReadonlyHint')}
            </DaltonInlineNotice>
          )}
          {feishuAppSource === 'vault_error' && (
            <DaltonInlineNotice tone="error" testId="feishu-vault-error-notice">
              {t('settings:dalton.deploymentCredentials.feishuVaultErrorHint')}
            </DaltonInlineNotice>
          )}

          <div className="space-y-3">
            <div className="text-xs font-medium text-[var(--color-text-primary)]">
              {t('settings:dalton.deploymentCredentials.feishuAppGroupTitle')}
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <label
                  htmlFor={`${baseId}-feishu-id`}
                  className="text-xs font-medium text-[var(--color-text-secondary)]"
                >
                  {isZh
                    ? (feishuIdField?.label_zh ?? '飞书应用 App ID')
                    : (feishuIdField?.label_en ?? 'Feishu App ID')}
                </label>
                <Input
                  id={`${baseId}-feishu-id`}
                  type="text"
                  value={feishuAppSource === 'vault_error' ? '' : getTextValue(feishuIdField)}
                  disabled={feishuAppReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_APP_ID_KEY, e.target.value)}
                  placeholder={feishuAppSource === 'vault_error' ? '—' : 'cli_xxxxxxxxxxxxxxxx'}
                  autoComplete="off"
                  data-testid="input-feishu-app-id"
                />
              </div>
              <div className="space-y-1">
                <label
                  htmlFor={`${baseId}-feishu-secret`}
                  className="text-xs font-medium text-[var(--color-text-secondary)]"
                >
                  {isZh
                    ? (feishuSecretField?.label_zh ?? '飞书应用 App Secret')
                    : (feishuSecretField?.label_en ?? 'Feishu App Secret')}
                </label>
                <Input
                  id={`${baseId}-feishu-secret`}
                  type="password"
                  value={getSecretDraft(FEISHU_APP_SECRET_KEY)}
                  disabled={feishuAppReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_APP_SECRET_KEY, e.target.value)}
                  placeholder={
                    feishuAppReadonly
                      ? '••••••••'
                      : feishuSecretField?.configured
                        ? t('settings:dalton.deploymentCredentials.secretSavedPlaceholder')
                        : t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder')
                  }
                  autoComplete="new-password"
                  data-testid="input-feishu-app-secret"
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                size="sm"
                disabled={feishuAppReadonly || saveMutation.isPending}
                onClick={handleSaveFeishuApp}
                data-testid="save-feishu-app-btn"
              >
                {t('settings:dalton.deploymentCredentials.saveGroup')}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={
                  probingTarget !== null ||
                  saveMutation.isPending ||
                  feishuAppSource === 'vault_error'
                }
                onClick={() => { void handleRunProbe('feishu_app'); }}
                data-testid="test-feishu-app-btn"
                className="gap-1.5"
              >
                {probingTarget === 'feishu_app' ? (
                  <Loader size={12} label={t('settings:dalton.deploymentCredentials.testing')} />
                ) : (
                  <FlaskConical className="h-3.5 w-3.5" />
                )}
                <span>{t('settings:dalton.deploymentCredentials.testConnectivity')}</span>
              </Button>
              {feishuAppSource === 'saved' && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={saveMutation.isPending}
                  onClick={handleClearFeishuApp}
                  data-testid="clear-feishu-app-btn"
                >
                  {t('settings:dalton.deploymentCredentials.clearGroup')}
                </Button>
              )}
            </div>

            {feishuProbe && (
              <DaltonInlineNotice
                tone={formatProbeMessage(feishuProbe, t).tone}
                testId="feishu-app-probe-result"
              >
                {formatProbeMessage(feishuProbe, t).text}
              </DaltonInlineNotice>
            )}
          </div>

          <hr className="border-[var(--color-border-muted)]" />

          {/* Independent Feishu Event Verification Keys (editable even when feishu_app is vault_readonly / vault_error) */}
          <div className="space-y-3" data-testid="feishu-event-section">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-xs font-medium text-[var(--color-text-primary)]">
                  {t('settings:dalton.deploymentCredentials.feishuEventGroupTitle')}
                </div>
                <p className="text-xs text-[var(--color-text-tertiary)]">
                  {t('settings:dalton.deploymentCredentials.feishuEventGroupDesc')}
                </p>
              </div>
              <DaltonStatusBadge
                tone={SOURCE_TONE[feishuEventSource]}
                dot
                label={sourceBadgeLabel(feishuEventSource, t)}
                testId="feishu-event-source-badge"
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <label
                  htmlFor={`${baseId}-feishu-encrypt`}
                  className="text-xs font-medium text-[var(--color-text-secondary)]"
                >
                  {isZh
                    ? (feishuEncryptField?.label_zh ?? '飞书事件加密密钥（Encrypt Key）')
                    : (feishuEncryptField?.label_en ?? 'Feishu Event Encrypt Key')}
                </label>
                <Input
                  id={`${baseId}-feishu-encrypt`}
                  type="password"
                  value={getSecretDraft(FEISHU_ENCRYPT_KEY)}
                  disabled={feishuEventReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_ENCRYPT_KEY, e.target.value)}
                  placeholder={
                    feishuEventReadonly
                      ? '••••••••'
                      : feishuEncryptField?.configured
                        ? t('settings:dalton.deploymentCredentials.secretSavedPlaceholder')
                        : t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder')
                  }
                  autoComplete="new-password"
                  data-testid="input-feishu-encrypt-key"
                />
              </div>
              <div className="space-y-1">
                <label
                  htmlFor={`${baseId}-feishu-verify`}
                  className="text-xs font-medium text-[var(--color-text-secondary)]"
                >
                  {isZh
                    ? (feishuVerifyField?.label_zh ?? '飞书事件验证令牌（Verification Token）')
                    : (feishuVerifyField?.label_en ?? 'Feishu Event Verification Token')}
                </label>
                <Input
                  id={`${baseId}-feishu-verify`}
                  type="password"
                  value={getSecretDraft(FEISHU_VERIFY_TOKEN_KEY)}
                  disabled={feishuEventReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_VERIFY_TOKEN_KEY, e.target.value)}
                  placeholder={
                    feishuEventReadonly
                      ? '••••••••'
                      : feishuVerifyField?.configured
                        ? t('settings:dalton.deploymentCredentials.secretSavedPlaceholder')
                        : t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder')
                  }
                  autoComplete="new-password"
                  data-testid="input-feishu-verify-token"
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                size="sm"
                disabled={
                  feishuEventReadonly ||
                  saveMutation.isPending ||
                  (!getSecretDraft(FEISHU_ENCRYPT_KEY).trim() &&
                    !getSecretDraft(FEISHU_VERIFY_TOKEN_KEY).trim())
                }
                onClick={handleSaveFeishuEvent}
                data-testid="save-feishu-event-btn"
              >
                {t('settings:dalton.deploymentCredentials.saveEventKeys')}
              </Button>
              {feishuEventSource === 'saved' && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={saveMutation.isPending}
                  onClick={handleClearFeishuEvent}
                  data-testid="clear-feishu-event-btn"
                >
                  {t('settings:dalton.deploymentCredentials.clearGroup')}
                </Button>
              )}
            </div>
          </div>
        </div>
      </DaltonSettingsCard>

      {/* Tri-state capability switches (rendered dynamically when registered in catalog) */}
      {booleanFields.length > 0 && (
        <DaltonSettingsCard
          id="deployment-credentials-switches"
          testId="deployment-switches-card"
          title={t('settings:dalton.deploymentCredentials.switchesTitle')}
          description={t('settings:dalton.deploymentCredentials.switchesDesc')}
          layout="rows"
        >
          {booleanFields.map((field) => {
            const isGoogleSwitch =
              field.name.toUpperCase().includes('GOOGLE') ||
              field.section === 'calendar_mail';
            const googleBlocked = isGoogleSwitch && !data.google_redirect_uri_eligible;
            const checked = field.value === 'true' || field.value === '1';
            const disabled = field.readonly || googleBlocked || saveMutation.isPending;
            const label = isZh ? field.label_zh : field.label_en;
            return (
              <div
                key={field.name}
                className="settings-row is-inline"
                data-testid={`switch-row-${field.name}`}
              >
                <div className="settings-row-text">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="settings-row-label">{label}</span>
                    <DaltonStatusBadge
                      tone={SOURCE_TONE[field.source]}
                      label={sourceBadgeLabel(field.source, t)}
                    />
                  </div>
                  {googleBlocked && (
                    <p className="settings-row-desc">
                      {t('settings:dalton.deploymentCredentials.googleSwitchDisabledHint')}
                    </p>
                  )}
                </div>
                <div className="settings-row-control flex items-center gap-2">
                  {field.source === 'saved' && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={saveMutation.isPending}
                      onClick={() => handleResetBoolean(field)}
                      data-testid={`reset-switch-${field.name}`}
                    >
                      {t('settings:dalton.deploymentCredentials.restoreDefault')}
                    </Button>
                  )}
                  <ToggleSwitch
                    id={`${baseId}-sw-${field.name}`}
                    checked={checked}
                    disabled={disabled}
                    ariaLabel={label}
                    onChange={() => handleToggleBoolean(field, !checked)}
                  />
                </div>
              </div>
            );
          })}
        </DaltonSettingsCard>
      )}
    </div>
  );
}
