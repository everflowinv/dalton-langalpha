import { useId, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronRight, X } from 'lucide-react'; // DALTON-HOOK(dalton-notify): allow clearing existing delivery chips when Dalton notify manages task
import {
  Select,
  SelectItem,
  SelectListBox,
  SelectPopover,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/aria-select';
import { Disclosure } from '@/components/ui/Disclosure';
import { Input } from '@/components/ui/input';
import { SegmentedControl } from '@/components/ui/segmented-control';
// DALTON-HOOK(automation-model): reuse Settings ModelSelector & useAllModels
// DALTON-HOOK(antigravity-series): format saved Antigravity variants as "Series · Effort"
// DALTON-HOOK(automation-effort): reasoning effort selector for non-Antigravity automation models
import { ModelSelector } from '@/components/model/ModelSelector';
import { Select as NativeSelect } from '@/components/ui/select';
import { getModelDisplayName } from '@/components/ui/chat-input.helpers';
import { useAllModels } from '@/hooks/useAllModels';
import { useApiKeys } from '@/hooks/useApiKeys';
// DALTON-HOOK(report-delivery): gate report_profile form section on channel_gateway capability
import { useCapabilities } from '@/hooks/useCapabilities';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import ReportProfileFields from '@/components/dalton/ReportProfileFields';
// DALTON-HOOK(dalton-notify): detect built-in dalton-* automations and routed custom tasks managed by Dalton notify settings
import { isDaltonNotifyManagedTask } from '@/lib/dalton/automationNotify';
import { formatAntigravityVariantLabel, inspectSavedAntigravityModel } from '@/lib/antigravitySeries';
import { effortLabelFor, effortOptions, resolveEffort } from '@/lib/modelTuning';
import { cn } from '@/lib/utils';
import type { Automation, DeliveryAttempt } from '@/types/automation';
import { useAutomationPlace } from '../hooks/useAutomationPlace';
import { useDeliveryOptions } from '../hooks/useDeliveryOptions';
import { useWorkspaceOptions, workspaceNameOf } from '../hooks/useWorkspaceOptions';
import { type DeliveryProblem, deliveryEntryName, deliveryMethodName, deliveryNames } from '../utils/delivery';
import { ALL_WORKSPACES, type FormPatch, type FormState } from '../utils/form';
import { MIN_COOLDOWN_MINUTES, RETRIGGER_MODES } from '../utils/price';
import CountInput from './CountInput';
import DeliveryPicker, { DeliveryProblems } from './DeliveryPicker';
import FormRow from './FormRow';

type DeliveryChoice = 'none' | 'slack' | 'discord';

/** The one segment the delivery control can press, or null for a set it
 *  cannot show: several channels, or one the agent chose that the form does
 *  not offer. Either survives an edit untouched until a segment is pressed. */
function deliveryChoice(methods: string[]): DeliveryChoice | null {
  if (methods.length === 0) return 'none';
  if (methods.length > 1) return null;
  return methods[0] === 'slack' || methods[0] === 'discord' ? methods[0] : null;
}

interface MoreOptionsProps {
  form: FormState;
  patch: FormPatch;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The edited automation's last deliveries, which name chats the apps may
   *  no longer list. */
  deliveryAttempts?: DeliveryAttempt[] | null;
  /** The delivery entries the last save was refused over. */
  deliveryProblems?: DeliveryProblem[];
  // DALTON-HOOK(automation-model): server refusal message when saving an unavailable model
  modelError?: string | null;
  // DALTON-HOOK(automation-effort): track explicit "Follow default" selection on create
  onExplicitDefaultEffort?: (explicit: boolean) => void;
  /** Company tasks cannot store a personal delivery choice. */
  omitDelivery?: boolean;
  // DALTON-HOOK(dalton-notify): edited automation metadata for built-in dalton-* notify detection
  automation?: Automation | null;
}

const NO_PROBLEMS: DeliveryProblem[] = [];

/** The settings most automations keep at their defaults, folded under one
 *  line that says what they are set to. */
export default function MoreOptions({
  form,
  patch,
  open,
  onOpenChange,
  deliveryAttempts,
  deliveryProblems = NO_PROBLEMS,
  modelError = null,
  onExplicitDefaultEffort,
  omitDelivery = false,
  automation = null,
}: MoreOptionsProps) {
  const { t } = useTranslation();
  // DALTON-HOOK(report-delivery): gate report_profile configuration on channel_gateway capability
  const { hasCapability } = useCapabilities();
  // DALTON-HOOK(dalton-notify): built-in dalton-* tasks and routed custom tasks manage delivery via Dalton notify settings
  const daltonNotifyManaged =
    hasCapability('channel_gateway') && isDaltonNotifyManagedTask(form.name, automation);
  const uid = useId();
  const ids = {
    thread: `${uid}-thread`,
    delivery: `${uid}-delivery`,
    retrigger: `${uid}-retrigger`,
    cooldown: `${uid}-cooldown`,
    failures: `${uid}-failures`,
    description: `${uid}-description`,
    panel: `${uid}-panel`,
  };

  // The list leaves Home out; the Chief of Staff is offered in its place.
  const workspaces = useWorkspaceOptions();
  const allWorkspaces = form.place === ALL_WORKSPACES;
  const workspaceName = workspaceNameOf(workspaces, form.place);
  const { homeId, label: placeLabel } = useAutomationPlace();
  // Chats are asked with the workspace the run lands in, which for All
  // workspaces is Home, once its id is known.
  const delivery = useDeliveryOptions({ workspaceId: allWorkspaces ? homeId : form.place });
  // With no messaging service, or before its chats load, the form offers the
  // apps alone, as it always has.
  const picker = delivery.options?.enabled ? delivery.options : null;
  // Until the chats are known, entries the old control cannot show stay as
  // they are: it would replace them all with the one segment pressed.
  const unread = !delivery.options && (delivery.isPending || !!delivery.error);
  const hold = unread && form.delivery_methods.length > 0 && deliveryChoice(form.delivery_methods) === null;
  const names = useMemo(() => deliveryNames(picker, deliveryAttempts ?? []), [picker, deliveryAttempts]);
  const entryName = (entry: string) => deliveryEntryName(entry, t, picker, names);
  // DALTON-HOOK(automation-model): available user models (excluding keyless custom models) and display formatting
  // DALTON-HOOK(antigravity-series): read seriesCatalog & customModels for Antigravity series display and selector
  const {
    models,
    metadata,
    payers,
    keylessModels,
    isLoading: modelsLoading,
    rawApiResponse,
    seriesCatalog,
    customModels,
  } = useAllModels();
  const rawMetadata = rawApiResponse?.model_metadata ?? metadata;
  // Until the catalog (and the keys deciding which custom models are keyless) has
  // arrived, an empty list is not "no models": showing the saved model as
  // unavailable then would invite clearing it, so the selector waits.
  // The keys decide which custom models are keyless: a failed keys request reads
  // as "no keys", which would mark a working custom model unavailable.
  const { apiKeys } = useApiKeys();
  // DALTON-HOOK(automation-model): so does a subscription status that could not be
  // read (a custom model riding it).
  const { statusError } = useConfiguredProviders();
  const catalogReady = !modelsLoading && rawApiResponse != null && apiKeys != null && !statusError;
  const availableModels = useMemo(() => {
    if (!keylessModels || keylessModels.size === 0) return models;
    const filtered: typeof models = {};
    for (const [provider, data] of Object.entries(models)) {
      const kept = (data.models ?? []).filter((m) => !keylessModels.has(m));
      if (kept.length > 0) {
        filtered[provider] = { ...data, models: kept };
      }
    }
    return filtered;
  }, [models, keylessModels]);
  // DALTON-HOOK(antigravity-series): format saved Antigravity models as "Series · Effort"
  // DALTON-HOOK(automation-effort): resolve effort options for non-Antigravity models and account default
  const seriesOpts = useMemo(
    () => ({ seriesCatalog, rawMetadata, customModels }),
    [seriesCatalog, rawMetadata, customModels],
  );
  const selectedAntigravity = useMemo(
    () => (form.llm_model ? inspectSavedAntigravityModel(form.llm_model, seriesOpts) : null),
    [form.llm_model, seriesOpts],
  );
  const nonAntigravityEfforts = useMemo(() => {
    if (selectedAntigravity) return [];
    if (form.llm_model) {
      const meta = metadata[form.llm_model] ?? rawMetadata[form.llm_model];
      return effortOptions(meta?.reasoning_efforts ?? [], form.reasoning_effort ?? '');
    }
    const union = new Set<string>();
    for (const [key, entry] of Object.entries(metadata)) {
      if (inspectSavedAntigravityModel(key, seriesOpts)) continue;
      for (const lv of entry?.reasoning_efforts ?? []) union.add(lv);
    }
    const offered = union.size > 0 ? [...union] : ['low', 'medium', 'high'];
    return effortOptions(offered, form.reasoning_effort ?? '');
  }, [selectedAntigravity, form.llm_model, form.reasoning_effort, metadata, rawMetadata, seriesOpts]);
  const showEffortSelector = catalogReady && !selectedAntigravity && nonAntigravityEfforts.length > 0;

  // DALTON-HOOK(automation-effort): keep reasoning_effort in sync when switching models
  const handleModelChange = (nextRaw: string) => {
    const nextModel = nextRaw || null;
    patch('llm_model', nextModel);
    if (!nextModel) return;
    if (inspectSavedAntigravityModel(nextModel, seriesOpts)) {
      onExplicitDefaultEffort?.(false);
      if (form.reasoning_effort !== null) patch('reasoning_effort', null);
      return;
    }
    const nextMeta = metadata[nextModel] ?? rawMetadata[nextModel];
    const offered = nextMeta?.reasoning_efforts ?? [];
    if (offered.length === 0) {
      onExplicitDefaultEffort?.(false);
      if (form.reasoning_effort !== null) patch('reasoning_effort', null);
      return;
    }
    if (form.reasoning_effort && !offered.includes(form.reasoning_effort)) {
      patch('reasoning_effort', resolveEffort(form.reasoning_effort, offered));
    }
  };

  const formatModel = (m: string) =>
    formatAntigravityVariantLabel(m, {
      seriesCatalog,
      rawMetadata,
      customModels,
      t,
      includeEffort: true,
    }) ?? getModelDisplayName(m, metadata);

  // DALTON-HOOK(automation-effort): include configured reasoning_effort in folded MoreOptions summary
  const effortSummaryLabel =
    !selectedAntigravity && form.reasoning_effort
      ? (effortLabelFor(t, form.reasoning_effort) ?? form.reasoning_effort)
      : null;

  // What the folded options are set to, so they can stay folded.
  const summary = [
    allWorkspaces ? t('agents.chiefOfStaff') : placeLabel({ workspace_id: form.place }, workspaceName),
    ...(form.llm_model
      ? [effortSummaryLabel ? `${formatModel(form.llm_model)} · ${effortSummaryLabel}` : formatModel(form.llm_model)]
      : effortSummaryLabel
        ? [`${t('automation:defaultModel')} · ${effortSummaryLabel}`]
        : []),
    t(form.thread_strategy === 'continue' ? 'automation:continueExisting' : 'automation:newThreadEachRun'),
    ...(omitDelivery
      ? []
      : [
          form.delivery_methods.length
            ? t('automation:deliversTo', { method: form.delivery_methods.map(entryName).join(', ') })
            : t('automation:noDelivery'),
        ]),
    t('automation:stopsAfter', { count: form.max_failures }),
  ].join(' · ');

  return (
    <div className="automation-form-more">
      <button
        type="button"
        className="automation-form-row automation-form-more-toggle"
        aria-expanded={open}
        aria-controls={ids.panel}
        onClick={() => onOpenChange(!open)}
      >
        <span className="automation-form-label inline-flex items-center gap-1">
          {t('automation:moreOptions')}
          <ChevronRight className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-90')} />
        </span>
        {!open && <span className="automation-form-summary">{summary}</span>}
      </button>

      <Disclosure open={open} id={ids.panel} className="automation-form-rows automation-form-rows-nested rule-t rule-b">
        <FormRow label={t('thread.workspace')}>
          <Select
            aria-label={t('thread.workspace')}
            selectedKey={form.place || null}
            onSelectionChange={(key) => patch('place', key == null ? '' : String(key))}
            placeholder={t('automation:selectWorkspace')}
            className="max-w-sm"
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectPopover>
              <SelectListBox>
                <SelectItem id={ALL_WORKSPACES}>{t('agents.chiefOfStaff')}</SelectItem>
                {workspaces.map((ws) => (
                  <SelectItem key={ws.workspace_id} id={ws.workspace_id}>
                    {ws.name}
                  </SelectItem>
                ))}
              </SelectListBox>
            </SelectPopover>
          </Select>
        </FormRow>

        {/* DALTON-HOOK(automation-model): per-automation model selector in MoreOptions */}
        <FormRow label={t('automation:detailModel')}>
          <div className="max-w-sm">
            {!catalogReady ? (
              <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }} role="status">
                {modelsLoading ? t('automation:modelCatalogLoading') : t('automation:modelCatalogUnavailable')}
              </p>
            ) : (
            // DALTON-HOOK(antigravity-series): pass seriesCatalog, rawMetadata & customModels into automation ModelSelector
            <ModelSelector
              ariaLabel={t('automation:detailModel')}
              nameOf={formatModel}
              // A saved model missing from the list is called unavailable only when the
              // keys say so; any other absence may be a request that failed, so it is
              // shown as the current setting rather than inviting a reset.
              missingLabel={(m) =>
                keylessModels?.has(m)
                  ? t('automation:modelUnavailableOption', { model: formatModel(m) })
                  : t('automation:modelCurrentOption', { model: formatModel(m) })
              }
              value={form.llm_model ?? ''}
              onChange={handleModelChange}
              models={availableModels}
              placeholder={t('automation:defaultModelOption')}
              payers={payers}
              metadata={metadata}
              seriesCatalog={seriesCatalog}
              rawMetadata={rawMetadata}
              customModels={customModels}
              allowEmpty
            />
            )}
            {modelError && (
              <p className="automation-delivery-error mt-2" role="alert">
                {modelError}
              </p>
            )}
          </div>
        </FormRow>

        {/* DALTON-HOOK(automation-effort): reasoning effort dropdown for non-Antigravity models and account default */}
        {showEffortSelector && (
          <FormRow label={t('chat.modelSelector.reasoningEffort')}>
            <div className="max-w-sm">
              <NativeSelect
                aria-label={t('chat.modelSelector.reasoningEffort')}
                value={form.reasoning_effort ?? ''}
                onChange={(e) => {
                  const next = e.target.value || null;
                  onExplicitDefaultEffort?.(next === null);
                  patch('reasoning_effort', next);
                }}
              >
                <option value="">{t('automation:reasoningEffortDefault')}</option>
                {nonAntigravityEfforts.map((lv) => (
                  <option key={lv} value={lv}>
                    {effortLabelFor(t, lv) ?? lv}
                  </option>
                ))}
              </NativeSelect>
            </div>
          </FormRow>
        )}

        <FormRow label={t('automation:threadStrategy')} labelId={ids.thread}>
          <SegmentedControl
            labelledBy={ids.thread}
            value={form.thread_strategy}
            onChange={(strategy) => patch('thread_strategy', strategy)}
            options={[
              { value: 'new', label: t('automation:newThreadEachRun') },
              { value: 'continue', label: t('automation:continueExisting') },
            ]}
          />
        </FormRow>

        {/* DALTON-HOOK(company-automations): company edits do not offer personal delivery. */}
        {/* DALTON-HOOK(dalton-notify): built-in dalton-* tasks disable DeliveryPicker and point to Dalton notify settings. */}
        {!omitDelivery && <FormRow label={t('automation:delivery')} labelId={ids.delivery}>
          {picker ? (
            <DeliveryPicker
              methods={form.delivery_methods}
              onChange={(methods) => patch('delivery_methods', methods)}
              options={picker}
              stale={delivery.isPlaceholderData}
              workspaceId={delivery.workspaceId}
              names={names}
              problems={deliveryProblems}
              labelledBy={ids.delivery}
              disabled={daltonNotifyManaged}
              disabledReason={daltonNotifyManaged ? t('automation:deliveryManagedByDaltonNotify') : null}
            />
          ) : daltonNotifyManaged ? (
            <>
              <p className="automation-form-readout mt-0">{t('automation:deliveryManagedByDaltonNotify')}</p>
              {form.delivery_methods.length > 0 && (
                <ul className="automation-delivery-chips">
                  {form.delivery_methods.map((entry) => (
                    <li key={entry} className="automation-delivery-chip">
                      <span className="automation-delivery-chip-label">
                        <span className="truncate">{entryName(entry)}</span>
                      </span>
                      <button
                        type="button"
                        className="automation-delivery-chip-remove"
                        aria-label={t('automation:deliveryRemove', { name: entryName(entry) })}
                        onClick={() => patch('delivery_methods', form.delivery_methods.filter((m) => m !== entry))}
                      >
                        <X className="size-3" aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : hold ? (
            <>
              <ul className="automation-delivery-chips">
                {form.delivery_methods.map((entry) => (
                  <li key={entry} className="automation-delivery-chip">
                    <span className="automation-delivery-chip-label">
                      <span className="truncate">{entryName(entry)}</span>
                    </span>
                  </li>
                ))}
              </ul>
              {!!delivery.error && <p className="automation-form-readout">{t('automation:deliveryOptionsUnavailable')}</p>}
              <DeliveryProblems problems={deliveryProblems} nameOf={entryName} />
            </>
          ) : (
            <>
              <SegmentedControl<DeliveryChoice>
                labelledBy={ids.delivery}
                value={deliveryChoice(form.delivery_methods)}
                onChange={(choice) => patch('delivery_methods', choice === 'none' ? [] : [choice])}
                options={[
                  { value: 'none', label: t('automation:deliverNone') },
                  { value: 'slack', label: deliveryMethodName('slack', t) },
                  { value: 'discord', label: deliveryMethodName('discord', t) },
                ]}
              />
              {!!delivery.error && <p className="automation-form-readout">{t('automation:deliveryOptionsUnavailable')}</p>}
              <DeliveryProblems problems={deliveryProblems} nameOf={entryName} />
            </>
          )}
        {/* DALTON-HOOK(company-automations): company edits stop before this delivery choice. */}
        </FormRow>}

        {/* DALTON-HOOK(report-delivery): optional report_profile delivery targets gated on channel_gateway capability */}
        {!omitDelivery && hasCapability('channel_gateway') && (
          <FormRow label={t('automation:reportDeliverySection', '报告外发')}>
            <ReportProfileFields
              value={form.report_profile}
              onChange={(next) => patch('report_profile', next)}
            />
          </FormRow>
        )}

        {form.trigger_type === 'price' && (
          <FormRow label={t('automation:priceRetrigger')} labelId={ids.retrigger}>
            <SegmentedControl
              labelledBy={ids.retrigger}
              value={form.price_retrigger_mode}
              onChange={(mode) => patch('price_retrigger_mode', mode)}
              options={RETRIGGER_MODES.map((opt) => ({ value: opt.value, label: t(opt.labelKey) }))}
            />
          </FormRow>
        )}

        {form.trigger_type === 'price' && form.price_retrigger_mode === 'recurring' && (
          <FormRow label={t('automation:priceCooldown')} htmlFor={ids.cooldown}>
            <div className="flex items-center gap-2">
              <Input
                id={ids.cooldown}
                type="number"
                min={MIN_COOLDOWN_MINUTES}
                value={form.price_cooldown_minutes}
                onChange={(e) => patch('price_cooldown_minutes', e.target.value)}
                className="w-28 font-mono tabular-nums"
              />
              <span className="automation-form-joiner">{t('automation:minutes')}</span>
            </div>
            <p className="automation-form-readout">{t('automation:cooldownHint')}</p>
          </FormRow>
        )}

        <FormRow label={t('automation:maxFailures')} htmlFor={ids.failures}>
          <CountInput
            id={ids.failures}
            value={form.max_failures}
            min={1}
            max={100}
            onChange={(n) => patch('max_failures', n)}
            className="w-20 font-mono tabular-nums"
          />
        </FormRow>

        <FormRow label={t('common.description')} htmlFor={ids.description}>
          <Input
            id={ids.description}
            value={form.description}
            onChange={(e) => patch('description', e.target.value)}
            placeholder={t('automation:descPlaceholder')}
          />
        </FormRow>
      </Disclosure>
    </div>
  );
}
