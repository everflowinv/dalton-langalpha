import { useEffect, useId, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, FlaskConical, RotateCcw, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ToggleSwitch } from '@/components/ui/switch';
import { DaltonInlineNotice, DaltonStatusBadge } from '@/components/dalton/primitives';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useDialogA11y } from '@/hooks/useDialogA11y';
import { useIsMobile } from '@/hooks/useIsMobile';
import { cn } from '@/lib/utils';
import type {
  TeamCatalogBillingMode,
  TeamCatalogModelItem,
  TeamCatalogModelUpsertPayload,
  TeamCatalogPriceSourceKind,
  TeamCatalogProviderVariant,
  TeamCatalogReasoningConfig,
  TeamCatalogTestCallResponse,
} from '@/pages/Settings/utils/daltonApi';
import {
  parseCatalogApiError,
  useCreateTeamCatalogModel,
  useRestoreTeamCatalogModelDefault,
  useTestCallTeamCatalogModel,
  useUpdateTeamCatalogModel,
  useUpsertTeamCatalogProviderVariant,
} from './useTeamModelCatalog';

export type TeamModelEditMode = 'create' | 'edit' | 'clone' | 'create_variant' | 'edit_variant';
type ToolCallingTriState = 'unknown' | 'true' | 'false';

export interface ProviderOptionItem {
  provider: string;
  displayName: string;
  isVariant: boolean;
  sdk?: string;
  parentProvider?: string;
  accessType?: 'api_key' | 'coding_plan';
  billing?: TeamCatalogBillingMode;
  baseUrl?: string;
  credentialConfigured?: boolean;
  keyFingerprint?: string | null;
}

export interface TeamModelEditSheetProps {
  open: boolean;
  mode: TeamModelEditMode;
  onClose: () => void;
  revision: number;
  latestRevision?: number;
  model: TeamCatalogModelItem | null;
  latestModel?: TeamCatalogModelItem | null;
  variant: TeamCatalogProviderVariant | null;
  latestVariant?: TeamCatalogProviderVariant | null;
  allModels: readonly TeamCatalogModelItem[];
  providerOptions: readonly ProviderOptionItem[];
  parentProviderGroups: readonly string[];
  supportedVariantSdks: readonly string[];
  curatedSetup: readonly string[] | null;
  effectiveCuratedCandidates: readonly string[];
  onRefreshCatalog?: () => void;
  onSaved?: (message: string) => void;
}

const MODALITIES = ['text', 'image', 'pdf', 'audio', 'video'] as const;
const REASONING_EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;
const COMPACTION_PROFILES = [
  '',
  'aggressive',
  'moderate',
  'extended',
  'economy',
  'relaxed',
  'spacious',
  'expansive',
  'maximal',
] as const;
const RATING_VALUES = ['', '1', '2', '3', '4', '5'] as const;

type GroupId = 'identity' | 'pricing' | 'window' | 'reasoning' | 'ratings';

function isOAuthKey(value: string): boolean {
  return value.trim().toLowerCase().endsWith('-oauth');
}

function parseOptionalNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

function parseOptionalInt(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number.parseInt(trimmed, 10);
  return Number.isSafeInteger(n) ? n : null;
}

/**
 * 5-group Team Model Catalog drawer & Provider Variant drawer.
 * - Desktop (`>= 768px`): right-side drawer with all 5 groups open.
 * - Mobile (`< 768px`): full-screen bottom drawer with accordion groups, `>= 16px` inputs,
 *   and `env(safe-area-inset-bottom)` sticky action bar.
 */
