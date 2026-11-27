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

type EditableGroup = 'google_oauth' | 'feishu_app' | 'feishu_event';

const GROUP_FIELDS: Record<EditableGroup, readonly string[]> = {
  google_oauth: [GOOGLE_ID_KEY, GOOGLE_SECRET_KEY],
  feishu_app: [FEISHU_APP_ID_KEY, FEISHU_APP_SECRET_KEY],
  feishu_event: [FEISHU_ENCRYPT_KEY, FEISHU_VERIFY_TOKEN_KEY],
};

const FIELD_TO_GROUP: Record<string, EditableGroup> = {
  [GOOGLE_ID_KEY]: 'google_oauth',
  [GOOGLE_SECRET_KEY]: 'google_oauth',
  [FEISHU_APP_ID_KEY]: 'feishu_app',
  [FEISHU_APP_SECRET_KEY]: 'feishu_app',
  [FEISHU_ENCRYPT_KEY]: 'feishu_event',
  [FEISHU_VERIFY_TOKEN_KEY]: 'feishu_event',
};

/**
 * Explicit registry of capability switch keys gated on Google redirect URI eligibility,
 * aligned with the personal calendar/mail integration capability constants.
 */
const GOOGLE_CONNECTOR_SWITCH_KEYS: ReadonlySet<string> = new Set([
  'DALTON_CAP_GOOGLE_WORKSPACE',
  'DALTON_CAP_GOOGLE_CALENDAR',
  'DALTON_CAP_GOOGLE_MAIL',
]);

function isGoogleConnectorSwitch(field: DeploymentSettingField): boolean {
  return GOOGLE_CONNECTOR_SWITCH_KEYS.has(field.name) || field.section === 'calendar_mail';
}

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

function sourceShortLabel(
  source: CredentialSource,
  t: (key: string) => string,
): string | null {
  switch (source) {
    case 'env':
      return t('settings:dalton.deploymentCredentials.sourceShortEnv');
    case 'saved':
      return t('settings:dalton.deploymentCredentials.sourceShortSaved');
    case 'vault_readonly':
      return t('settings:dalton.deploymentCredentials.sourceShortVaultReadonly');
    case 'vault_error':
      return t('settings:dalton.deploymentCredentials.sourceShortVaultError');
    case 'unset':
      return null;
  }
}

function FieldStatusBadge({
  field,
  testId,
}: {
  field: DeploymentSettingField | undefined;
  testId: string;
}) {
  const { t } = useTranslation();
  const source: CredentialSource = field?.source ?? 'unset';
  if (source === 'vault_error') {
    return (
      <DaltonStatusBadge
        tone="danger"
        label={t('settings:dalton.deploymentCredentials.fieldReadError')}
        testId={testId}
      />
    );
  }
  if (field?.configured) {
    const shortSource = sourceShortLabel(source, t);
    const label = shortSource
      ? `${t('settings:dalton.deploymentCredentials.fieldConfigured')} · ${shortSource}`
      : t('settings:dalton.deploymentCredentials.fieldConfigured');
    return <DaltonStatusBadge tone={SOURCE_TONE[source]} label={label} testId={testId} />;
  }
  return (
    <DaltonStatusBadge
      tone="neutral"
      label={t('settings:dalton.deploymentCredentials.fieldUnset')}
      testId={testId}
    />
  );
}

function resolveFieldPlaceholder(
  field: DeploymentSettingField | undefined,
  options: {
    readonly: boolean;
    secret: boolean;
    emptyPlaceholder: string;
    t: (key: string) => string;
  },
): string {
  const { readonly, secret, emptyPlaceholder, t } = options;
  if (field?.source === 'vault_error') {
    return t('settings:dalton.deploymentCredentials.vaultErrorPlaceholder');
  }
  if (readonly) {
    if (field?.configured) {
      return secret
        ? '••••••••'
        : t('settings:dalton.deploymentCredentials.readonlyConfiguredPlaceholder');
    }
    return t('settings:dalton.deploymentCredentials.readonlyUnsetPlaceholder');
  }
  if (field?.configured) {
    return secret
      ? t('settings:dalton.deploymentCredentials.secretSavedPlaceholder')
      : t('settings:dalton.deploymentCredentials.idSavedPlaceholder');
  }
  return emptyPlaceholder;
}

