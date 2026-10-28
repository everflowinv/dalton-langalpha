/* eslint-disable react-refresh/only-export-components -- the form's hook and its fields belong together */
import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Input } from '@/components/ui/input';
import { ApiKeyInput } from '@/components/model/ApiKeyInput';
import { isOwnKey, type ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import { useUpdateApiKeys } from '@/hooks/useApiKeys';
import { useRefreshAfterAccessChange } from '@/hooks/useModelAccessActions';
import { testApiKey } from '@/api/model';
import { apiErrorDetail, apiErrorDetailMessage } from '@/api/errors';
import { getApiFormatKey } from './apiFormat';
import type { KeyOption } from './providerOptions';
import { UnverifiedNote } from './parts';
import { useEndpointVerdict } from './endpointTrust';

/** The refusal as the server worded it, else the caller's fallback. */
function refusalText(err: unknown, fallback: string): string {
  const detail = apiErrorDetail(err);
  if (typeof detail === 'string' && detail) return detail;
  return apiErrorDetailMessage(err) ?? fallback;
}

/**
 * The state behind an API-key form for one plan and region: its base URL and
 * the key itself. A saved key's base URL is where the form starts, not the
 * catalog default: the server stores whatever base URL comes with a key, so
 * starting from the default would quietly replace a custom one.
 */
export function useApiKeyForm(option: KeyOption, configured: readonly ConfiguredProvider[]) {
  const { t } = useTranslation();
  const updateApiKeys = useUpdateApiKeys();
  const refresh = useRefreshAfterAccessChange();
  const saved = configured.find((p) => p.provider === option.provider && p.access_type !== 'oauth') ?? null;
  const defaultUrl = option.base_url ?? '';
  // Only what the user typed is state: the saved URL is read through, so a
  // form opened before the saved keys arrive still saves the key with it.
  const [typedUrl, setBaseUrl] = useState<string | null>(null);
  const baseUrl = typedUrl ?? (saved?.base_url || defaultUrl);
  const [apiKey, setApiKey] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const local = option.entry.access_type === 'local';
  const baseChanged = baseUrl.trim() !== (saved?.base_url || defaultUrl);
  // A deployment's own key has no row of the user's to carry a base URL, so
  // moving the URL takes a key of their own.
  const ownKey = isOwnKey(saved);

  const canSave = local
    ? !!baseUrl.trim()
    : !!apiKey.trim() || (ownKey && baseChanged && !!baseUrl.trim());

  /** Saves and refreshes what the change makes stale; resolves to the provider
   *  the key was saved under, or null when the save was refused. */
  const save = async (): Promise<string | null> => {
    if (!canSave || saving) return null;
    setSaving(true);
    setError(null);
    const url = baseUrl.trim();
    // Only an override is stored: a base URL equal to the default is left
    // unsaved, so the key follows the catalog if the default ever moves.
    const override = url && url !== defaultUrl ? url : null;
    const payload: Record<string, unknown> = { byok_enabled: true };
    // A local server saved before keeps its key when the field is left blank,
    // as any saved key does: a blank key in the payload would replace it. One
    // with no key of the user's (the deployment's server, or keyless) has no
    // row a URL alone would land on, so the save writes one.
    if (apiKey.trim() || (local && !ownKey)) {
      payload.api_keys = { [option.provider]: apiKey.trim() };
      if (override || local) payload.base_urls = { [option.provider]: url };
    } else {
      // Only the base URL changed on a key already saved.
      payload.base_urls = { [option.provider]: local ? url : override };
    }
    try {
      await updateApiKeys.mutateAsync(payload);
      await refresh(option.provider);
    } catch (err) {
      setError(refusalText(err, t('modelAccess.apiKey.saveFailed')));
      setSaving(false);
      return null;
    }
    setSaving(false);
    setApiKey('');
    return option.provider;
  };

  const runTest = async (_provider: string, key: string) => {
    try {
      return await testApiKey(option.provider, key, baseUrl.trim() || undefined);
    } catch (err) {
      return { success: false, error: refusalText(err, t('setup.testFailed')) };
    }
  };

  return {
    option,
    saved,
    ownKey,
    baseUrl,
    setBaseUrl,
    apiKey,
    setApiKey,
    local,
    canSave,
    saving,
    error,
    save,
    runTest,
  };
}

export type ApiKeyFormState = ReturnType<typeof useApiKeyForm>;

/** The form's fields: the base URL and its API format, then the key and its test. */
export function ApiKeyFormFields({ form }: { form: ApiKeyFormState }) {
  const { t } = useTranslation();
  const baseUrlId = useId();
  const keyId = useId();
  const { option, saved, ownKey, baseUrl, local } = form;
  const { entry } = option;
  const defaultUrl = option.base_url ?? '';
  const verdict = useEndpointVerdict(baseUrl);
  const proxied = !local && baseUrl.trim() !== defaultUrl && verdict === false;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <label htmlFor={baseUrlId} className="text-xs font-medium" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('setup.baseUrlLabel')}
            <span className="ml-1.5 font-normal">· {t(getApiFormatKey(option.sdk, option.use_response_api), { sdk: option.sdk })}</span>
          </label>
          {defaultUrl && baseUrl !== defaultUrl && (
            <button
              type="button"
              onClick={() => form.setBaseUrl(defaultUrl)}
              className="text-xs underline underline-offset-2 hover:opacity-80"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {t('setup.resetToDefault')}
            </button>
          )}
        </div>
        <Input
          id={baseUrlId}
          type="url"
          value={baseUrl}
          onChange={(e) => form.setBaseUrl(e.target.value)}
          placeholder={defaultUrl || 'https://…'}
          className="font-mono text-xs"
        />
        {proxied ? (
          <UnverifiedNote>{t('setup.proxyUnverifiedBody', { provider: entry.group_display_name ?? entry.display_name })}</UnverifiedNote>
        ) : defaultUrl && baseUrl.trim() && baseUrl.trim() !== defaultUrl && (
          <p className="text-xs" style={{ color: 'var(--color-warning)' }}>
            {t('setup.customUrlWarning', { url: defaultUrl })}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor={keyId} className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('setup.providerApiKey', { provider: entry.display_name })}
          {local && (
            <span className="ml-1.5 text-xs font-normal" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('setup.optional')}
            </span>
          )}
        </label>
        <ApiKeyInput
          id={keyId}
          provider={option.provider}
          value={form.apiKey}
          onChange={form.setApiKey}
          maskedKey={ownKey ? saved?.masked_key : null}
          baseUrl={baseUrl}
          optional={local}
          onTest={local ? undefined : form.runTest}
        />
        {ownKey && !local && (
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('modelAccess.apiKey.keepSaved')}
          </p>
        )}
      </div>

      {form.error && (
        <p role="alert" className="text-sm" style={{ color: 'var(--color-loss)' }}>
          {form.error}
        </p>
      )}
    </div>
  );
}
