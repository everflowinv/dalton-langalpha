import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import { ApiKeyFormFields, useApiKeyForm } from '@/components/modelAccess/ApiKeyForm';
import { BackButton, KeyStepFrame } from '@/components/modelAccess/KeyProviderPicker';
import { optionName } from '@/components/modelAccess/optionLabels';
import type { KeyOption, ProviderGroup } from '@/components/modelAccess/providerOptions';

/** Adding or updating a key, then on to its models. */
export function KeySheet({
  option,
  group,
  configured,
  onSaved,
  onCancel,
  onBack,
}: {
  /** The plan and region the key is for. */
  option: KeyOption;
  /** The provider it was picked from in the add-key flow: the form is then
   *  one step of that flow, at its fixed size, naming a choice among several. */
  group?: ProviderGroup;
  configured: ConfiguredProvider[];
  onSaved: (provider: string) => void;
  /** Back to the saved key, where the form edits one. */
  onCancel?: () => void;
  /** Back to the step that picked the provider. */
  onBack?: () => void;
}) {
  const { t } = useTranslation();
  const form = useApiKeyForm(option, configured);
  const fields = (
    <>
      {group && group.options.length > 1 && (
        <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{optionName(t, option)}</span>
      )}
      <ApiKeyFormFields form={form} />
    </>
  );
  const buttons = (
    <>
      {onBack && <BackButton onClick={onBack} disabled={form.saving} className="mr-auto" />}
      {onCancel && (
        <Button type="button" variant="ghost" onClick={onCancel} disabled={form.saving}>
          {t('common.cancel')}
        </Button>
      )}
      <Button
        type="button"
        disabled={!form.canSave || form.saving}
        aria-busy={form.saving}
        onClick={async () => {
          const saved = await form.save();
          if (saved) onSaved(saved);
        }}
      >
        {form.saving ? t('modelAccess.apiKey.saving') : form.saved ? t('modelAccess.apiKey.update') : t('modelAccess.apiKey.save')}
      </Button>
    </>
  );
  return group ? (
    <KeyStepFrame footer={buttons}>
      <div className="-mx-1 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-1">{fields}</div>
    </KeyStepFrame>
  ) : (
    <div className="flex flex-col gap-4">
      {fields}
      <div className="flex items-center justify-end gap-2">{buttons}</div>
    </div>
  );
}