export function TeamModelEditSheet({
  open,
  mode,
  onClose,
  revision,
  latestRevision,
  model,
  latestModel,
  variant,
  latestVariant,
  allModels,
  providerOptions,
  parentProviderGroups,
  supportedVariantSdks,
  curatedSetup,
  effectiveCuratedCandidates,
  onRefreshCatalog,
  onSaved,
}: TeamModelEditSheetProps) {
  const { t } = useTranslation();
  const labelId = useId();
  const isMobile = useIsMobile();
  const { hasCapability, profile } = useCapabilities();
  const oauthSyncEnabled = hasCapability('native_oauth_sync');
  const isCoreProfile = profile === 'core';

  const createModelMutation = useCreateTeamCatalogModel();
  const updateModelMutation = useUpdateTeamCatalogModel();
  const restoreDefaultMutation = useRestoreTeamCatalogModelDefault();
  const upsertVariantMutation = useUpsertTeamCatalogProviderVariant();
  const testCallMutation = useTestCallTeamCatalogModel();

  const isVariantOnly = mode === 'create_variant' || mode === 'edit_variant';
  const busy =
    createModelMutation.isPending ||
    updateModelMutation.isPending ||
    restoreDefaultMutation.isPending ||
    upsertVariantMutation.isPending;

  const dialogRef = useDialogA11y<HTMLDivElement>(busy ? () => {} : onClose);

  // Accordion open state (on mobile only group 1 starts open; desktop keeps all 5 open)
  const [openGroups, setOpenGroups] = useState<Record<GroupId, boolean>>({
    identity: true,
    pricing: !isMobile,
    window: !isMobile,
    reasoning: !isMobile,
    ratings: !isMobile,
  });

  // Group 1: Identity & Provider
  const [modelKey, setModelKey] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [provider, setProvider] = useState('');
  const [modelId, setModelId] = useState('');
  const [visible, setVisible] = useState(true);
  const [promptGuidance, setPromptGuidance] = useState('');
  const [copiedFromSibling, setCopiedFromSibling] = useState<string | null>(null);

  // Variant fields (used in variant-only mode OR inline when editing a catalog variant model)
  const [variantSlug, setVariantSlug] = useState('');
  const [variantDisplayName, setVariantDisplayName] = useState('');
  const [variantParent, setVariantParent] = useState('openai');
  const [variantSdk, setVariantSdk] = useState('openai');
  const [variantAccessType, setVariantAccessType] = useState<'api_key' | 'coding_plan'>('api_key');
  const [variantBilling, setVariantBilling] = useState<TeamCatalogBillingMode>('cash');
  const [variantBaseUrl, setVariantBaseUrl] = useState('');
  const [variantApiKey, setVariantApiKey] = useState('');

  // Group 2: Pricing, Source & Task Cost
  const [billingMode, setBillingMode] = useState<TeamCatalogBillingMode>('cash');
  const [cashInput, setCashInput] = useState('');
  const [cashCachedInput, setCashCachedInput] = useState('');
  const [cashCache5m, setCashCache5m] = useState('');
  const [cashCache1h, setCashCache1h] = useState('');
  const [cashOutput, setCashOutput] = useState('');
  const [notionalInput, setNotionalInput] = useState('');
  const [notionalCachedInput, setNotionalCachedInput] = useState('');
  const [notionalOutput, setNotionalOutput] = useState('');
  const [notionalRef, setNotionalRef] = useState('');
  const [priceSourceKind, setPriceSourceKind] = useState<TeamCatalogPriceSourceKind>('official');
  const [priceEstimated, setPriceEstimated] = useState(false);
  const [priceSourceUrl, setPriceSourceUrl] = useState('');
  const [priceVerifiedAt, setPriceVerifiedAt] = useState('');
  const [priceNote, setPriceNote] = useState('');
  const [taskCostRefModel, setTaskCostRefModel] = useState('');
  const [taskCostTimes, setTaskCostTimes] = useState('');
  const [pricingTouched, setPricingTouched] = useState(false);

  // Group 3: Context Window & Modalities
  const [contextWindow, setContextWindow] = useState('200000');
  const [maxTokens, setMaxTokens] = useState('');
  const [compactionProfile, setCompactionProfile] = useState('');
  const [modalities, setModalities] = useState<string[]>(['text']);

  // Group 4: Reasoning Effort & Protocol
  const [reasoningEnabled, setReasoningEnabled] = useState(false);
  const [reasoningEfforts, setReasoningEfforts] = useState<string[]>(['low', 'medium', 'high']);
  const [reasoningDefault, setReasoningDefault] = useState('medium');
  const [reasoningWrite, setReasoningWrite] = useState('');

  // Group 5: Ratings, Series & Curated Setup
  const [intelligence, setIntelligence] = useState('');
  const [speed, setSpeed] = useState('');
  const [plainLanguage, setPlainLanguage] = useState('');
  const [family, setFamily] = useState('');
  const [toolCalling, setToolCalling] = useState<ToolCallingTriState>('unknown');
  const [series, setSeries] = useState('');
  const [inCuratedSetup, setInCuratedSetup] = useState(false);
  const [initialInCuratedSetup, setInitialInCuratedSetup] = useState(false);

  // Opened snapshot baseline (for expected_revision & explicit null-clear tracking)
  const [baseRevision, setBaseRevision] = useState(revision);
  const [initialToolCalling, setInitialToolCalling] = useState<ToolCallingTriState>('unknown');
  const [initialPromptGuidance, setInitialPromptGuidance] = useState('');
  const [initialCompactionProfile, setInitialCompactionProfile] = useState('');
  const [initialMaxTokens, setInitialMaxTokens] = useState('');
  const [initialHasMaxOutputTokensKey, setInitialHasMaxOutputTokensKey] = useState(false);
  const [initialHasReasoning, setInitialHasReasoning] = useState(false);
  const [initialReasoningWrite, setInitialReasoningWrite] = useState('');
  const [initialHasTaskCostBound, setInitialHasTaskCostBound] = useState(false);
  const [initialHasNotionalPricing, setInitialHasNotionalPricing] = useState(false);
  const [initialNotionalRef, setInitialNotionalRef] = useState('');
  const [initialIntelligence, setInitialIntelligence] = useState('');
  const [initialSpeed, setInitialSpeed] = useState('');
  const [initialPlainLanguage, setInitialPlainLanguage] = useState('');
  const [initialFamily, setInitialFamily] = useState('');
  const [initialSeries, setInitialSeries] = useState('');
  const [initialBillingMode, setInitialBillingMode] = useState<TeamCatalogBillingMode>('cash');

  // Comment, test-call result, conflict state & confirmations
  const [comment, setComment] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<TeamCatalogTestCallResponse | null>(null);
  const [confirmRestoreDefault, setConfirmRestoreDefault] = useState(false);
  const [confirmLastCurated, setConfirmLastCurated] = useState(false);
  const [revisionConflict, setRevisionConflict] = useState<{ currentRevision: number } | null>(null);

  const visibleProviderOptions = useMemo(
    () =>
      providerOptions.filter((p) => oauthSyncEnabled || !isOAuthKey(p.provider)),
    [providerOptions, oauthSyncEnabled],
  );

  const visibleParentGroups = useMemo(() => {
    const filtered = parentProviderGroups.filter((g) => oauthSyncEnabled || !isOAuthKey(g));
    if (variant?.parent_provider && !filtered.includes(variant.parent_provider)) {
      return [variant.parent_provider, ...filtered];
    }
    return filtered;
  }, [parentProviderGroups, oauthSyncEnabled, variant?.parent_provider]);

  const variantSdkOptions = useMemo(() => {
    if (variant?.sdk && !supportedVariantSdks.includes(variant.sdk)) {
      return [variant.sdk, ...supportedVariantSdks];
    }
    return [...supportedVariantSdks];
  }, [supportedVariantSdks, variant?.sdk]);

  const selectedProviderMeta = useMemo(
    () => visibleProviderOptions.find((p) => p.provider === provider) ?? null,
    [visibleProviderOptions, provider],
  );

  const populateFromSnapshot = (
    snapModel: TeamCatalogModelItem | null,
    snapVariant: TeamCatalogProviderVariant | null,
    snapRevision: number,
  ) => {
    setErrorMsg(null);
    setTestResult(null);
    setCopiedFromSibling(null);
    setConfirmRestoreDefault(false);
    setConfirmLastCurated(false);
    setRevisionConflict(null);
    setComment('');
    setVariantApiKey('');
    setBaseRevision(snapRevision);
    setPricingTouched(mode === 'create' || mode === 'clone');
    setOpenGroups({
      identity: true,
      pricing: !isMobile,
      window: !isMobile,
      reasoning: !isMobile,
      ratings: !isMobile,
    });

    if (mode === 'create_variant') {
      setVariantSlug('');
      setVariantDisplayName('');
      setVariantParent(visibleParentGroups.includes('openai') ? 'openai' : (visibleParentGroups[0] ?? ''));
      setVariantSdk(variantSdkOptions.includes('openai') ? 'openai' : (variantSdkOptions[0] ?? ''));
      setVariantAccessType('api_key');
      setVariantBilling('cash');
      setVariantBaseUrl('https://');
      return;
    }

    if (mode === 'edit_variant' && snapVariant) {
      setVariantSlug(snapVariant.provider_slug);
      setVariantDisplayName(snapVariant.display_name);
      setVariantParent(snapVariant.parent_provider);
      setVariantSdk(snapVariant.sdk);
      setVariantAccessType(snapVariant.access_type);
      setVariantBilling(snapVariant.billing);
      setVariantBaseUrl(snapVariant.base_url);
      return;
    }

    if ((mode === 'edit' || mode === 'clone') && snapModel) {
      const clonedKey = mode === 'clone' ? `${snapModel.model_key}-copy` : snapModel.model_key;
      setModelKey(clonedKey);
      setDisplayName(mode === 'clone' ? `${snapModel.display_name} (Copy)` : snapModel.display_name);
      setProvider(snapModel.provider);
      setModelId(snapModel.model_id);
      setVisible(snapModel.visible);
      const initPrompt = snapModel.prompt_guidance ?? '';
      setPromptGuidance(initPrompt);
      setInitialPromptGuidance(initPrompt);

      const provMeta = visibleProviderOptions.find((p) => p.provider === snapModel.provider);
      setVariantBaseUrl(provMeta?.isVariant ? (provMeta.baseUrl ?? '') : '');

      const effBilling: TeamCatalogBillingMode =
        snapModel.dalton_meta?.billing === 'subscription' || provMeta?.billing === 'subscription'
          ? 'subscription'
          : 'cash';
      setBillingMode(effBilling);
      setInitialBillingMode(effBilling);

      setCashInput(snapModel.pricing?.input != null ? String(snapModel.pricing.input) : '');
      setCashCachedInput(
        snapModel.pricing?.cached_input != null
          ? String(snapModel.pricing.cached_input)
          : snapModel.pricing?.cache_read != null
            ? String(snapModel.pricing.cache_read)
            : '',
      );
      setCashCache5m(snapModel.pricing?.cache_5m != null ? String(snapModel.pricing.cache_5m) : '');
      setCashCache1h(snapModel.pricing?.cache_1h != null ? String(snapModel.pricing.cache_1h) : '');
      setCashOutput(snapModel.pricing?.output != null ? String(snapModel.pricing.output) : '');

      const np = snapModel.dalton_meta?.notional_pricing;
      setNotionalInput(np?.input != null ? String(np.input) : '');
      setNotionalCachedInput(np?.cached_input != null ? String(np.cached_input) : '');
      setNotionalOutput(np?.output != null ? String(np.output) : '');
      const initNotionalRef = snapModel.dalton_meta?.notional_ref ?? '';
      setNotionalRef(initNotionalRef);
      setInitialHasNotionalPricing(Boolean(np && (np.input != null || np.output != null || np.cached_input != null)));
      setInitialNotionalRef(initNotionalRef);

      const pm = effBilling === 'subscription' ? np : snapModel.pricing_meta;
      setPriceSourceKind((pm?.source_kind as TeamCatalogPriceSourceKind) || 'official');
      setPriceEstimated(Boolean(pm?.estimated));
      setPriceSourceUrl(pm?.source_url ?? '');
      setPriceVerifiedAt(pm?.verified_at ?? '');
      setPriceNote(pm?.note ?? pm?.source ?? '');

      setTaskCostRefModel(snapModel.task_costs_bound?.model ?? '');
      setTaskCostTimes(
        snapModel.task_costs_bound?.times != null ? String(snapModel.task_costs_bound.times) : '',
      );
      setInitialHasTaskCostBound(
        Boolean(snapModel.task_costs_bound?.model && snapModel.task_costs_bound?.times != null),
      );

      setContextWindow(snapModel.context != null ? String(snapModel.context) : '');
      const paramMax =
        (snapModel.parameters?.max_tokens as number | undefined) ??
        (snapModel.parameters?.max_output_tokens as number | undefined);
      const initMaxStr = paramMax != null ? String(paramMax) : '';
      setMaxTokens(initMaxStr);
      setInitialMaxTokens(initMaxStr);
      setInitialHasMaxOutputTokensKey(snapModel.parameters?.max_output_tokens != null);
      const initCompaction = snapModel.compaction_profile ?? '';
      setCompactionProfile(initCompaction);
      setInitialCompactionProfile(initCompaction);
      setModalities(snapModel.input_modalities?.length ? [...snapModel.input_modalities] : ['text']);

      const r = snapModel.reasoning;
      const hasReasoning = Boolean(r && Array.isArray(r.efforts) && r.efforts.length > 0);
      const initWrite = typeof r?.write === 'string' ? r.write : '';
      setReasoningEnabled(hasReasoning);
      setInitialHasReasoning(hasReasoning);
      setReasoningEfforts(hasReasoning && r?.efforts ? [...r.efforts] : ['low', 'medium', 'high']);
      setReasoningDefault(r?.default ?? 'medium');
      setReasoningWrite(initWrite);
      setInitialReasoningWrite(initWrite);

      const initIntel = snapModel.intelligence != null ? String(snapModel.intelligence) : '';
      const initSpd = snapModel.speed != null ? String(snapModel.speed) : '';
      const initPlain = snapModel.plain_language != null ? String(snapModel.plain_language) : '';
      setIntelligence(initIntel);
      setInitialIntelligence(initIntel);
      setSpeed(initSpd);
      setInitialSpeed(initSpd);
      setPlainLanguage(initPlain);
      setInitialPlainLanguage(initPlain);
      const initFam = snapModel.dalton_meta?.family ?? '';
      setFamily(initFam);
      setInitialFamily(initFam);
      const initTc: ToolCallingTriState =
        snapModel.dalton_meta?.tool_calling === true
          ? 'true'
          : snapModel.dalton_meta?.tool_calling === false
            ? 'false'
            : 'unknown';
      setToolCalling(initTc);
      setInitialToolCalling(initTc);
      const initSer = snapModel.dalton_meta?.series ?? '';
      setSeries(initSer);
      setInitialSeries(initSer);

      const isCurated =
        mode === 'edit' &&
        (curatedSetup
          ? curatedSetup.includes(snapModel.model_key)
          : effectiveCuratedCandidates.includes(snapModel.model_key));
      setInCuratedSetup(isCurated);
      setInitialInCuratedSetup(isCurated);
      return;
    }

    // Create new model defaults
    const defaultProv = visibleProviderOptions[0]?.provider ?? 'openai';
    const defaultProvMeta = visibleProviderOptions[0];
    setModelKey('');
    setDisplayName('');
    setProvider(defaultProv);
    setVariantBaseUrl(defaultProvMeta?.isVariant ? (defaultProvMeta.baseUrl ?? '') : '');
    setModelId('');
    setVisible(true);
    setPromptGuidance('');
    setInitialPromptGuidance('');
    const initBilling = defaultProvMeta?.billing ?? 'cash';
    setBillingMode(initBilling);
    setInitialBillingMode(initBilling);
    setCashInput('');
    setCashCachedInput('');
    setCashCache5m('');
    setCashCache1h('');
    setCashOutput('');
    setNotionalInput('');
    setNotionalCachedInput('');
    setNotionalOutput('');
    setNotionalRef('');
    setInitialHasNotionalPricing(false);
    setInitialNotionalRef('');
    setPriceSourceKind('official');
    setPriceEstimated(false);
    setPriceSourceUrl('');
    setPriceVerifiedAt('');
    setPriceNote('');
    setTaskCostRefModel('');
    setTaskCostTimes('');
    setInitialHasTaskCostBound(false);
    setContextWindow('200000');
    setMaxTokens('');
    setInitialMaxTokens('');
    setInitialHasMaxOutputTokensKey(false);
    setCompactionProfile('');
    setInitialCompactionProfile('');
    setModalities(['text', 'image']);
    setReasoningEnabled(false);
    setInitialHasReasoning(false);
    setReasoningEfforts(['low', 'medium', 'high']);
    setReasoningDefault('medium');
    setReasoningWrite('');
    setInitialReasoningWrite('');
    setIntelligence('');
    setInitialIntelligence('');
    setSpeed('');
    setInitialSpeed('');
    setPlainLanguage('');
    setInitialPlainLanguage('');
    setFamily('');
    setInitialFamily('');
    setToolCalling('unknown');
    setInitialToolCalling('unknown');
    setSeries('');
    setInitialSeries('');
    setInCuratedSetup(false);
    setInitialInCuratedSetup(false);
  };

  // Initialize form whenever sheet opens or opened snapshot changes (never reset on background catalog refresh)
  useEffect(() => {
    if (!open) return;
    populateFromSnapshot(model, variant, revision);
  }, [open, mode, model, variant, revision, isMobile]);

  useEffect(() => {
    if (!open || mode !== 'create_variant') return;
    if (!variantParent && visibleParentGroups.length > 0) {
      setVariantParent(visibleParentGroups.includes('openai') ? 'openai' : visibleParentGroups[0]);
    }
    if (!variantSdk && variantSdkOptions.length > 0) {
      setVariantSdk(variantSdkOptions.includes('openai') ? 'openai' : variantSdkOptions[0]);
    }
  }, [open, mode, variantParent, variantSdk, visibleParentGroups, variantSdkOptions]);

  /** Copy window, modalities, reasoning protocol, billing & family defaults from a sibling model on the same provider. */
  const applySiblingDefaultsForProvider = (nextProvider: string) => {
    setProvider(nextProvider);
    // Always clear any previously entered write-only API key when switching provider or variant
    setVariantApiKey('');
    const provMeta = visibleProviderOptions.find((p) => p.provider === nextProvider);
    setVariantBaseUrl(provMeta?.isVariant ? (provMeta.baseUrl ?? '') : '');
    if (mode !== 'create') return;

    const sibling = allModels.find((m) => m.provider === nextProvider);
    if (!sibling) {
      setCopiedFromSibling(null);
      if (provMeta?.billing) setBillingMode(provMeta.billing);
      return;
    }

    if (sibling.context != null) setContextWindow(String(sibling.context));
    if (sibling.compaction_profile) setCompactionProfile(sibling.compaction_profile);
    if (sibling.input_modalities?.length) setModalities([...sibling.input_modalities]);
    if (sibling.reasoning?.efforts?.length) {
      setReasoningEnabled(true);
      setReasoningEfforts([...sibling.reasoning.efforts]);
      setReasoningDefault(sibling.reasoning.default ?? sibling.reasoning.efforts[0] ?? 'medium');
      setReasoningWrite(typeof sibling.reasoning.write === 'string' ? sibling.reasoning.write : '');
    } else {
      setReasoningEnabled(false);
    }
    const sibBilling: TeamCatalogBillingMode =
      sibling.dalton_meta?.billing === 'subscription' || provMeta?.billing === 'subscription'
        ? 'subscription'
        : 'cash';
    setBillingMode(sibBilling);
    if (sibling.dalton_meta?.family) setFamily(sibling.dalton_meta.family);
    if (sibling.dalton_meta?.series && oauthSyncEnabled) setSeries(sibling.dalton_meta.series);
    setCopiedFromSibling(sibling.display_name || sibling.model_key);
  };

  if (!open) return null;

  const toggleGroup = (id: GroupId) => {
    setOpenGroups((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const toggleModality = (mod: string) => {
    setModalities((prev) => {
      if (prev.includes(mod)) {
        return prev.length > 1 ? prev.filter((x) => x !== mod) : prev;
      }
      return [...prev, mod];
    });
  };

  const toggleEffort = (eff: string) => {
    setReasoningEfforts((prev) => {
      if (prev.includes(eff)) {
        const next = prev.filter((x) => x !== eff);
        if (next.length === 0) return prev;
        if (reasoningDefault === eff) setReasoningDefault(next[0]);
        return next;
      }
      return [...prev, eff];
    });
  };

  const handleTestCall = async () => {
    setErrorMsg(null);
    setTestResult(null);
    const targetProvider = isVariantOnly ? variantSlug.trim() : provider.trim();
    const targetModelId = modelId.trim() || modelKey.trim();
    if (!targetProvider || !targetModelId) {
      setErrorMsg(t('settings:dalton.teamCatalog.testCallMissingFields'));
      return;
    }
    try {
      const res = await testCallMutation.mutateAsync({
        model_key: modelKey.trim() || undefined,
        provider: targetProvider,
        model_id: targetModelId,
        base_url: selectedProviderMeta?.isVariant || isVariantOnly ? variantBaseUrl.trim() || undefined : undefined,
        api_key: selectedProviderMeta?.isVariant || isVariantOnly ? variantApiKey.trim() || undefined : undefined,
        sdk: isVariantOnly ? variantSdk : selectedProviderMeta?.sdk,
      });
      setTestResult(res);
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.testCallFailed'));
      setTestResult({ ok: false, error: parsed.code ?? 'error', message: parsed.message });
    }
  };

  const handleRestoreDefault = async () => {
    if (!model) return;
    setErrorMsg(null);
    try {
      await restoreDefaultMutation.mutateAsync({
        modelKey: model.model_key,
        expected_revision: baseRevision,
        comment: comment.trim() || undefined,
      });
      onSaved?.(
        t('settings:dalton.teamCatalog.restoreDefaultSuccess', {
          name: model.display_name || model.model_key,
        }),
      );
      onClose();
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.saveFailed'));
      if (parsed.code === 'revision_conflict') {
        onRefreshCatalog?.();
        setRevisionConflict({
          currentRevision: parsed.currentRevision ?? latestRevision ?? baseRevision,
        });
      }
      setErrorMsg(parsed.message);
    }
  };

  const handleSaveVariantOnly = async (overrideExpectedRevision?: number) => {
    setErrorMsg(null);
    const slug = variantSlug.trim();
    const baseUrl = variantBaseUrl.trim();
    if (!slug) {
      setErrorMsg(t('settings:dalton.teamCatalog.variantSlugRequired'));
      return;
    }
    if (!baseUrl.startsWith('https://')) {
      setErrorMsg(t('settings:dalton.teamCatalog.variantHttpsRequired'));
      return;
    }
    if (mode === 'create_variant' && !variantApiKey.trim()) {
      setErrorMsg(t('settings:dalton.teamCatalog.variantApiKeyRequired'));
      return;
    }
    const targetExpectedRev = overrideExpectedRevision ?? baseRevision;
    try {
      await upsertVariantMutation.mutateAsync({
        isUpdate: mode === 'edit_variant',
        payload: {
          provider_slug: slug,
          display_name: variantDisplayName.trim() || slug,
          sdk: variantSdk,
          parent_provider: variantParent,
          access_type: variantAccessType,
          billing: variantBilling,
          base_url: baseUrl,
          api_key: variantApiKey.trim() || undefined,
          expected_revision: targetExpectedRev,
          comment: comment.trim() || undefined,
        },
      });
      onSaved?.(
        t('settings:dalton.teamCatalog.variantSaveSuccess', {
          name: variantDisplayName.trim() || slug,
        }),
      );
      onClose();
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.saveFailed'));
      if (parsed.code === 'revision_conflict') {
        onRefreshCatalog?.();
        setRevisionConflict({
          currentRevision: parsed.currentRevision ?? latestRevision ?? targetExpectedRev,
        });
      }
      setErrorMsg(parsed.message);
    }
  };

  const handleSaveModel = async (options?: {
    overrideExpectedRevision?: number;
    confirmedLastCurated?: boolean;
  }) => {
    setErrorMsg(null);
    const cleanKey = modelKey.trim();
    const cleanModelId = modelId.trim();
    const cleanProvider = provider.trim();
    if (!cleanKey || !cleanModelId || !cleanProvider) {
      setErrorMsg(t('settings:dalton.teamCatalog.requiredIdentityMissing'));
      return;
    }
    const ctxVal = parseOptionalInt(contextWindow);
    const maxTokVal = parseOptionalInt(maxTokens);
    if (ctxVal !== null && maxTokVal !== null && maxTokVal > ctxVal) {
      setErrorMsg(t('settings:dalton.teamCatalog.maxTokensExceedsContext'));
      return;
    }

    // Everflow last visible curated model risk confirmation (covers hiding OR un-curating from the EditSheet)
    const removesLastVisibleCuratedEverflow =
      !isCoreProfile &&
      mode === 'edit' &&
      model !== null &&
      effectiveCuratedCandidates.includes(cleanKey) &&
      effectiveCuratedCandidates.filter((k) => k !== cleanKey).length === 0 &&
      (!visible || !inCuratedSetup);

    if (removesLastVisibleCuratedEverflow && !options?.confirmedLastCurated) {
      setConfirmLastCurated(true);
      return;
    }
    setConfirmLastCurated(false);

    const targetExpectedRev = options?.overrideExpectedRevision ?? baseRevision;

    try {
      const payload: TeamCatalogModelUpsertPayload = {
        model_id: cleanModelId,
        provider: cleanProvider,
        display_name: displayName.trim() || cleanKey,
        visible,
        expected_revision: targetExpectedRev,
        comment: comment.trim() || undefined,
      };
      if (mode === 'create' || mode === 'clone') {
        payload.model_key = cleanKey;
      }

      // Single-transaction inline catalog provider variant update (strictly from currently selected variant)
      const variantUrlTrimmed = variantBaseUrl.trim();
      const variantKeyTrimmed = variantApiKey.trim();
      const variantNeedsUpdate =
        Boolean(selectedProviderMeta?.isVariant) &&
        (Boolean(variantKeyTrimmed) ||
          (Boolean(variantUrlTrimmed) && variantUrlTrimmed !== (selectedProviderMeta?.baseUrl ?? '')));
      if (variantNeedsUpdate && selectedProviderMeta) {
        const cleanUrl = variantUrlTrimmed || selectedProviderMeta.baseUrl || '';
        if (!cleanUrl.startsWith('https://')) {
          setErrorMsg(t('settings:dalton.teamCatalog.variantHttpsRequired'));
          return;
        }
        payload.provider_variant_update = {
          provider_slug: selectedProviderMeta.provider,
          display_name: selectedProviderMeta.displayName,
          sdk: selectedProviderMeta.sdk ?? 'openai',
          parent_provider: selectedProviderMeta.parentProvider ?? 'openai',
          access_type: selectedProviderMeta.accessType ?? 'api_key',
          billing: selectedProviderMeta.billing ?? billingMode,
          base_url: cleanUrl,
          ...(variantKeyTrimmed ? { api_key: variantKeyTrimmed } : {}),
        };
      }

      // Prompt guidance: explicit null deletes override when reset to default in edit mode
      if (promptGuidance.trim()) {
        payload.prompt_guidance = promptGuidance.trim();
      } else if (mode === 'edit' && initialPromptGuidance) {
        payload.prompt_guidance = null;
      }

      if (ctxVal !== null) payload.context = ctxVal;

      // Compaction profile: explicit null resets to auto in edit mode
      if (compactionProfile) {
        payload.compaction_profile = compactionProfile;
      } else if (mode === 'edit' && initialCompactionProfile) {
        payload.compaction_profile = null;
      }

      if (modalities.length > 0) payload.input_modalities = modalities;

      // Parameters: send ONLY the edit-sheet max_tokens patch (never echo non-UI parameters)
      if (maxTokVal !== null) {
        if (mode !== 'edit' || maxTokens.trim() !== initialMaxTokens.trim()) {
          payload.parameters = {
            max_tokens: maxTokVal,
            ...(initialHasMaxOutputTokensKey ? { max_output_tokens: null } : {}),
          };
        }
      } else if (mode === 'edit' && initialMaxTokens) {
        payload.parameters = { max_tokens: null, max_output_tokens: null };
      }

      const intelVal = parseOptionalInt(intelligence);
      const speedVal = parseOptionalInt(speed);
      const plainVal = parseOptionalInt(plainLanguage);
      if (intelVal !== null) {
        payload.intelligence = intelVal;
      } else if (mode === 'edit' && initialIntelligence) {
        payload.intelligence = null;
      }
      if (speedVal !== null) {
        payload.speed = speedVal;
      } else if (mode === 'edit' && initialSpeed) {
        payload.speed = null;
      }
      if (plainVal !== null) {
        payload.plain_language = plainVal;
      } else if (mode === 'edit' && initialPlainLanguage) {
        payload.plain_language = null;
      }

      // Reasoning: turning off submits null; clearing write submits write: null
      if (reasoningEnabled && reasoningEfforts.length > 0) {
        const nextReasoning: TeamCatalogReasoningConfig = {
          efforts: reasoningEfforts,
          default: reasoningDefault || reasoningEfforts[0],
        };
        if (reasoningWrite.trim()) {
          nextReasoning.write = reasoningWrite.trim();
        } else if (mode === 'edit' && initialReasoningWrite) {
          nextReasoning.write = null;
        }
        payload.reasoning = nextReasoning;
      } else if (mode === 'edit' && initialHasReasoning) {
        payload.reasoning = null;
      }

      const priceMeta = {
        source_kind: priceSourceKind,
        estimated: priceSourceKind === 'estimated' ? true : priceEstimated,
        ...(priceSourceUrl.trim() ? { source_url: priceSourceUrl.trim() } : {}),
        ...(priceVerifiedAt.trim() ? { verified_at: priceVerifiedAt.trim() } : {}),
        ...(priceNote.trim() ? { note: priceNote.trim() } : {}),
      };

      const daltonMeta: NonNullable<TeamCatalogModelUpsertPayload['dalton_meta']> = {};
      if (mode !== 'edit' || billingMode !== initialBillingMode) {
        daltonMeta.billing = billingMode;
      }
      // Tri-state tool_calling: omit when unchanged in edit mode or unverified on create/clone
      if (mode === 'edit') {
        if (toolCalling !== initialToolCalling) {
          daltonMeta.tool_calling =
            toolCalling === 'true' ? true : toolCalling === 'false' ? false : null;
        }
      } else if (toolCalling === 'true' || toolCalling === 'false') {
        daltonMeta.tool_calling = toolCalling === 'true';
      }

      if (family.trim()) {
        if (mode !== 'edit' || family.trim() !== initialFamily.trim()) {
          daltonMeta.family = family.trim();
        }
      } else if (mode === 'edit' && initialFamily) {
        daltonMeta.family = null;
      }
      if (oauthSyncEnabled) {
        if (series.trim()) {
          if (mode !== 'edit' || series.trim() !== initialSeries.trim()) {
            daltonMeta.series = series.trim();
          }
        } else if (mode === 'edit' && initialSeries) {
          daltonMeta.series = null;
        }
      }

      if (billingMode === 'cash') {
        const inRate = parseOptionalNumber(cashInput);
        const outRate = parseOptionalNumber(cashOutput);
        if ((mode === 'create' || mode === 'clone') && (inRate === null || outRate === null)) {
          setErrorMsg(t('settings:dalton.teamCatalog.cashPricingRequired'));
          return;
        }
        if (pricingTouched && (inRate !== null || outRate !== null)) {
          payload.pricing = {
            input: inRate ?? 0,
            output: outRate ?? 0,
            ...(parseOptionalNumber(cashCachedInput) !== null
              ? { cached_input: parseOptionalNumber(cashCachedInput) }
              : {}),
            ...(parseOptionalNumber(cashCache5m) !== null
              ? { cache_5m: parseOptionalNumber(cashCache5m) }
              : {}),
            ...(parseOptionalNumber(cashCache1h) !== null
              ? { cache_1h: parseOptionalNumber(cashCache1h) }
              : {}),
            unit: 'per_1m_tokens',
          };
          payload.pricing_meta = priceMeta;
        }
      } else {
        const nIn = parseOptionalNumber(notionalInput);
        const nOut = parseOptionalNumber(notionalOutput);
        const nCached = parseOptionalNumber(notionalCachedInput);
        if (nIn !== null || nOut !== null || nCached !== null) {
          daltonMeta.notional_pricing = {
            input: nIn ?? 0,
            output: nOut ?? 0,
            ...(nCached !== null ? { cached_input: nCached } : {}),
            ...priceMeta,
          };
          daltonMeta.notional_ref = null;
        } else if (notionalRef.trim()) {
          daltonMeta.notional_ref = notionalRef.trim();
          if (mode === 'edit' && initialHasNotionalPricing) {
            daltonMeta.notional_pricing = null;
          }
        } else if (mode === 'edit') {
          if (initialHasNotionalPricing) {
            daltonMeta.notional_pricing = null;
          }
          if (initialNotionalRef) {
            daltonMeta.notional_ref = null;
          }
        }
      }
      if (Object.keys(daltonMeta).length > 0) {
        payload.dalton_meta = daltonMeta;
      }

      const taskTimesVal = parseOptionalNumber(taskCostTimes);
      if (taskCostRefModel.trim() && taskTimesVal !== null) {
        payload.task_costs_bound = {
          model: taskCostRefModel.trim(),
          times: taskTimesVal,
        };
      } else if (mode === 'edit' && initialHasTaskCostBound) {
        payload.task_costs_bound = null;
      }

      // Single-transaction curated setup membership update when toggled
      if (inCuratedSetup !== initialInCuratedSetup) {
        payload.curated_setup_member = inCuratedSetup;
      }

      if (mode === 'edit') {
        await updateModelMutation.mutateAsync({ modelKey: cleanKey, payload });
      } else {
        await createModelMutation.mutateAsync(payload);
      }

      onSaved?.(
        t('settings:dalton.teamCatalog.modelSaveSuccess', {
          name: displayName.trim() || cleanKey,
        }),
      );
      onClose();
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.saveFailed'));
      if (parsed.code === 'revision_conflict') {
        onRefreshCatalog?.();
        setRevisionConflict({
          currentRevision: parsed.currentRevision ?? latestRevision ?? targetExpectedRev,
        });
      }
      setErrorMsg(parsed.message);
    }
  };

  const sheetTitle =
    mode === 'create_variant'
      ? t('settings:dalton.teamCatalog.addVariantTitle')
      : mode === 'edit_variant'
        ? t('settings:dalton.teamCatalog.editVariantTitle', { name: variant?.display_name || variant?.provider_slug || '' })
        : mode === 'edit'
          ? t('settings:dalton.teamCatalog.editModelTitle', { name: model?.display_name || model?.model_key || '' })
          : mode === 'clone'
            ? t('settings:dalton.teamCatalog.cloneModelTitle')
            : t('settings:dalton.teamCatalog.addModelTitle');

  const inputCls = 'text-base md:text-sm';
  const selectCls =
    'w-full rounded-md border px-3 py-2 text-base md:text-sm';
  const selectStyle = {
    backgroundColor: 'var(--color-bg-input)',
    borderColor: 'var(--color-border-muted)',
    color: 'var(--color-text-primary)',
  };

  const renderGroupSection = (id: GroupId, title: string, children: React.ReactNode) => {
    const expanded = !isMobile || openGroups[id];
    return (
      <section
        className="rounded-lg border overflow-hidden"
        style={{
          borderColor: 'var(--color-border-muted)',
          backgroundColor: 'var(--color-bg-card)',
        }}
        data-testid={`edit-sheet-group-${id}`}
      >
        <button
          type="button"
          className={cn(
            'flex w-full items-center justify-between gap-2 px-4 py-3 text-left text-xs font-semibold',
            isMobile && 'tap-target',
          )}
          style={{ color: 'var(--color-text-primary)' }}
          onClick={() => isMobile && toggleGroup(id)}
          aria-expanded={expanded}
        >
          <span>{title}</span>
          {isMobile && (
            expanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />
          )}
        </button>
        {expanded && (
          <div
            className="border-t px-4 py-3.5 space-y-3"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            {children}
          </div>
        )}
      </section>
    );
  };

  const resolvedLatestRevision = Math.max(
    revisionConflict?.currentRevision ?? 0,
    latestRevision ?? baseRevision,
  );

  return (
    <div
      className={cn(
        'fixed inset-0 z-1030 flex',
        isMobile ? 'items-end justify-center' : 'justify-end',
      )}
      style={{ backgroundColor: 'var(--color-bg-overlay-strong)' }}
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelId}
        data-sheet={isMobile ? '' : undefined}
        data-testid="team-model-edit-sheet"
        tabIndex={-1}
        className={cn(
          'flex flex-col border shadow-xl overflow-hidden',
          isMobile
            ? 'fixed left-0 right-0 bottom-[var(--visual-viewport-bottom-offset,var(--keyboard-overlap,0px))] z-1030 w-full h-[var(--visual-viewport-height,100dvh)] max-h-[var(--visual-viewport-height,100dvh)] rounded-none'
            : 'w-full max-w-2xl h-full',
        )}
        style={{
          backgroundColor: 'var(--color-shell-float, var(--color-bg-elevated))',
          borderColor: 'var(--color-shell-edge, var(--color-border-muted))',
        }}
      >
        {/* Sticky Header */}
        <div
          className="flex items-center justify-between gap-3 border-b px-5 py-3.5 shrink-0"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          <div className="min-w-0">
            <h2
              id={labelId}
              className="text-base font-semibold truncate"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {sheetTitle}
            </h2>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings:dalton.teamCatalog.sheetRevisionHint', { revision: baseRevision })}
            </p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="tap-target h-8 w-8 p-0"
            disabled={busy}
            onClick={onClose}
            aria-label={t('common.close')}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>

        {/* Scrollable Body */}
        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-4">
          {revisionConflict && (
            <DaltonInlineNotice tone="warning" testId="edit-sheet-revision-conflict">
              <div className="space-y-2">
                <div className="font-medium">
                  {t('settings:dalton.teamCatalog.revisionConflictTitle', {
                    latestRevision: resolvedLatestRevision,
                  })}
                </div>
                <div className="text-[11px] opacity-90">
                  {t('settings:dalton.teamCatalog.revisionConflictDesc')}
                </div>
                {!isVariantOnly && latestModel && (
                  <div
                    className="rounded border px-2.5 py-1.5 text-[11px] font-mono break-words"
                    style={{
                      borderColor: 'var(--color-border-muted)',
                      backgroundColor: 'var(--color-bg-card)',
                    }}
                    data-testid="edit-sheet-conflict-latest-summary"
                  >
                    {t('settings:dalton.teamCatalog.revisionConflictLatestSummary', {
                      displayName: latestModel.display_name,
                      modelId: latestModel.model_id,
                      visibility: latestModel.visible
                        ? t('settings:dalton.teamCatalog.filterVisible')
                        : t('settings:dalton.teamCatalog.filterHidden'),
                      provider: latestModel.provider,
                    })}
                  </div>
                )}
                {isVariantOnly && latestVariant && (
                  <div
                    className="rounded border px-2.5 py-1.5 text-[11px] font-mono break-words"
                    style={{
                      borderColor: 'var(--color-border-muted)',
                      backgroundColor: 'var(--color-bg-card)',
                    }}
                    data-testid="edit-sheet-conflict-latest-summary"
                  >
                    {t('settings:dalton.teamCatalog.revisionConflictLatestVariantSummary', {
                      displayName: latestVariant.display_name,
                      baseUrl: latestVariant.base_url,
                    })}
                  </div>
                )}
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <Button
                    type="button"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      const nextRev = resolvedLatestRevision;
                      setBaseRevision(nextRev);
                      setRevisionConflict(null);
                      if (isVariantOnly) {
                        void handleSaveVariantOnly(nextRev);
                      } else {
                        void handleSaveModel({ overrideExpectedRevision: nextRev });
                      }
                    }}
                    data-testid="edit-sheet-conflict-resubmit-btn"
                  >
                    {t('settings:dalton.teamCatalog.revisionConflictResubmit', {
                      latestRevision: resolvedLatestRevision,
                    })}
                  </Button>
                  {(latestModel || latestVariant) && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={busy}
                      onClick={() =>
                        populateFromSnapshot(
                          latestModel ?? model,
                          latestVariant ?? variant,
                          resolvedLatestRevision,
                        )
                      }
                      data-testid="edit-sheet-conflict-reload-btn"
                    >
                      {t('settings:dalton.teamCatalog.revisionConflictLoadLatest')}
                    </Button>
                  )}
                </div>
              </div>
            </DaltonInlineNotice>
          )}

          {errorMsg && !revisionConflict && (
            <DaltonInlineNotice tone="error" testId="edit-sheet-error">
              {errorMsg}
            </DaltonInlineNotice>
          )}

          {testResult && (
            <DaltonInlineNotice
              tone={testResult.ok ? 'success' : 'error'}
              testId="edit-sheet-test-result"
            >
              {testResult.ok ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span>{t('settings:dalton.teamCatalog.testCallSuccess')}</span>
                  <DaltonStatusBadge
                    tone={testResult.tool_calling_verified ? 'success' : 'warning'}
                    label={
                      testResult.tool_calling_verified
                        ? t('settings:dalton.teamCatalog.testCallToolVerified')
                        : t('settings:dalton.teamCatalog.testCallToolUnverified')
                    }
                  />
                </div>
              ) : (
                <div>
                  {t('settings:dalton.teamCatalog.testCallFailedDetail', {
                    message: testResult.message || testResult.error || '',
                  })}
                </div>
              )}
            </DaltonInlineNotice>
          )}

          {isVariantOnly ? (
            <div className="space-y-4" data-testid="variant-form-fields">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label htmlFor="variant-slug" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('settings:dalton.teamCatalog.variantSlugLabel')}
                  </label>
                  <Input
                    id="variant-slug"
                    value={variantSlug}
                    onChange={(e) => setVariantSlug(e.target.value)}
                    disabled={mode === 'edit_variant' || busy}
                    placeholder="acme-corp-gateway"
                    className={inputCls}
                    data-testid="variant-slug-input"
                  />
                </div>
                <div className="space-y-1">
                  <label htmlFor="variant-display-name" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('settings:dalton.teamCatalog.variantDisplayNameLabel')}
                  </label>
                  <Input
                    id="variant-display-name"
                    value={variantDisplayName}
                    onChange={(e) => setVariantDisplayName(e.target.value)}
                    disabled={busy}
                    placeholder={t('settings:dalton.teamCatalog.variantDisplayNamePlaceholder')}
                    className={inputCls}
                    data-testid="variant-display-name-input"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="space-y-1">
                  <label htmlFor="variant-parent" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('settings:dalton.teamCatalog.variantParentLabel')}
                  </label>
                  <select
                    id="variant-parent"
                    value={variantParent}
                    onChange={(e) => setVariantParent(e.target.value)}
                    disabled={busy}
                    className={selectCls}
                    style={selectStyle}
                    data-testid="variant-parent-select"
                  >
                    {visibleParentGroups.map((g) => (
                      <option key={g} value={g}>
                        {g}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <label htmlFor="variant-sdk" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('settings:dalton.teamCatalog.variantSdkLabel')}
                  </label>
                  <select
                    id="variant-sdk"
                    value={variantSdk}
                    onChange={(e) => setVariantSdk(e.target.value)}
                    disabled={busy}
                    className={selectCls}
                    style={selectStyle}
                    data-testid="variant-sdk-select"
                  >
                    {variantSdkOptions.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <label htmlFor="variant-billing" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('settings:dalton.teamCatalog.billingModeLabel')}
                  </label>
                  <select
                    id="variant-billing"
                    value={variantBilling}
                    onChange={(e) => setVariantBilling(e.target.value as TeamCatalogBillingMode)}
                    disabled={busy}
                    className={selectCls}
                    style={selectStyle}
                  >
                    <option value="cash">{t('settings:dalton.teamCatalog.billingCash')}</option>
                    <option value="subscription">{t('settings:dalton.teamCatalog.billingSubscription')}</option>
                  </select>
                </div>
              </div>

              <div className="space-y-1">
                <label htmlFor="variant-access-type" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('settings:dalton.teamCatalog.variantAccessTypeLabel')}
                </label>
                <select
                  id="variant-access-type"
                  value={variantAccessType}
                  onChange={(e) => setVariantAccessType(e.target.value as 'api_key' | 'coding_plan')}
                  disabled={busy}
                  className={selectCls}
                  style={selectStyle}
                >
                  <option value="api_key">API Key</option>
                  <option value="coding_plan">Coding Plan</option>
                </select>
              </div>

              <div className="space-y-1">
                <label htmlFor="variant-base-url" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('settings:dalton.teamCatalog.variantBaseUrlLabel')}
                </label>
                <Input
                  id="variant-base-url"
                  type="url"
                  value={variantBaseUrl}
                  onChange={(e) => {
                    setVariantBaseUrl(e.target.value);
                    setVariantApiKey('');
                  }}
                  disabled={busy}
                  placeholder="https://llm-gw.example.com/v1"
                  className={inputCls}
                  data-testid="variant-base-url-input"
                />
                <p className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings:dalton.teamCatalog.variantBaseUrlHint')}
                </p>
              </div>

              <div className="space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <label htmlFor="variant-api-key" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('settings:dalton.teamCatalog.variantApiKeyLabel')}
                  </label>
                  {variant?.credential_configured && (
                    <DaltonStatusBadge
                      tone="success"
                      label={t('settings:dalton.teamCatalog.variantCredentialConfigured', {
                        fingerprint: variant.key_fingerprint ?? 'configured',
                      })}
                    />
                  )}
                </div>
                <Input
                  id="variant-api-key"
                  type="password"
                  autoComplete="new-password"
                  value={variantApiKey}
                  onChange={(e) => setVariantApiKey(e.target.value)}
                  disabled={busy}
                  placeholder={
                    mode === 'edit_variant'
                      ? t('settings:dalton.teamCatalog.variantApiKeyRotatePlaceholder')
                      : t('settings:dalton.teamCatalog.variantApiKeyPlaceholder')
                  }
                  className={inputCls}
                  data-testid="variant-api-key-input"
                />
                <p className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings:dalton.teamCatalog.variantWriteOnlyHint')}
                </p>
              </div>

              <div className="space-y-1">
                <label htmlFor="variant-test-model-id" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('settings:dalton.teamCatalog.variantTestModelIdLabel')}
                </label>
                <Input
                  id="variant-test-model-id"
                  value={modelId}
                  onChange={(e) => setModelId(e.target.value)}
                  disabled={busy}
                  placeholder="gpt-5"
                  className={inputCls}
                />
              </div>
            </div>
          ) : (
            <>
              {/* Group 1: Identity & Provider */}
              {renderGroupSection(
                'identity',
                t('settings:dalton.teamCatalog.group1Title'),
                <>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label htmlFor="catalog-provider" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.providerLabel')}
                      </label>
                      <select
                        id="catalog-provider"
                        value={provider}
                        onChange={(e) => applySiblingDefaultsForProvider(e.target.value)}
                        disabled={busy}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-provider-select"
                      >
                        {visibleProviderOptions.map((opt) => (
                          <option key={opt.provider} value={opt.provider}>
                            {opt.displayName} ({opt.provider})
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="catalog-model-key" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.modelKeyLabel')}
                      </label>
                      <Input
                        id="catalog-model-key"
                        value={modelKey}
                        onChange={(e) => setModelKey(e.target.value)}
                        disabled={mode === 'edit' || busy}
                        placeholder="glm-99-preview-plan"
                        className={inputCls}
                        data-testid="edit-model-key-input"
                      />
                    </div>
                  </div>

                  {copiedFromSibling && (
                    <DaltonInlineNotice tone="info" testId="edit-sheet-sibling-copied">
                      {t('settings:dalton.teamCatalog.copiedDefaultsFromSibling', {
                        sibling: copiedFromSibling,
                      })}
                    </DaltonInlineNotice>
                  )}

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label htmlFor="catalog-display-name" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.displayNameLabel')}
                      </label>
                      <Input
                        id="catalog-display-name"
                        value={displayName}
                        onChange={(e) => setDisplayName(e.target.value)}
                        disabled={busy}
                        placeholder="GLM-99 Preview"
                        className={inputCls}
                        data-testid="edit-model-display-name-input"
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="catalog-model-id" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.modelIdLabel')}
                      </label>
                      <Input
                        id="catalog-model-id"
                        value={modelId}
                        onChange={(e) => setModelId(e.target.value)}
                        disabled={busy}
                        placeholder="glm-99-preview"
                        className={inputCls}
                        data-testid="edit-model-id-input"
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3 items-center">
                    <div className="space-y-1">
                      <label htmlFor="catalog-prompt-guidance" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.promptGuidanceLabel')}
                      </label>
                      <select
                        id="catalog-prompt-guidance"
                        value={promptGuidance}
                        onChange={(e) => setPromptGuidance(e.target.value)}
                        disabled={busy}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-prompt-guidance-select"
                      >
                        <option value="">{t('settings:dalton.teamCatalog.promptGuidanceDefault')}</option>
                        <option value="lean">lean</option>
                        <option value="detailed">detailed</option>
                      </select>
                    </div>
                    <div className="flex items-center justify-between gap-3 pt-4">
                      <span className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                        {t('settings:dalton.teamCatalog.visibleInPickerLabel')}
                      </span>
                      <ToggleSwitch
                        checked={visible}
                        onChange={() => {
                          setVisible((v) => !v);
                          setConfirmLastCurated(false);
                        }}
                        disabled={busy}
                        ariaLabel={t('settings:dalton.teamCatalog.visibleInPickerLabel')}
                        testId="edit-model-visible-toggle"
                      />
                    </div>
                  </div>

                  {/* Inline write-only credential binding when the selected provider is a catalog variant */}
                  {selectedProviderMeta?.isVariant && (
                    <div
                      className="rounded-md border p-3 space-y-2.5"
                      style={{
                        borderColor: 'var(--color-border-muted)',
                        backgroundColor: 'var(--color-bg-elevated)',
                      }}
                      data-testid="edit-sheet-variant-credential-box"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                          {t('settings:dalton.teamCatalog.variantCredentialBoxTitle')}
                        </span>
                        <DaltonStatusBadge
                          tone={selectedProviderMeta.credentialConfigured ? 'success' : 'warning'}
                          label={
                            selectedProviderMeta.credentialConfigured
                              ? t('settings:dalton.teamCatalog.variantCredentialConfigured', {
                                  fingerprint: selectedProviderMeta.keyFingerprint ?? 'ok',
                                })
                              : t('settings:dalton.teamCatalog.variantCredentialMissing')
                          }
                        />
                      </div>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                        <div className="space-y-1">
                          <label htmlFor="inline-variant-url" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                            {t('settings:dalton.teamCatalog.variantBaseUrlLabel')}
                          </label>
                          <Input
                            id="inline-variant-url"
                            type="url"
                            value={variantBaseUrl}
                            onChange={(e) => {
                              setVariantBaseUrl(e.target.value);
                              setVariantApiKey('');
                            }}
                            placeholder="https://llm-gw.example.com/v1"
                            className={inputCls}
                            data-testid="inline-variant-base-url-input"
                          />
                        </div>
                        <div className="space-y-1">
                          <label htmlFor="inline-variant-key" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                            {t('settings:dalton.teamCatalog.variantApiKeyLabel')}
                          </label>
                          <Input
                            id="inline-variant-key"
                            type="password"
                            autoComplete="new-password"
                            value={variantApiKey}
                            onChange={(e) => setVariantApiKey(e.target.value)}
                            placeholder={t('settings:dalton.teamCatalog.variantApiKeyRotatePlaceholder')}
                            className={inputCls}
                            data-testid="inline-variant-api-key-input"
                          />
                        </div>
                      </div>
                    </div>
                  )}
                </>,
              )}

              {/* Group 2: Pricing, Source & Task Cost */}
              {renderGroupSection(
                'pricing',
                t('settings:dalton.teamCatalog.group2Title'),
                <>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label htmlFor="catalog-billing" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.billingModeLabel')}
                      </label>
                      <select
                        id="catalog-billing"
                        value={billingMode}
                        onChange={(e) => {
                          setBillingMode(e.target.value as TeamCatalogBillingMode);
                          setPricingTouched(true);
                        }}
                        disabled={busy || selectedProviderMeta?.billing === 'subscription'}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-billing-select"
                      >
                        <option value="cash">{t('settings:dalton.teamCatalog.billingCash')}</option>
                        <option value="subscription">{t('settings:dalton.teamCatalog.billingSubscription')}</option>
                      </select>
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="catalog-price-source" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.priceSourceLabel')}
                      </label>
                      <select
                        id="catalog-price-source"
                        value={priceSourceKind}
                        onChange={(e) => {
                          const next = e.target.value as TeamCatalogPriceSourceKind;
                          setPriceSourceKind(next);
                          if (next === 'estimated') setPriceEstimated(true);
                          setPricingTouched(true);
                        }}
                        disabled={busy}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-price-source-select"
                      >
                        <option value="official">{t('settings:dalton.teamCatalog.priceSourceOfficial')}</option>
                        <option value="estimated">{t('settings:dalton.teamCatalog.priceSourceEstimated')}</option>
                        <option value="third_party">{t('settings:dalton.teamCatalog.priceSourceThirdParty')}</option>
                      </select>
                    </div>
                  </div>

                  {billingMode === 'cash' ? (
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-2.5">
                      <div className="space-y-1">
                        <label htmlFor="price-input" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                          {t('settings:dalton.teamCatalog.rateInputLabel')}
                        </label>
                        <Input
                          id="price-input"
                          type="number"
                          step="any"
                          min="0"
                          value={cashInput}
                          onChange={(e) => {
                            setCashInput(e.target.value);
                            setPricingTouched(true);
                          }}
                          className={inputCls}
                          data-testid="edit-model-cash-input"
                        />
                      </div>
                      <div className="space-y-1">
                        <label htmlFor="price-cached" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                          {t('settings:dalton.teamCatalog.rateCachedLabel')}
                        </label>
                        <Input
                          id="price-cached"
                          type="number"
                          step="any"
                          min="0"
                          value={cashCachedInput}
                          onChange={(e) => {
                            setCashCachedInput(e.target.value);
                            setPricingTouched(true);
                          }}
                          className={inputCls}
                        />
                      </div>
                      <div className="space-y-1">
                        <label htmlFor="price-output" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                          {t('settings:dalton.teamCatalog.rateOutputLabel')}
                        </label>
                        <Input
                          id="price-output"
                          type="number"
                          step="any"
                          min="0"
                          value={cashOutput}
                          onChange={(e) => {
                            setCashOutput(e.target.value);
                            setPricingTouched(true);
                          }}
                          className={inputCls}
                          data-testid="edit-model-cash-output"
                        />
                      </div>
                      <div className="space-y-1">
                        <label htmlFor="price-cache5m" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                          cache_5m
                        </label>
                        <Input
                          id="price-cache5m"
                          type="number"
                          step="any"
                          min="0"
                          value={cashCache5m}
                          onChange={(e) => {
                            setCashCache5m(e.target.value);
                            setPricingTouched(true);
                          }}
                          className={inputCls}
                        />
                      </div>
                      <div className="space-y-1">
                        <label htmlFor="price-cache1h" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                          cache_1h
                        </label>
                        <Input
                          id="price-cache1h"
                          type="number"
                          step="any"
                          min="0"
                          value={cashCache1h}
                          onChange={(e) => {
                            setCashCache1h(e.target.value);
                            setPricingTouched(true);
                          }}
                          className={inputCls}
                        />
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-2.5">
                      <div className="grid grid-cols-3 gap-2.5">
                        <div className="space-y-1">
                          <label htmlFor="notional-input" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                            {t('settings:dalton.teamCatalog.notionalInputLabel')}
                          </label>
                          <Input
                            id="notional-input"
                            type="number"
                            step="any"
                            min="0"
                            value={notionalInput}
                            onChange={(e) => setNotionalInput(e.target.value)}
                            className={inputCls}
                            data-testid="edit-model-notional-input"
                          />
                        </div>
                        <div className="space-y-1">
                          <label htmlFor="notional-cached" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                            {t('settings:dalton.teamCatalog.notionalCachedLabel')}
                          </label>
                          <Input
                            id="notional-cached"
                            type="number"
                            step="any"
                            min="0"
                            value={notionalCachedInput}
                            onChange={(e) => setNotionalCachedInput(e.target.value)}
                            className={inputCls}
                          />
                        </div>
                        <div className="space-y-1">
                          <label htmlFor="notional-output" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                            {t('settings:dalton.teamCatalog.notionalOutputLabel')}
                          </label>
                          <Input
                            id="notional-output"
                            type="number"
                            step="any"
                            min="0"
                            value={notionalOutput}
                            onChange={(e) => setNotionalOutput(e.target.value)}
                            className={inputCls}
                            data-testid="edit-model-notional-output"
                          />
                        </div>
                      </div>
                      <div className="space-y-1">
                        <label htmlFor="notional-ref" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                          {t('settings:dalton.teamCatalog.notionalRefLabel')}
                        </label>
                        <Input
                          id="notional-ref"
                          value={notionalRef}
                          onChange={(e) => setNotionalRef(e.target.value)}
                          placeholder="glm-5.3"
                          className={inputCls}
                        />
                      </div>
                    </div>
                  )}

                  <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5">
                    <div className="space-y-1">
                      <label htmlFor="price-verified-at" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.priceVerifiedAtLabel')}
                      </label>
                      <Input
                        id="price-verified-at"
                        value={priceVerifiedAt}
                        onChange={(e) => {
                          setPriceVerifiedAt(e.target.value);
                          setPricingTouched(true);
                        }}
                        placeholder="2026-11-25"
                        className={inputCls}
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="price-source-url" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.priceSourceUrlLabel')}
                      </label>
                      <Input
                        id="price-source-url"
                        value={priceSourceUrl}
                        onChange={(e) => {
                          setPriceSourceUrl(e.target.value);
                          setPricingTouched(true);
                        }}
                        placeholder="https://..."
                        className={inputCls}
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="price-note" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.priceNoteLabel')}
                      </label>
                      <Input
                        id="price-note"
                        value={priceNote}
                        onChange={(e) => {
                          setPriceNote(e.target.value);
                          setPricingTouched(true);
                        }}
                        className={inputCls}
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                    <div className="space-y-1">
                      <label htmlFor="task-cost-ref" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.taskCostRefLabel')}
                      </label>
                      <Input
                        id="task-cost-ref"
                        value={taskCostRefModel}
                        onChange={(e) => setTaskCostRefModel(e.target.value)}
                        placeholder="claude-sonnet-5-5"
                        className={inputCls}
                        data-testid="edit-model-task-cost-ref-input"
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="task-cost-times" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.taskCostTimesLabel')}
                      </label>
                      <Input
                        id="task-cost-times"
                        type="number"
                        step="any"
                        min="0"
                        value={taskCostTimes}
                        onChange={(e) => setTaskCostTimes(e.target.value)}
                        placeholder="1.0"
                        className={inputCls}
                        data-testid="edit-model-task-cost-times-input"
                      />
                    </div>
                  </div>
                </>,
              )}

              {/* Group 3: Context Window & Modalities */}
              {renderGroupSection(
                'window',
                t('settings:dalton.teamCatalog.group3Title'),
                <>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div className="space-y-1">
                      <label htmlFor="catalog-context" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.contextWindowLabel')}
                      </label>
                      <Input
                        id="catalog-context"
                        type="number"
                        min="1000"
                        value={contextWindow}
                        onChange={(e) => setContextWindow(e.target.value)}
                        className={inputCls}
                        data-testid="edit-model-context-input"
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="catalog-max-tokens" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.maxOutputLabel')}
                      </label>
                      <Input
                        id="catalog-max-tokens"
                        type="number"
                        min="1"
                        value={maxTokens}
                        onChange={(e) => setMaxTokens(e.target.value)}
                        className={inputCls}
                        data-testid="edit-model-max-tokens-input"
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="catalog-compaction" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.compactionProfileLabel')}
                      </label>
                      <select
                        id="catalog-compaction"
                        value={compactionProfile}
                        onChange={(e) => setCompactionProfile(e.target.value)}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-compaction-select"
                      >
                        {COMPACTION_PROFILES.map((cp) => (
                          <option key={cp || 'auto'} value={cp}>
                            {cp || t('settings:dalton.teamCatalog.compactionAuto')}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <div className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                      {t('settings:dalton.teamCatalog.modalitiesLabel')}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {MODALITIES.map((mod) => {
                        const active = modalities.includes(mod);
                        return (
                          <button
                            key={mod}
                            type="button"
                            onClick={() => toggleModality(mod)}
                            className="tap-target rounded-full border px-3 py-1 text-xs font-medium transition-colors"
                            style={{
                              borderColor: active
                                ? 'var(--color-accent-primary)'
                                : 'var(--color-border-muted)',
                              backgroundColor: active
                                ? 'color-mix(in srgb, var(--color-accent-primary) 14%, transparent)'
                                : 'var(--color-bg-elevated)',
                              color: active
                                ? 'var(--color-accent-primary)'
                                : 'var(--color-text-secondary)',
                            }}
                            aria-pressed={active}
                            data-testid={`modality-chip-${mod}`}
                          >
                            {mod}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </>,
              )}

              {/* Group 4: Reasoning Effort & Protocol */}
              {renderGroupSection(
                'reasoning',
                t('settings:dalton.teamCatalog.group4Title'),
                <>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                      {t('settings:dalton.teamCatalog.reasoningEnabledLabel')}
                    </span>
                    <ToggleSwitch
                      checked={reasoningEnabled}
                      onChange={() => setReasoningEnabled((v) => !v)}
                      disabled={busy}
                      ariaLabel={t('settings:dalton.teamCatalog.reasoningEnabledLabel')}
                      testId="edit-model-reasoning-toggle"
                    />
                  </div>
                  {reasoningEnabled && (
                    <div className="space-y-3">
                      <div className="space-y-1.5">
                        <div className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                          {t('settings:dalton.teamCatalog.reasoningEffortsLabel')}
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                          {REASONING_EFFORTS.map((eff) => {
                            const active = reasoningEfforts.includes(eff);
                            return (
                              <button
                                key={eff}
                                type="button"
                                onClick={() => toggleEffort(eff)}
                                className="tap-target rounded-full border px-2.5 py-1 text-xs"
                                style={{
                                  borderColor: active
                                    ? 'var(--color-accent-primary)'
                                    : 'var(--color-border-muted)',
                                  backgroundColor: active
                                    ? 'color-mix(in srgb, var(--color-accent-primary) 14%, transparent)'
                                    : 'var(--color-bg-elevated)',
                                  color: active
                                    ? 'var(--color-accent-primary)'
                                    : 'var(--color-text-secondary)',
                                }}
                                aria-pressed={active}
                              >
                                {eff}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div className="space-y-1">
                          <label htmlFor="reasoning-default" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                            {t('settings:dalton.teamCatalog.reasoningDefaultLabel')}
                          </label>
                          <select
                            id="reasoning-default"
                            value={reasoningDefault}
                            onChange={(e) => setReasoningDefault(e.target.value)}
                            className={selectCls}
                            style={selectStyle}
                          >
                            {reasoningEfforts.map((eff) => (
                              <option key={eff} value={eff}>
                                {eff}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="space-y-1">
                          <label htmlFor="reasoning-write" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                            {t('settings:dalton.teamCatalog.reasoningWriteLabel')}
                          </label>
                          <Input
                            id="reasoning-write"
                            value={reasoningWrite}
                            onChange={(e) => setReasoningWrite(e.target.value)}
                            placeholder="parameters.reasoning.effort"
                            className={inputCls}
                            data-testid="edit-model-reasoning-write-input"
                          />
                        </div>
                      </div>
                    </div>
                  )}
                </>,
              )}

              {/* Group 5: Ratings, Series & Curated Setup */}
              {renderGroupSection(
                'ratings',
                t('settings:dalton.teamCatalog.group5Title'),
                <>
                  <div className="grid grid-cols-3 gap-2.5">
                    <div className="space-y-1">
                      <label htmlFor="rating-intelligence" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.ratingIntelligence')}
                      </label>
                      <select
                        id="rating-intelligence"
                        value={intelligence}
                        onChange={(e) => setIntelligence(e.target.value)}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-intelligence-select"
                      >
                        {RATING_VALUES.map((rv) => (
                          <option key={rv || 'none'} value={rv}>
                            {rv || '-'}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="rating-speed" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.ratingSpeed')}
                      </label>
                      <select
                        id="rating-speed"
                        value={speed}
                        onChange={(e) => setSpeed(e.target.value)}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-speed-select"
                      >
                        {RATING_VALUES.map((rv) => (
                          <option key={rv || 'none'} value={rv}>
                            {rv || '-'}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="rating-plain" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.ratingPlainLanguage')}
                      </label>
                      <select
                        id="rating-plain"
                        value={plainLanguage}
                        onChange={(e) => setPlainLanguage(e.target.value)}
                        className={selectCls}
                        style={selectStyle}
                        data-testid="edit-model-plain-select"
                      >
                        {RATING_VALUES.map((rv) => (
                          <option key={rv || 'none'} value={rv}>
                            {rv || '-'}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label htmlFor="meta-family" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.familyLabel')}
                      </label>
                      <Input
                        id="meta-family"
                        value={family}
                        onChange={(e) => setFamily(e.target.value)}
                        placeholder="zai-glm"
                        className={inputCls}
                      />
                    </div>
                    {oauthSyncEnabled && (
                      <div className="space-y-1">
                        <label htmlFor="meta-series" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                          {t('settings:dalton.teamCatalog.seriesLabel')}
                        </label>
                        <Input
                          id="meta-series"
                          value={series}
                          onChange={(e) => setSeries(e.target.value)}
                          placeholder="antigravity-oauth:gemini-4-neon"
                          className={inputCls}
                        />
                      </div>
                    )}
                  </div>

                  <div className="space-y-1">
                    <label htmlFor="catalog-tool-calling" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                      {t('settings:dalton.teamCatalog.toolCallingLabel')}
                    </label>
                    <select
                      id="catalog-tool-calling"
                      value={toolCalling}
                      onChange={(e) => setToolCalling(e.target.value as ToolCallingTriState)}
                      disabled={busy}
                      className={selectCls}
                      style={selectStyle}
                      data-testid="edit-model-tool-calling-select"
                    >
                      <option value="unknown">{t('settings:dalton.teamCatalog.toolCallingUnknown')}</option>
                      <option value="true">{t('settings:dalton.teamCatalog.toolCallingSupported')}</option>
                      <option value="false">{t('settings:dalton.teamCatalog.toolCallingUnsupported')}</option>
                    </select>
                  </div>

                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                        {t('settings:dalton.teamCatalog.curatedToggleLabel')}
                      </div>
                      <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                        {t('settings:dalton.teamCatalog.curatedToggleDesc')}
                      </div>
                    </div>
                    <ToggleSwitch
                      checked={inCuratedSetup}
                      onChange={() => {
                        setInCuratedSetup((v) => !v);
                        setConfirmLastCurated(false);
                      }}
                      disabled={busy}
                      ariaLabel={t('settings:dalton.teamCatalog.curatedToggleLabel')}
                      testId="edit-model-curated-toggle"
                    />
                  </div>
                </>,
              )}
            </>
          )}

          {/* Optional audit comment */}
          <div className="space-y-1">
            <label htmlFor="edit-sheet-comment" className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
              {t('settings:dalton.teamCatalog.commentOptional')}
            </label>
            <Input
              id="edit-sheet-comment"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={t('settings:dalton.teamCatalog.editCommentPlaceholder')}
              className={inputCls}
              disabled={busy}
            />
          </div>

          {confirmLastCurated && (
            <DaltonInlineNotice tone="warning" testId="edit-sheet-last-curated-confirm">
              <div className="space-y-2">
                <div className="font-medium">
                  {t('settings:dalton.teamCatalog.lastCuratedEditTitle')}
                </div>
                <div className="text-[11px] opacity-90">
                  {t('settings:dalton.teamCatalog.lastCuratedEditWarning')}
                </div>
                <div className="flex items-center justify-end gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setConfirmLastCurated(false)}
                  >
                    {t('common.cancel')}
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    disabled={busy}
                    onClick={() => void handleSaveModel({ confirmedLastCurated: true })}
                    data-testid="edit-sheet-last-curated-confirm-btn"
                  >
                    {t('settings:dalton.teamCatalog.lastCuratedEditConfirmAction')}
                  </Button>
                </div>
              </div>
            </DaltonInlineNotice>
          )}

          {confirmRestoreDefault && (
            <DaltonInlineNotice tone="warning" testId="restore-default-confirm-box">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span>{t('settings:dalton.teamCatalog.restoreDefaultConfirmText')}</span>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setConfirmRestoreDefault(false)}
                  >
                    {t('common.cancel')}
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    disabled={busy}
                    onClick={() => void handleRestoreDefault()}
                    data-testid="restore-default-confirm-btn"
                  >
                    {t('settings:dalton.teamCatalog.restoreDefaultAction')}
                  </Button>
                </div>
              </div>
            </DaltonInlineNotice>
          )}
        </div>

        {/* Sticky Footer with safe-area-inset-bottom */}
        <div
          className="flex flex-wrap items-center justify-between gap-2 border-t px-5 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] shrink-0"
          style={{
            borderColor: 'var(--color-border-muted)',
            backgroundColor: 'var(--color-shell-float, var(--color-bg-elevated))',
          }}
          data-testid="edit-sheet-footer"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-1.5"
              disabled={testCallMutation.isPending || busy}
              onClick={() => void handleTestCall()}
              data-testid="edit-sheet-test-call-btn"
            >
              <FlaskConical className="h-3.5 w-3.5" />
              <span>
                {testCallMutation.isPending
                  ? t('settings:dalton.teamCatalog.testingCall')
                  : t('settings:dalton.teamCatalog.testCallAction')}
              </span>
            </Button>

            {mode === 'edit' && model?.source === 'team_overridden' && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="gap-1.5"
                disabled={busy}
                onClick={() => setConfirmRestoreDefault(true)}
                data-testid="edit-sheet-restore-default-btn"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                <span>{t('settings:dalton.teamCatalog.restoreDefaultAction')}</span>
              </Button>
            )}
          </div>

          <div className="flex items-center gap-2">
            <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={busy}
              onClick={() => void (isVariantOnly ? handleSaveVariantOnly() : handleSaveModel())}
              data-testid="edit-sheet-save-btn"
            >
              {busy
                ? t('settings:dalton.teamCatalog.saving')
                : t('settings:dalton.teamCatalog.saveEffectiveAction')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