/**
 * Shared draft-to-payload resolver for both Save (`PUT`) and Connectivity Test (`POST /test`).
 *
 * - Non-empty drafts submit the trimmed replacement value.
 * - Empty drafts on already-configured fields omit the key so both Save and Test preserve the
 *   existing server-side value.
 * - Empty drafts on unconfigured fields emit `null` so incomplete pairs produce identical
 *   validation verdicts across Save and Test.
 */
function buildPairedGroupPayload(
  idKey: string,
  secretKey: string,
  idField: DeploymentSettingField | undefined,
  secretField: DeploymentSettingField | undefined,
  drafts: Readonly<Record<string, string>>,
): Record<string, string | null> {
  const values: Record<string, string | null> = {};
  const idVal = (drafts[idKey] ?? '').trim();
  const secretVal = (drafts[secretKey] ?? '').trim();

  if (idVal) {
    values[idKey] = idVal;
  } else if (!idField?.configured) {
    values[idKey] = null;
  }

  if (secretVal) {
    values[secretKey] = secretVal;
  } else if (!secretField?.configured) {
    values[secretKey] = null;
  }

  return values;
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

function formatProbeErrorMessage(
  parsed: ParsedDeploymentSettingsError,
  t: (key: string) => string,
): string {
  if (parsed.isTimeout) {
    return t('settings:dalton.deploymentCredentials.probeClientTimeout');
  }
  if (parsed.isNetworkError) {
    return t('settings:dalton.deploymentCredentials.probeRequestNetworkError');
  }
  if (parsed.status === 401 || parsed.status === 403) {
    return t('settings:dalton.deploymentCredentials.probeAuthError');
  }
  if (parsed.status === 422) {
    return t('settings:dalton.deploymentCredentials.probeFormatInvalid');
  }
  return t('settings:dalton.deploymentCredentials.errProbeGeneric');
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
    if (result.detail_code === 'network_timeout' || result.detail_code === 'probe_timeout') {
      return {
        tone: 'error',
        text: t('settings:dalton.deploymentCredentials.probeTimeout'),
      };
    }
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
  const { profile, brandName } = useCapabilities();
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

  // Draft values keyed by env name (inputs always stay unprefilled; drafts hold only user edits).
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // Each edited group records the revision active when editing began so concurrent saves never bypass 409.
  const [draftBaseRevisions, setDraftBaseRevisions] = useState<
    Partial<Record<EditableGroup, number>>
  >({});
  const [statusNotice, setStatusNotice] = useState<{
    tone: 'success' | 'error' | 'warning' | 'info';
    text: string;
    showRefresh?: boolean;
  } | null>(null);
  const [probeResults, setProbeResults] = useState<
    Partial<Record<ProbeTarget, { revision: number; result: DeploymentCredentialProbeResponse }>>
  >({});
  const [probeErrors, setProbeErrors] = useState<
    Partial<Record<ProbeTarget, { revision: number; text: string }>>
  >({});
  const [probingTarget, setProbingTarget] = useState<ProbeTarget | null>(null);

  const saveMutation = useMutation({
    mutationFn: (payload: Parameters<typeof updateDeploymentSettings>[0]) =>
      updateDeploymentSettings(payload),
    onSuccess: (res, variables) => {
      const touchedGroups = new Set<EditableGroup>();
      setDrafts((prev) => {
        const next = { ...prev };
        for (const key of Object.keys(variables.values)) {
          delete next[key];
          const grp = FIELD_TO_GROUP[key];
          if (grp) touchedGroups.add(grp);
        }
        return next;
      });
      if (touchedGroups.size > 0) {
        setDraftBaseRevisions((prev) => {
          const next = { ...prev };
          for (const grp of touchedGroups) {
            delete next[grp];
          }
          return next;
        });
      }
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

  // Profile and brand always come from the single frontend capability source (`useCapabilities()`).
  const profileLabel =
    profile === 'core'
      ? t('settings:dalton.deploymentCredentials.profileCore')
      : brandName;

  const getDraftValue = (name: string): string => drafts[name] ?? '';

  const clearProbeStateForGroup = (group: EditableGroup) => {
    if (group === 'google_oauth' || group === 'feishu_app') {
      setProbeResults((prev) => {
        if (!prev[group]) return prev;
        const next = { ...prev };
        delete next[group];
        return next;
      });
      setProbeErrors((prev) => {
        if (!prev[group]) return prev;
        const next = { ...prev };
        delete next[group];
        return next;
      });
    }
  };

  const setDraft = (name: string, val: string) => {
    const group = FIELD_TO_GROUP[name];
    setDrafts((prev) => ({ ...prev, [name]: val }));
    if (group) {
      setDraftBaseRevisions((prev) =>
        prev[group] === undefined ? { ...prev, [group]: data.revision } : prev,
      );
      clearProbeStateForGroup(group);
    }
  };

  const isGroupDraftStale = (group: EditableGroup): boolean => {
    const baseRev = draftBaseRevisions[group];
    return baseRev !== undefined && baseRev !== data.revision;
  };

  const discardGroupDraft = (group: EditableGroup) => {
    setDrafts((prev) => {
      const next = { ...prev };
      for (const key of GROUP_FIELDS[group]) {
        delete next[key];
      }
      return next;
    });
    setDraftBaseRevisions((prev) => {
      const next = { ...prev };
      delete next[group];
      return next;
    });
    clearProbeStateForGroup(group);
  };

  const rebaselineGroupDraft = (group: EditableGroup) => {
    setDraftBaseRevisions((prev) => ({ ...prev, [group]: data.revision }));
    clearProbeStateForGroup(group);
  };

  const googleDraftStale = isGroupDraftStale('google_oauth');
  const feishuAppDraftStale = isGroupDraftStale('feishu_app');
  const feishuEventDraftStale = isGroupDraftStale('feishu_event');

  const handleSaveGoogle = () => {
    setStatusNotice(null);
    const values = buildPairedGroupPayload(
      GOOGLE_ID_KEY,
      GOOGLE_SECRET_KEY,
      googleIdField,
      googleSecretField,
      drafts,
    );
    saveMutation.mutate({
      expected_revision: draftBaseRevisions.google_oauth ?? data.revision,
      values,
    });
  };

  const handleClearGoogle = () => {
    setStatusNotice(null);
    clearProbeStateForGroup('google_oauth');
    saveMutation.mutate({
      expected_revision: draftBaseRevisions.google_oauth ?? data.revision,
      values: { [GOOGLE_ID_KEY]: null, [GOOGLE_SECRET_KEY]: null },
    });
  };

  const handleSaveFeishuApp = () => {
    setStatusNotice(null);
    const values = buildPairedGroupPayload(
      FEISHU_APP_ID_KEY,
      FEISHU_APP_SECRET_KEY,
      feishuIdField,
      feishuSecretField,
      drafts,
    );
    saveMutation.mutate({
      expected_revision: draftBaseRevisions.feishu_app ?? data.revision,
      values,
    });
  };

  const handleClearFeishuApp = () => {
    setStatusNotice(null);
    clearProbeStateForGroup('feishu_app');
    saveMutation.mutate({
      expected_revision: draftBaseRevisions.feishu_app ?? data.revision,
      values: { [FEISHU_APP_ID_KEY]: null, [FEISHU_APP_SECRET_KEY]: null },
    });
  };

  const handleSaveFeishuEvent = () => {
    setStatusNotice(null);
    const values: Record<string, string | null> = {};
    const encVal = getDraftValue(FEISHU_ENCRYPT_KEY).trim();
    const verifyVal = getDraftValue(FEISHU_VERIFY_TOKEN_KEY).trim();
    if (encVal) values[FEISHU_ENCRYPT_KEY] = encVal;
    if (verifyVal) values[FEISHU_VERIFY_TOKEN_KEY] = verifyVal;
    if (Object.keys(values).length === 0) return;
    saveMutation.mutate({
      expected_revision: draftBaseRevisions.feishu_event ?? data.revision,
      values,
    });
  };

  const handleClearFeishuEvent = () => {
    setStatusNotice(null);
    saveMutation.mutate({
      expected_revision: draftBaseRevisions.feishu_event ?? data.revision,
      values: { [FEISHU_ENCRYPT_KEY]: null, [FEISHU_VERIFY_TOKEN_KEY]: null },
    });
  };

  const handleRunProbe = async (target: ProbeTarget) => {
    const requestRevision = data.revision;
    setProbingTarget(target);
    clearProbeStateForGroup(target);
    try {
      const probePayload =
        target === 'google_oauth'
          ? buildPairedGroupPayload(
              GOOGLE_ID_KEY,
              GOOGLE_SECRET_KEY,
              googleIdField,
              googleSecretField,
              drafts,
            )
          : buildPairedGroupPayload(
              FEISHU_APP_ID_KEY,
              FEISHU_APP_SECRET_KEY,
              feishuIdField,
              feishuSecretField,
              drafts,
            );
      const res = await testDeploymentCredentials(target, probePayload);
      setProbeResults((prev) => ({
        ...prev,
        [target]: { revision: requestRevision, result: res },
      }));
    } catch (err) {
      const parsed = parseDeploymentSettingsError(err);
      setProbeErrors((prev) => ({
        ...prev,
        [target]: {
          revision: requestRevision,
          text: formatProbeErrorMessage(parsed, t),
        },
      }));
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
  // Probe outputs automatically expire whenever the server revision advances.
  const googleProbe =
    probeResults.google_oauth?.revision === data.revision
      ? probeResults.google_oauth.result
      : undefined;
  const googleProbeError =
    probeErrors.google_oauth?.revision === data.revision
      ? probeErrors.google_oauth.text
      : undefined;
  const feishuProbe =
    probeResults.feishu_app?.revision === data.revision
      ? probeResults.feishu_app.result
      : undefined;
  const feishuProbeError =
    probeErrors.feishu_app?.revision === data.revision
      ? probeErrors.feishu_app.text
      : undefined;

  const renderStaleDraftBanner = (group: EditableGroup) => {
    const baseRev = draftBaseRevisions[group];
    if (baseRev === undefined || baseRev === data.revision) return null;
    return (
      <DaltonInlineNotice tone="warning" testId={`${group}-stale-draft-notice`}>
        <div className="space-y-2">
          <div>
            {t('settings:dalton.deploymentCredentials.staleDraftNotice', {
              current: data.revision,
              base: baseRev,
            })}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              data-testid={`${group}-discard-draft-btn`}
              onClick={() => discardGroupDraft(group)}
            >
              {t('settings:dalton.deploymentCredentials.discardDraft')}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              data-testid={`${group}-rebase-draft-btn`}
              onClick={() => rebaselineGroupDraft(group)}
            >
              {t('settings:dalton.deploymentCredentials.confirmDraftRebase', {
                revision: data.revision,
              })}
            </Button>
          </div>
        </div>
      </DaltonInlineNotice>
    );
  };

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
          <div className="flex flex-wrap items-center gap-1.5">
            {booleanFields.length === 0 && (
              <DaltonStatusBadge
                tone="neutral"
                label={t('settings:dalton.deploymentCredentials.personalConnectorComingSoonBadge')}
                testId="google-connector-availability-badge"
              />
            )}
            <DaltonStatusBadge
              tone={SOURCE_TONE[googleSource]}
              dot
              label={sourceBadgeLabel(googleSource, t)}
              testId="google-oauth-source-badge"
            />
          </div>
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

          {booleanFields.length === 0 && (
            <p className="text-xs text-[var(--color-text-tertiary)]" data-testid="google-callback-availability-hint">
              {t('settings:dalton.deploymentCredentials.googleCallbackComingSoonHint')}
            </p>
          )}

          <CopyableUrlRow
            label={t('settings:dalton.deploymentCredentials.googleCallbackLabel')}
            url={data.redirect_uris.google}
            testId="google-redirect-uri"
          />

          {renderStaleDraftBanner('google_oauth')}

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <label
                  htmlFor={`${baseId}-google-id`}
                  className="text-xs font-medium text-[var(--color-text-secondary)]"
                >
                  {isZh
                    ? (googleIdField?.label_zh ?? 'Google OAuth 客户端 ID')
                    : (googleIdField?.label_en ?? 'Google OAuth Client ID')}
                </label>
                <FieldStatusBadge
                  field={googleIdField}
                  testId={`field-status-${GOOGLE_ID_KEY}`}
                />
              </div>
              <Input
                id={`${baseId}-google-id`}
                type="text"
                value={getDraftValue(GOOGLE_ID_KEY)}
                disabled={googleReadonly || saveMutation.isPending}
                onChange={(e) => setDraft(GOOGLE_ID_KEY, e.target.value)}
                placeholder={resolveFieldPlaceholder(googleIdField, {
                  readonly: googleReadonly,
                  secret: false,
                  emptyPlaceholder: '1234567890-xxxx.apps.googleusercontent.com',
                  t,
                })}
                autoComplete="off"
                data-testid="input-google-client-id"
              />
            </div>
            <div className="space-y-1">
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <label
                  htmlFor={`${baseId}-google-secret`}
                  className="text-xs font-medium text-[var(--color-text-secondary)]"
                >
                  {isZh
                    ? (googleSecretField?.label_zh ?? 'Google OAuth 客户端密钥')
                    : (googleSecretField?.label_en ?? 'Google OAuth Client Secret')}
                </label>
                <FieldStatusBadge
                  field={googleSecretField}
                  testId={`field-status-${GOOGLE_SECRET_KEY}`}
                />
              </div>
              <Input
                id={`${baseId}-google-secret`}
                type="password"
                value={getDraftValue(GOOGLE_SECRET_KEY)}
                disabled={googleReadonly || saveMutation.isPending}
                onChange={(e) => setDraft(GOOGLE_SECRET_KEY, e.target.value)}
                placeholder={resolveFieldPlaceholder(googleSecretField, {
                  readonly: googleReadonly,
                  secret: true,
                  emptyPlaceholder: t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder'),
                  t,
                })}
                autoComplete="new-password"
                data-testid="input-google-client-secret"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              disabled={googleReadonly || googleDraftStale || saveMutation.isPending}
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
                disabled={googleDraftStale || saveMutation.isPending}
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
          {googleProbeError && (
            <DaltonInlineNotice tone="error" testId="google-oauth-probe-error">
              {googleProbeError}
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
          <div className="flex flex-wrap items-center gap-1.5">
            {booleanFields.length === 0 && (
              <DaltonStatusBadge
                tone="neutral"
                label={t('settings:dalton.deploymentCredentials.personalConnectorComingSoonBadge')}
                testId="feishu-connector-availability-badge"
              />
            )}
            <DaltonStatusBadge
              tone={SOURCE_TONE[feishuAppSource]}
              dot
              label={sourceBadgeLabel(feishuAppSource, t)}
              testId="feishu-app-source-badge"
            />
          </div>
        }
      >
        <div className="space-y-5">
          <div className="space-y-3">
            {booleanFields.length === 0 && (
              <p className="text-xs text-[var(--color-text-tertiary)]" data-testid="feishu-callback-availability-hint">
                {t('settings:dalton.deploymentCredentials.feishuCallbackComingSoonHint')}
              </p>
            )}
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

            {renderStaleDraftBanner('feishu_app')}

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <label
                    htmlFor={`${baseId}-feishu-id`}
                    className="text-xs font-medium text-[var(--color-text-secondary)]"
                  >
                    {isZh
                      ? (feishuIdField?.label_zh ?? '飞书应用 App ID')
                      : (feishuIdField?.label_en ?? 'Feishu App ID')}
                  </label>
                  <FieldStatusBadge
                    field={feishuIdField}
                    testId={`field-status-${FEISHU_APP_ID_KEY}`}
                  />
                </div>
                <Input
                  id={`${baseId}-feishu-id`}
                  type="text"
                  value={getDraftValue(FEISHU_APP_ID_KEY)}
                  disabled={feishuAppReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_APP_ID_KEY, e.target.value)}
                  placeholder={resolveFieldPlaceholder(feishuIdField, {
                    readonly: feishuAppReadonly,
                    secret: false,
                    emptyPlaceholder: 'cli_xxxxxxxxxxxxxxxx',
                    t,
                  })}
                  autoComplete="off"
                  data-testid="input-feishu-app-id"
                />
              </div>
              <div className="space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <label
                    htmlFor={`${baseId}-feishu-secret`}
                    className="text-xs font-medium text-[var(--color-text-secondary)]"
                  >
                    {isZh
                      ? (feishuSecretField?.label_zh ?? '飞书应用 App Secret')
                      : (feishuSecretField?.label_en ?? 'Feishu App Secret')}
                  </label>
                  <FieldStatusBadge
                    field={feishuSecretField}
                    testId={`field-status-${FEISHU_APP_SECRET_KEY}`}
                  />
                </div>
                <Input
                  id={`${baseId}-feishu-secret`}
                  type="password"
                  value={getDraftValue(FEISHU_APP_SECRET_KEY)}
                  disabled={feishuAppReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_APP_SECRET_KEY, e.target.value)}
                  placeholder={resolveFieldPlaceholder(feishuSecretField, {
                    readonly: feishuAppReadonly,
                    secret: true,
                    emptyPlaceholder: t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder'),
                    t,
                  })}
                  autoComplete="new-password"
                  data-testid="input-feishu-app-secret"
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                size="sm"
                disabled={feishuAppReadonly || feishuAppDraftStale || saveMutation.isPending}
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
                  disabled={feishuAppDraftStale || saveMutation.isPending}
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
            {feishuProbeError && (
              <DaltonInlineNotice tone="error" testId="feishu-app-probe-error">
                {feishuProbeError}
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

            {renderStaleDraftBanner('feishu_event')}

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <label
                    htmlFor={`${baseId}-feishu-encrypt`}
                    className="text-xs font-medium text-[var(--color-text-secondary)]"
                  >
                    {isZh
                      ? (feishuEncryptField?.label_zh ?? '飞书事件加密密钥（Encrypt Key）')
                      : (feishuEncryptField?.label_en ?? 'Feishu Event Encrypt Key')}
                  </label>
                  <FieldStatusBadge
                    field={feishuEncryptField}
                    testId={`field-status-${FEISHU_ENCRYPT_KEY}`}
                  />
                </div>
                <Input
                  id={`${baseId}-feishu-encrypt`}
                  type="password"
                  value={getDraftValue(FEISHU_ENCRYPT_KEY)}
                  disabled={feishuEventReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_ENCRYPT_KEY, e.target.value)}
                  placeholder={resolveFieldPlaceholder(feishuEncryptField, {
                    readonly: feishuEventReadonly,
                    secret: true,
                    emptyPlaceholder: t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder'),
                    t,
                  })}
                  autoComplete="new-password"
                  data-testid="input-feishu-encrypt-key"
                />
              </div>
              <div className="space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <label
                    htmlFor={`${baseId}-feishu-verify`}
                    className="text-xs font-medium text-[var(--color-text-secondary)]"
                  >
                    {isZh
                      ? (feishuVerifyField?.label_zh ?? '飞书事件验证令牌（Verification Token）')
                      : (feishuVerifyField?.label_en ?? 'Feishu Event Verification Token')}
                  </label>
                  <FieldStatusBadge
                    field={feishuVerifyField}
                    testId={`field-status-${FEISHU_VERIFY_TOKEN_KEY}`}
                  />
                </div>
                <Input
                  id={`${baseId}-feishu-verify`}
                  type="password"
                  value={getDraftValue(FEISHU_VERIFY_TOKEN_KEY)}
                  disabled={feishuEventReadonly || saveMutation.isPending}
                  onChange={(e) => setDraft(FEISHU_VERIFY_TOKEN_KEY, e.target.value)}
                  placeholder={resolveFieldPlaceholder(feishuVerifyField, {
                    readonly: feishuEventReadonly,
                    secret: true,
                    emptyPlaceholder: t('settings:dalton.deploymentCredentials.secretEmptyPlaceholder'),
                    t,
                  })}
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
                  feishuEventDraftStale ||
                  saveMutation.isPending ||
                  (!getDraftValue(FEISHU_ENCRYPT_KEY).trim() &&
                    !getDraftValue(FEISHU_VERIFY_TOKEN_KEY).trim())
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
                  disabled={feishuEventDraftStale || saveMutation.isPending}
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

      {/* Connector capability switches: live tri-state switches once registered, or coming-soon status in Batch 1 */}
      <DaltonSettingsCard
        id="deployment-credentials-switches"
        testId="deployment-switches-card"
        title={t('settings:dalton.deploymentCredentials.switchesTitle')}
        description={t('settings:dalton.deploymentCredentials.switchesDesc')}
        layout="rows"
      >
        {booleanFields.length > 0 ? (
          booleanFields.map((field) => {
            const googleBlocked =
              isGoogleConnectorSwitch(field) && !data.google_redirect_uri_eligible;
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
          })
        ) : (
          <>
            <div
              className="settings-row is-inline"
              data-testid="switch-row-coming-soon-google"
            >
              <div className="settings-row-text">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="settings-row-label">
                    {t('settings:dalton.deploymentCredentials.switchGoogleCalendarLabel')}
                  </span>
                  <DaltonStatusBadge
                    tone="neutral"
                    label={t('settings:dalton.deploymentCredentials.comingSoonBadge')}
                  />
                </div>
                <p className="settings-row-desc">
                  {!data.google_redirect_uri_eligible
                    ? t('settings:dalton.deploymentCredentials.googleSwitchDisabledHint')
                    : t('settings:dalton.deploymentCredentials.switchGoogleCalendarDesc')}
                </p>
              </div>
              <div className="settings-row-control">
                <ToggleSwitch
                  id={`${baseId}-sw-google-coming-soon`}
                  checked={false}
                  disabled
                  ariaLabel={t('settings:dalton.deploymentCredentials.switchGoogleCalendarLabel')}
                  onChange={() => {}}
                />
              </div>
            </div>
            <div
              className="settings-row is-inline"
              data-testid="switch-row-coming-soon-feishu"
            >
              <div className="settings-row-text">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="settings-row-label">
                    {t('settings:dalton.deploymentCredentials.switchFeishuCalendarLabel')}
                  </span>
                  <DaltonStatusBadge
                    tone="neutral"
                    label={t('settings:dalton.deploymentCredentials.comingSoonBadge')}
                  />
                </div>
                <p className="settings-row-desc">
                  {t('settings:dalton.deploymentCredentials.switchFeishuCalendarDesc')}
                </p>
              </div>
              <div className="settings-row-control">
                <ToggleSwitch
                  id={`${baseId}-sw-feishu-coming-soon`}
                  checked={false}
                  disabled
                  ariaLabel={t('settings:dalton.deploymentCredentials.switchFeishuCalendarLabel')}
                  onChange={() => {}}
                />
              </div>
            </div>
          </>
        )}
      </DaltonSettingsCard>
    </div>
  );
}
