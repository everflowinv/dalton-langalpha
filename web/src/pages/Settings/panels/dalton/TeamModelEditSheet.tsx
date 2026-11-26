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
  TeamCatalogPricingCard,
  TeamCatalogPricingMeta,
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
  onRefreshCatalog?: () => Promise<number | void> | void;
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
type ConflictOperation = 'save_model' | 'restore_default' | 'save_variant';

interface RevisionConflictState {
  requiredRevision: number;
  operation: ConflictOperation;
  confirmedLastCurated?: boolean;
  refreshStatus: 'loading' | 'ready' | 'failed';
}

interface PendingLastCuratedConfirm {
  expectedRevision: number;
}

interface ConflictFieldItem {
  key: string;
  label: string;
  latestValue: string;
  draftValue?: string;
  directConflict: boolean;
}

interface EditBaseline {
  modelId: string;
  provider: string;
  variantBaseUrl: string;
  displayName: string;
  visible: boolean;
  promptGuidance: string;
  contextWindow: string;
  maxTokens: string;
  hasMaxTokensKey: boolean;
  hasMaxOutputTokensKey: boolean;
  compactionProfile: string;
  modalities: string[];
  hasReasoning: boolean;
  reasoningEfforts: string[];
  reasoningDefault: string;
  reasoningWrite: string;
  intelligence: string;
  speed: string;
  plainLanguage: string;
  billingMode: TeamCatalogBillingMode;
  hasCashPricing: boolean;
  cashInput: string;
  cashCachedInput: string;
  cashCache5m: string;
  cashCache1h: string;
  cashOutput: string;
  hasNotionalPricing: boolean;
  notionalInput: string;
  notionalCachedInput: string;
  notionalOutput: string;
  notionalRef: string;
  priceSourceKind: TeamCatalogPriceSourceKind;
  priceEstimated: boolean;
  priceSourceUrl: string;
  priceVerifiedAt: string;
  priceNote: string;
  hasTaskCostBound: boolean;
  taskCostRefModel: string;
  taskCostTimes: string;
  family: string;
  series: string;
  toolCalling: ToolCallingTriState;
  inCuratedSetup: boolean;
}

const EMPTY_BASELINE: EditBaseline = {
  modelId: '',
  provider: '',
  variantBaseUrl: '',
  displayName: '',
  visible: true,
  promptGuidance: '',
  contextWindow: '200000',
  maxTokens: '',
  hasMaxTokensKey: false,
  hasMaxOutputTokensKey: false,
  compactionProfile: '',
  modalities: ['text', 'image'],
  hasReasoning: false,
  reasoningEfforts: ['low', 'medium', 'high'],
  reasoningDefault: 'medium',
  reasoningWrite: '',
  intelligence: '',
  speed: '',
  plainLanguage: '',
  billingMode: 'cash',
  hasCashPricing: false,
  cashInput: '',
  cashCachedInput: '',
  cashCache5m: '',
  cashCache1h: '',
  cashOutput: '',
  hasNotionalPricing: false,
  notionalInput: '',
  notionalCachedInput: '',
  notionalOutput: '',
  notionalRef: '',
  priceSourceKind: 'official',
  priceEstimated: false,
  priceSourceUrl: '',
  priceVerifiedAt: '',
  priceNote: '',
  hasTaskCostBound: false,
  taskCostRefModel: '',
  taskCostTimes: '',
  family: '',
  series: '',
  toolCalling: 'unknown',
  inCuratedSetup: false,
};

function isOAuthKey(value: string): boolean {
  return value.trim().toLowerCase().endsWith('-oauth');
}

function sameStringList(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((v, idx) => v === b[idx]);
}

function resolveMaxOutputParamKey(
  providerName: string,
  providerSdk?: string | null,
  existingParams?: Record<string, unknown> | null,
): 'max_output_tokens' | 'max_tokens' {
  const sdk = (providerSdk ?? '').trim().toLowerCase();
  if (sdk) {
    return sdk === 'antigravity' || sdk === 'gemini' ? 'max_output_tokens' : 'max_tokens';
  }
  const prov = providerName.trim().toLowerCase();
  if (
    prov === 'antigravity-oauth' ||
    prov === 'antigravity' ||
    prov === 'gemini' ||
    prov === 'google'
  ) {
    return 'max_output_tokens';
  }
  if (
    existingParams &&
    existingParams.max_output_tokens != null &&
    existingParams.max_tokens == null
  ) {
    return 'max_output_tokens';
  }
  return 'max_tokens';
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

function formatReasoningSummary(
  enabled: boolean,
  efforts: readonly string[],
  defaultEffort: string,
  writePath: string,
): string {
  if (!enabled || efforts.length === 0) return '-';
  const parts = [`${efforts.join('/')} (${defaultEffort || efforts[0]})`];
  if (writePath.trim()) parts.push(writePath.trim());
  return parts.join(' · ');
}

function formatRateTuple(
  input: string,
  cached: string,
  output: string,
  extra?: string[],
): string {
  const parts: string[] = [];
  if (input.trim()) parts.push(`in=${input.trim()}`);
  if (cached.trim()) parts.push(`cached=${cached.trim()}`);
  if (output.trim()) parts.push(`out=${output.trim()}`);
  for (const item of extra ?? []) {
    if (item) parts.push(item);
  }
  return parts.length > 0 ? parts.join(', ') : '-';
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

  // Opened snapshot baseline (for expected_revision, diff-only edit patches & conflict comparison)
  const [baseRevision, setBaseRevision] = useState(revision);
  const [acknowledgedRevision, setAcknowledgedRevision] = useState<number | null>(null);
  const [baseline, setBaseline] = useState<EditBaseline>(EMPTY_BASELINE);
  const [variantUrlBaseline, setVariantUrlBaseline] = useState('');

  // Comment, test-call result, conflict state & confirmations
  const [comment, setComment] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<TeamCatalogTestCallResponse | null>(null);
  const [confirmRestoreDefault, setConfirmRestoreDefault] = useState(false);
  const [confirmLastCurated, setConfirmLastCurated] = useState<PendingLastCuratedConfirm | null>(null);
  const [revisionConflict, setRevisionConflict] = useState<RevisionConflictState | null>(null);

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
    setConfirmLastCurated(null);
    setRevisionConflict(null);
    setComment('');
    setVariantApiKey('');
    setBaseRevision(snapRevision);
    setAcknowledgedRevision(null);
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
      const initDisplayName =
        mode === 'clone' ? `${snapModel.display_name} (Copy)` : snapModel.display_name;
      setModelKey(clonedKey);
      setDisplayName(initDisplayName);
      setProvider(snapModel.provider);
      setModelId(snapModel.model_id);
      setVisible(snapModel.visible);
      const initPrompt = snapModel.prompt_guidance ?? '';
      setPromptGuidance(initPrompt);

      const provMeta = visibleProviderOptions.find((p) => p.provider === snapModel.provider);
      const initVariantBaseUrl =
        snapVariant?.provider_slug === snapModel.provider
          ? snapVariant.base_url
          : provMeta?.isVariant
            ? (provMeta.baseUrl ?? '')
            : '';
      setVariantBaseUrl(initVariantBaseUrl);
      setVariantUrlBaseline(initVariantBaseUrl);

      const effBilling: TeamCatalogBillingMode =
        snapModel.dalton_meta?.billing === 'subscription' || provMeta?.billing === 'subscription'
          ? 'subscription'
          : 'cash';
      setBillingMode(effBilling);

      const initCashIn = snapModel.pricing?.input != null ? String(snapModel.pricing.input) : '';
      const initCashCached =
        snapModel.pricing?.cached_input != null
          ? String(snapModel.pricing.cached_input)
          : snapModel.pricing?.cache_read != null
            ? String(snapModel.pricing.cache_read)
            : '';
      const initCash5m = snapModel.pricing?.cache_5m != null ? String(snapModel.pricing.cache_5m) : '';
      const initCash1h = snapModel.pricing?.cache_1h != null ? String(snapModel.pricing.cache_1h) : '';
      const initCashOut = snapModel.pricing?.output != null ? String(snapModel.pricing.output) : '';
      setCashInput(initCashIn);
      setCashCachedInput(initCashCached);
      setCashCache5m(initCash5m);
      setCashCache1h(initCash1h);
      setCashOutput(initCashOut);

      const np = snapModel.dalton_meta?.notional_pricing;
      const initNotionalIn = np?.input != null ? String(np.input) : '';
      const initNotionalCached =
        np?.cached_input != null
          ? String(np.cached_input)
          : np?.cache_read != null
            ? String(np.cache_read)
            : '';
      const initNotionalOut = np?.output != null ? String(np.output) : '';
      const initNotionalRef = snapModel.dalton_meta?.notional_ref ?? '';
      setNotionalInput(initNotionalIn);
      setNotionalCachedInput(initNotionalCached);
      setNotionalOutput(initNotionalOut);
      setNotionalRef(initNotionalRef);

      const pm = effBilling === 'subscription' ? np : snapModel.pricing_meta;
      const initPriceKind = (pm?.source_kind as TeamCatalogPriceSourceKind) || 'official';
      const initPriceEst = Boolean(pm?.estimated);
      const initPriceUrl = pm?.source_url ?? '';
      const initPriceVerified = pm?.verified_at ?? '';
      const initPriceNote = pm?.note ?? pm?.source ?? '';
      setPriceSourceKind(initPriceKind);
      setPriceEstimated(initPriceEst);
      setPriceSourceUrl(initPriceUrl);
      setPriceVerifiedAt(initPriceVerified);
      setPriceNote(initPriceNote);

      const initTaskRef = snapModel.task_costs_bound?.model ?? '';
      const initTaskTimes =
        snapModel.task_costs_bound?.times != null ? String(snapModel.task_costs_bound.times) : '';
      setTaskCostRefModel(initTaskRef);
      setTaskCostTimes(initTaskTimes);

      const initCtx = snapModel.context != null ? String(snapModel.context) : '';
      setContextWindow(initCtx);
      const paramMax =
        (snapModel.parameters?.max_tokens as number | undefined) ??
        (snapModel.parameters?.max_output_tokens as number | undefined);
      const initMaxStr = paramMax != null ? String(paramMax) : '';
      setMaxTokens(initMaxStr);
      const initCompaction = snapModel.compaction_profile ?? '';
      setCompactionProfile(initCompaction);
      const initModalities = snapModel.input_modalities?.length
        ? [...snapModel.input_modalities]
        : ['text'];
      setModalities(initModalities);

      const r = snapModel.reasoning;
      const hasReasoning = Boolean(r && Array.isArray(r.efforts) && r.efforts.length > 0);
      const initEfforts = hasReasoning && r?.efforts ? [...r.efforts] : ['low', 'medium', 'high'];
      const initDefaultEffort = r?.default ?? 'medium';
      const initWrite = typeof r?.write === 'string' ? r.write : '';
      setReasoningEnabled(hasReasoning);
      setReasoningEfforts(initEfforts);
      setReasoningDefault(initDefaultEffort);
      setReasoningWrite(initWrite);

      const initIntel = snapModel.intelligence != null ? String(snapModel.intelligence) : '';
      const initSpd = snapModel.speed != null ? String(snapModel.speed) : '';
      const initPlain = snapModel.plain_language != null ? String(snapModel.plain_language) : '';
      setIntelligence(initIntel);
      setSpeed(initSpd);
      setPlainLanguage(initPlain);
      const initFam = snapModel.dalton_meta?.family ?? '';
      setFamily(initFam);
      const initTc: ToolCallingTriState =
        snapModel.dalton_meta?.tool_calling === true
          ? 'true'
          : snapModel.dalton_meta?.tool_calling === false
            ? 'false'
            : 'unknown';
      setToolCalling(initTc);
      const initSer = snapModel.dalton_meta?.series ?? '';
      setSeries(initSer);

      const isCurated =
        mode === 'edit' &&
        (curatedSetup
          ? curatedSetup.includes(snapModel.model_key)
          : effectiveCuratedCandidates.includes(snapModel.model_key));
      setInCuratedSetup(isCurated);

      setBaseline({
        modelId: snapModel.model_id,
        provider: snapModel.provider,
        variantBaseUrl: initVariantBaseUrl,
        displayName: initDisplayName,
        visible: snapModel.visible,
        promptGuidance: initPrompt,
        contextWindow: initCtx,
        maxTokens: initMaxStr,
        hasMaxTokensKey: snapModel.parameters?.max_tokens != null,
        hasMaxOutputTokensKey: snapModel.parameters?.max_output_tokens != null,
        compactionProfile: initCompaction,
        modalities: initModalities,
        hasReasoning,
        reasoningEfforts: initEfforts,
        reasoningDefault: initDefaultEffort,
        reasoningWrite: initWrite,
        intelligence: initIntel,
        speed: initSpd,
        plainLanguage: initPlain,
        billingMode: effBilling,
        hasCashPricing: Boolean(
          snapModel.pricing &&
            (snapModel.pricing.input != null ||
              snapModel.pricing.output != null ||
              snapModel.pricing.cached_input != null ||
              snapModel.pricing.cache_read != null ||
              snapModel.pricing.cache_5m != null ||
              snapModel.pricing.cache_1h != null),
        ),
        cashInput: initCashIn,
        cashCachedInput: initCashCached,
        cashCache5m: initCash5m,
        cashCache1h: initCash1h,
        cashOutput: initCashOut,
        hasNotionalPricing: Boolean(
          np && (np.input != null || np.output != null || np.cached_input != null || np.cache_read != null),
        ),
        notionalInput: initNotionalIn,
        notionalCachedInput: initNotionalCached,
        notionalOutput: initNotionalOut,
        notionalRef: initNotionalRef,
        priceSourceKind: initPriceKind,
        priceEstimated: initPriceEst,
        priceSourceUrl: initPriceUrl,
        priceVerifiedAt: initPriceVerified,
        priceNote: initPriceNote,
        hasTaskCostBound: Boolean(
          snapModel.task_costs_bound?.model && snapModel.task_costs_bound?.times != null,
        ),
        taskCostRefModel: initTaskRef,
        taskCostTimes: initTaskTimes,
        family: initFam,
        series: initSer,
        toolCalling: initTc,
        inCuratedSetup: isCurated,
      });
      return;
    }

    // Create new model defaults
    const defaultProv = visibleProviderOptions[0]?.provider ?? 'openai';
    const defaultProvMeta = visibleProviderOptions[0];
    const defaultVariantUrl = defaultProvMeta?.isVariant ? (defaultProvMeta.baseUrl ?? '') : '';
    setModelKey('');
    setDisplayName('');
    setProvider(defaultProv);
    setVariantBaseUrl(defaultVariantUrl);
    setVariantUrlBaseline(defaultVariantUrl);
    setModelId('');
    setVisible(true);
    setPromptGuidance('');
    const initBilling = defaultProvMeta?.billing ?? 'cash';
    setBillingMode(initBilling);
    setCashInput('');
    setCashCachedInput('');
    setCashCache5m('');
    setCashCache1h('');
    setCashOutput('');
    setNotionalInput('');
    setNotionalCachedInput('');
    setNotionalOutput('');
    setNotionalRef('');
    setPriceSourceKind('official');
    setPriceEstimated(false);
    setPriceSourceUrl('');
    setPriceVerifiedAt('');
    setPriceNote('');
    setTaskCostRefModel('');
    setTaskCostTimes('');
    setContextWindow('200000');
    setMaxTokens('');
    setCompactionProfile('');
    setModalities(['text', 'image']);
    setReasoningEnabled(false);
    setReasoningEfforts(['low', 'medium', 'high']);
    setReasoningDefault('medium');
    setReasoningWrite('');
    setIntelligence('');
    setSpeed('');
    setPlainLanguage('');
    setFamily('');
    setToolCalling('unknown');
    setSeries('');
    setInCuratedSetup(false);
    setBaseline({
      ...EMPTY_BASELINE,
      provider: defaultProv,
      variantBaseUrl: defaultVariantUrl,
      billingMode: initBilling,
    });
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
    const nextVariantUrl =
      nextProvider === baseline.provider && baseline.variantBaseUrl
        ? baseline.variantBaseUrl
        : provMeta?.isVariant
          ? (provMeta.baseUrl ?? '')
          : '';
    setVariantBaseUrl(nextVariantUrl);
    setVariantUrlBaseline(nextVariantUrl);
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

  const resolvedLatestRevision = revisionConflict
    ? Math.max(revisionConflict.requiredRevision, latestRevision ?? 0)
    : (latestRevision ?? baseRevision);
  const conflictCatalogSynced = Boolean(
    revisionConflict &&
      revisionConflict.refreshStatus === 'ready' &&
      latestRevision !== undefined &&
      latestRevision >= revisionConflict.requiredRevision,
  );
  const conflictSyncPending = Boolean(revisionConflict && !conflictCatalogSynced);

  const beginConflictRecovery = async (
    requiredRevision: number,
    operation: ConflictOperation,
    options?: { confirmedLastCurated?: boolean },
  ) => {
    setRevisionConflict({
      requiredRevision,
      operation,
      confirmedLastCurated: options?.confirmedLastCurated,
      refreshStatus: 'loading',
    });
    if (!onRefreshCatalog) {
      setRevisionConflict((prev) =>
        prev && prev.requiredRevision === requiredRevision && prev.operation === operation
          ? {
              ...prev,
              refreshStatus:
                latestRevision !== undefined && latestRevision >= requiredRevision ? 'ready' : 'failed',
            }
          : prev,
      );
      return;
    }
    try {
      const refreshedRev = await onRefreshCatalog();
      const reachedTarget =
        typeof refreshedRev === 'number' ? refreshedRev >= requiredRevision : true;
      setRevisionConflict((prev) =>
        prev && prev.requiredRevision === requiredRevision && prev.operation === operation
          ? {
              ...prev,
              refreshStatus: reachedTarget ? 'ready' : 'failed',
            }
          : prev,
      );
    } catch {
      setRevisionConflict((prev) =>
        prev && prev.requiredRevision === requiredRevision && prev.operation === operation
          ? {
              ...prev,
              refreshStatus: 'failed',
            }
          : prev,
      );
    }
  };

  const handleRestoreDefault = async (overrideExpectedRevision?: number) => {
    if (!model || conflictSyncPending) return;
    setErrorMsg(null);
    const targetExpectedRev =
      overrideExpectedRevision ??
      (conflictCatalogSynced ? resolvedLatestRevision : (acknowledgedRevision ?? baseRevision));
    if (conflictCatalogSynced) {
      setAcknowledgedRevision(targetExpectedRev);
      setRevisionConflict(null);
    }
    try {
      await restoreDefaultMutation.mutateAsync({
        modelKey: model.model_key,
        expected_revision: targetExpectedRev,
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
        setConfirmRestoreDefault(false);
        const requiredRev =
          parsed.currentRevision ?? Math.max((latestRevision ?? targetExpectedRev), targetExpectedRev + 1);
        void beginConflictRecovery(requiredRev, 'restore_default');
      }
      setErrorMsg(parsed.message);
    }
  };

  const handleSaveVariantOnly = async (overrideExpectedRevision?: number) => {
    if (conflictSyncPending) return;
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
    const targetExpectedRev =
      overrideExpectedRevision ??
      (conflictCatalogSynced ? resolvedLatestRevision : (acknowledgedRevision ?? baseRevision));
    if (conflictCatalogSynced) {
      setAcknowledgedRevision(targetExpectedRev);
      setRevisionConflict(null);
    }
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
        const requiredRev =
          parsed.currentRevision ?? Math.max((latestRevision ?? targetExpectedRev), targetExpectedRev + 1);
        void beginConflictRecovery(requiredRev, 'save_variant');
      }
      setErrorMsg(parsed.message);
    }
  };

  const handleSaveModel = async (options?: {
    overrideExpectedRevision?: number;
    confirmedLastCurated?: boolean;
  }) => {
    if (conflictSyncPending) return;
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

    const targetExpectedRev =
      options?.overrideExpectedRevision ??
      (conflictCatalogSynced ? resolvedLatestRevision : (acknowledgedRevision ?? baseRevision));

    // Everflow last visible curated model risk confirmation (covers hiding OR un-curating from the EditSheet)
    const removesLastVisibleCuratedEverflow =
      !isCoreProfile &&
      mode === 'edit' &&
      model !== null &&
      effectiveCuratedCandidates.includes(cleanKey) &&
      effectiveCuratedCandidates.filter((k) => k !== cleanKey).length === 0 &&
      (!visible || !inCuratedSetup);

    if (removesLastVisibleCuratedEverflow && !options?.confirmedLastCurated) {
      setConfirmLastCurated({ expectedRevision: targetExpectedRev });
      return;
    }
    setConfirmLastCurated(null);

    const isEdit = mode === 'edit';

    try {
      const cleanDisplayName = displayName.trim() || cleanKey;
      const payload: TeamCatalogModelUpsertPayload = {
        expected_revision: targetExpectedRev,
        comment: comment.trim() || undefined,
      };
      if (!isEdit) {
        payload.model_key = cleanKey;
        payload.model_id = cleanModelId;
        payload.provider = cleanProvider;
        payload.display_name = cleanDisplayName;
        payload.visible = visible;
      } else {
        if (cleanModelId !== baseline.modelId.trim()) {
          payload.model_id = cleanModelId;
        }
        if (cleanProvider !== baseline.provider.trim()) {
          payload.provider = cleanProvider;
        }
        if (cleanDisplayName !== (baseline.displayName.trim() || cleanKey)) {
          payload.display_name = cleanDisplayName;
        }
        if (visible !== baseline.visible) {
          payload.visible = visible;
        }
      }

      // Single-transaction inline catalog provider variant update:
      // compare against the opened variant URL baseline so remote URL updates are never overwritten.
      const variantUrlTrimmed = variantBaseUrl.trim();
      const variantKeyTrimmed = variantApiKey.trim();
      const baselineVariantUrlTrimmed = variantUrlBaseline.trim();
      const latestVariantBaseUrl = (selectedProviderMeta?.baseUrl ?? '').trim();
      const variantUrlChanged =
        Boolean(variantUrlTrimmed) && variantUrlTrimmed !== baselineVariantUrlTrimmed;
      const variantUrlChangedRemotely =
        Boolean(selectedProviderMeta?.isVariant) &&
        Boolean(latestVariantBaseUrl) &&
        latestVariantBaseUrl !== baselineVariantUrlTrimmed;
      const variantNeedsUpdate =
        Boolean(selectedProviderMeta?.isVariant) &&
        (Boolean(variantKeyTrimmed) || variantUrlChanged);
      if (variantNeedsUpdate && selectedProviderMeta) {
        if (!variantUrlChanged && Boolean(variantKeyTrimmed) && variantUrlChangedRemotely) {
          setVariantBaseUrl(latestVariantBaseUrl);
          setVariantUrlBaseline(latestVariantBaseUrl);
          setVariantApiKey('');
          setErrorMsg(
            t('settings:dalton.teamCatalog.variantBaseUrlChangedReviewRequired', {
              latestBaseUrl: latestVariantBaseUrl,
            }),
          );
          return;
        }
        const cleanUrl = variantUrlTrimmed || latestVariantBaseUrl;
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
      const cleanPrompt = promptGuidance.trim();
      if (!isEdit) {
        if (cleanPrompt) payload.prompt_guidance = cleanPrompt;
      } else if (cleanPrompt !== baseline.promptGuidance.trim()) {
        payload.prompt_guidance = cleanPrompt || null;
      }

      if (ctxVal !== null && (!isEdit || contextWindow.trim() !== baseline.contextWindow.trim())) {
        payload.context = ctxVal;
      }

      // Compaction profile: explicit null resets to auto in edit mode
      if (!isEdit) {
        if (compactionProfile) payload.compaction_profile = compactionProfile;
      } else if (compactionProfile !== baseline.compactionProfile) {
        payload.compaction_profile = compactionProfile || null;
      }

      if (modalities.length > 0 && (!isEdit || !sameStringList(modalities, baseline.modalities))) {
        payload.input_modalities = modalities;
      }

      // Parameters: select max output key strictly from the final saved provider's SDK (name fallback only when SDK is absent)
      if (maxTokVal !== null) {
        if (!isEdit || maxTokens.trim() !== baseline.maxTokens.trim()) {
          const effectiveSavedProvider =
            isEdit && cleanProvider === baseline.provider.trim() && latestModel?.provider
              ? latestModel.provider
              : cleanProvider;
          const effectiveSavedProviderMeta =
            visibleProviderOptions.find((p) => p.provider === effectiveSavedProvider) ??
            (effectiveSavedProvider === cleanProvider ? selectedProviderMeta : null);
          const effectiveSavedProviderSdk =
            effectiveSavedProviderMeta?.sdk ??
            (effectiveSavedProvider === (latestModel?.provider ?? '') ? latestModel?.provider_sdk : null) ??
            (effectiveSavedProvider === (model?.provider ?? '') ? model?.provider_sdk : null);
          const existingSavedParams =
            isEdit && effectiveSavedProvider === (latestModel?.provider ?? model?.provider ?? '')
              ? (latestModel?.parameters ?? model?.parameters)
              : null;
          const targetMaxKey = resolveMaxOutputParamKey(
            effectiveSavedProvider,
            effectiveSavedProviderSdk,
            existingSavedParams,
          );
          if (targetMaxKey === 'max_output_tokens') {
            payload.parameters = {
              max_output_tokens: maxTokVal,
              ...(isEdit && (baseline.hasMaxTokensKey || existingSavedParams?.max_tokens != null)
                ? { max_tokens: null }
                : {}),
            };
          } else {
            payload.parameters = {
              max_tokens: maxTokVal,
              ...(isEdit &&
              (baseline.hasMaxOutputTokensKey || existingSavedParams?.max_output_tokens != null)
                ? { max_output_tokens: null }
                : {}),
            };
          }
        }
      } else if (isEdit && baseline.maxTokens.trim()) {
        payload.parameters = { max_tokens: null, max_output_tokens: null };
      }

      const intelVal = parseOptionalInt(intelligence);
      const speedVal = parseOptionalInt(speed);
      const plainVal = parseOptionalInt(plainLanguage);
      if (!isEdit) {
        if (intelVal !== null) payload.intelligence = intelVal;
        if (speedVal !== null) payload.speed = speedVal;
        if (plainVal !== null) payload.plain_language = plainVal;
      } else {
        if (intelligence !== baseline.intelligence) {
          payload.intelligence = intelVal;
        }
        if (speed !== baseline.speed) {
          payload.speed = speedVal;
        }
        if (plainLanguage !== baseline.plainLanguage) {
          payload.plain_language = plainVal;
        }
      }

      // Reasoning: diff sub-fields individually in edit mode; turning off submits null; clearing write submits write: null
      if (!isEdit) {
        if (reasoningEnabled && reasoningEfforts.length > 0) {
          payload.reasoning = {
            efforts: reasoningEfforts,
            default: reasoningDefault || reasoningEfforts[0],
            ...(reasoningWrite.trim() ? { write: reasoningWrite.trim() } : {}),
          };
        }
      } else if (!reasoningEnabled) {
        if (baseline.hasReasoning) {
          payload.reasoning = null;
        }
      } else if (reasoningEfforts.length > 0) {
        const cleanDefault = reasoningDefault || reasoningEfforts[0];
        const cleanWrite = reasoningWrite.trim();
        if (!baseline.hasReasoning) {
          payload.reasoning = {
            efforts: reasoningEfforts,
            default: cleanDefault,
            ...(cleanWrite ? { write: cleanWrite } : {}),
          };
        } else {
          const nextReasoning: TeamCatalogReasoningConfig = {};
          if (!sameStringList(reasoningEfforts, baseline.reasoningEfforts)) {
            nextReasoning.efforts = reasoningEfforts;
          }
          if (cleanDefault !== baseline.reasoningDefault) {
            nextReasoning.default = cleanDefault;
          }
          if (cleanWrite !== baseline.reasoningWrite.trim()) {
            nextReasoning.write = cleanWrite || null;
          }
          if (Object.keys(nextReasoning).length > 0) {
            payload.reasoning = nextReasoning;
          }
        }
      }

      const effectivePriceEstimated = priceSourceKind === 'estimated' ? true : priceEstimated;
      const buildFullPriceMeta = (): TeamCatalogPricingMeta => ({
        source_kind: priceSourceKind,
        estimated: effectivePriceEstimated,
        ...(priceSourceUrl.trim() ? { source_url: priceSourceUrl.trim() } : {}),
        ...(priceVerifiedAt.trim() ? { verified_at: priceVerifiedAt.trim() } : {}),
        ...(priceNote.trim() ? { note: priceNote.trim() } : {}),
      });
      const buildDiffPriceMeta = (): TeamCatalogPricingMeta => {
        const metaPatch: TeamCatalogPricingMeta = {};
        if (priceSourceKind !== baseline.priceSourceKind) {
          metaPatch.source_kind = priceSourceKind;
        }
        if (effectivePriceEstimated !== baseline.priceEstimated) {
          metaPatch.estimated = effectivePriceEstimated;
        }
        if (priceSourceUrl.trim() !== baseline.priceSourceUrl.trim()) {
          metaPatch.source_url = priceSourceUrl.trim() || null;
        }
        if (priceVerifiedAt.trim() !== baseline.priceVerifiedAt.trim()) {
          metaPatch.verified_at = priceVerifiedAt.trim() || null;
        }
        if (priceNote.trim() !== baseline.priceNote.trim()) {
          metaPatch.note = priceNote.trim() || null;
        }
        return metaPatch;
      };

      const daltonMeta: NonNullable<TeamCatalogModelUpsertPayload['dalton_meta']> = {};
      if (!isEdit || billingMode !== baseline.billingMode) {
        daltonMeta.billing = billingMode;
      }
      // Tri-state tool_calling: omit when unchanged in edit mode or unverified on create/clone
      if (isEdit) {
        if (toolCalling !== baseline.toolCalling) {
          daltonMeta.tool_calling =
            toolCalling === 'true' ? true : toolCalling === 'false' ? false : null;
        }
      } else if (toolCalling === 'true' || toolCalling === 'false') {
        daltonMeta.tool_calling = toolCalling === 'true';
      }

      if (family.trim()) {
        if (!isEdit || family.trim() !== baseline.family.trim()) {
          daltonMeta.family = family.trim();
        }
      } else if (isEdit && baseline.family.trim()) {
        daltonMeta.family = null;
      }
      if (oauthSyncEnabled) {
        if (series.trim()) {
          if (!isEdit || series.trim() !== baseline.series.trim()) {
            daltonMeta.series = series.trim();
          }
        } else if (isEdit && baseline.series.trim()) {
          daltonMeta.series = null;
        }
      }

      if (billingMode === 'cash') {
        const inRate = parseOptionalNumber(cashInput);
        const outRate = parseOptionalNumber(cashOutput);
        const cachedRate = parseOptionalNumber(cashCachedInput);
        const cache5mRate = parseOptionalNumber(cashCache5m);
        const cache1hRate = parseOptionalNumber(cashCache1h);
        if (!isEdit && (inRate === null || outRate === null)) {
          setErrorMsg(t('settings:dalton.teamCatalog.cashPricingRequired'));
          return;
        }
        const creatingCashPricingCard =
          !isEdit || billingMode !== baseline.billingMode || !baseline.hasCashPricing;
        if (creatingCashPricingCard) {
          if (inRate !== null || outRate !== null) {
            payload.pricing = {
              input: inRate ?? 0,
              output: outRate ?? 0,
              ...(cachedRate !== null ? { cached_input: cachedRate } : {}),
              ...(cache5mRate !== null ? { cache_5m: cache5mRate } : {}),
              ...(cache1hRate !== null ? { cache_1h: cache1hRate } : {}),
              unit: 'per_1m_tokens',
            };
            payload.pricing_meta = buildFullPriceMeta();
          }
        } else {
          const pricingPatch: TeamCatalogPricingCard = {};
          if (cashInput.trim() !== baseline.cashInput.trim()) {
            pricingPatch.input = inRate;
          }
          if (cashOutput.trim() !== baseline.cashOutput.trim()) {
            pricingPatch.output = outRate;
          }
          if (cashCachedInput.trim() !== baseline.cashCachedInput.trim()) {
            pricingPatch.cached_input = cachedRate;
          }
          if (cashCache5m.trim() !== baseline.cashCache5m.trim()) {
            pricingPatch.cache_5m = cache5mRate;
          }
          if (cashCache1h.trim() !== baseline.cashCache1h.trim()) {
            pricingPatch.cache_1h = cache1hRate;
          }
          if (Object.keys(pricingPatch).length > 0) {
            payload.pricing = pricingPatch;
          }
          const metaPatch = buildDiffPriceMeta();
          if (Object.keys(metaPatch).length > 0) {
            payload.pricing_meta = metaPatch;
          }
        }
      } else {
        const nIn = parseOptionalNumber(notionalInput);
        const nOut = parseOptionalNumber(notionalOutput);
        const nCached = parseOptionalNumber(notionalCachedInput);
        const creatingNotionalCard =
          !isEdit || billingMode !== baseline.billingMode || !baseline.hasNotionalPricing;
        if (nIn !== null || nOut !== null || nCached !== null) {
          if (creatingNotionalCard) {
            daltonMeta.notional_pricing = {
              input: nIn ?? 0,
              output: nOut ?? 0,
              ...(nCached !== null ? { cached_input: nCached } : {}),
              ...buildFullPriceMeta(),
            };
            if (!isEdit || baseline.notionalRef.trim()) {
              daltonMeta.notional_ref = null;
            }
          } else {
            const npPatch: TeamCatalogPricingCard = {};
            if (notionalInput.trim() !== baseline.notionalInput.trim()) {
              npPatch.input = nIn;
            }
            if (notionalOutput.trim() !== baseline.notionalOutput.trim()) {
              npPatch.output = nOut;
            }
            if (notionalCachedInput.trim() !== baseline.notionalCachedInput.trim()) {
              npPatch.cached_input = nCached;
            }
            Object.assign(npPatch, buildDiffPriceMeta());
            if (Object.keys(npPatch).length > 0) {
              daltonMeta.notional_pricing = npPatch;
            }
            if (notionalRef.trim() !== baseline.notionalRef.trim()) {
              daltonMeta.notional_ref = notionalRef.trim() || null;
            }
          }
        } else if (notionalRef.trim()) {
          if (!isEdit || notionalRef.trim() !== baseline.notionalRef.trim()) {
            daltonMeta.notional_ref = notionalRef.trim();
          }
          if (isEdit && baseline.hasNotionalPricing) {
            daltonMeta.notional_pricing = null;
          }
        } else if (isEdit) {
          if (baseline.hasNotionalPricing) {
            daltonMeta.notional_pricing = null;
          }
          if (baseline.notionalRef.trim()) {
            daltonMeta.notional_ref = null;
          }
        }
      }
      if (Object.keys(daltonMeta).length > 0) {
        payload.dalton_meta = daltonMeta;
      }

      const taskTimesVal = parseOptionalNumber(taskCostTimes);
      const taskCostChanged =
        !isEdit ||
        taskCostRefModel.trim() !== baseline.taskCostRefModel.trim() ||
        taskCostTimes.trim() !== baseline.taskCostTimes.trim();
      if (taskCostChanged) {
        if (taskCostRefModel.trim() && taskTimesVal !== null) {
          payload.task_costs_bound = {
            model: taskCostRefModel.trim(),
            times: taskTimesVal,
          };
        } else if (isEdit && baseline.hasTaskCostBound) {
          payload.task_costs_bound = null;
        }
      }

      // Single-transaction curated setup membership update when toggled
      if (inCuratedSetup !== baseline.inCuratedSetup) {
        payload.curated_setup_member = inCuratedSetup;
      }

      if (conflictCatalogSynced) {
        setAcknowledgedRevision(targetExpectedRev);
        setRevisionConflict(null);
      }

      if (isEdit) {
        await updateModelMutation.mutateAsync({ modelKey: cleanKey, payload });
      } else {
        await createModelMutation.mutateAsync(payload);
      }

      onSaved?.(
        t('settings:dalton.teamCatalog.modelSaveSuccess', {
          name: cleanDisplayName,
        }),
      );
      onClose();
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.saveFailed'));
      if (parsed.code === 'revision_conflict') {
        const requiredRev =
          parsed.currentRevision ?? Math.max((latestRevision ?? targetExpectedRev), targetExpectedRev + 1);
        void beginConflictRecovery(requiredRev, 'save_model', {
          confirmedLastCurated: Boolean(options?.confirmedLastCurated),
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

  const conflictedFields: ConflictFieldItem[] = (() => {
    if (!revisionConflict || !conflictCatalogSynced) return [];
    const items: ConflictFieldItem[] = [];
    const pushIfRemoteChanged = (
      key: string,
      label: string,
      openedVal: string,
      latestVal: string,
      draftVal: string,
    ) => {
      if (latestVal === openedVal) return;
      const draftChanged = draftVal !== openedVal;
      items.push({
        key,
        label,
        latestValue: latestVal || '-',
        ...(draftChanged ? { draftValue: draftVal || '-' } : {}),
        directConflict: draftChanged && draftVal !== latestVal,
      });
    };

    if (!isVariantOnly && model && latestModel) {
      const visLabel = (v: boolean) =>
        v ? t('settings:dalton.teamCatalog.filterVisible') : t('settings:dalton.teamCatalog.filterHidden');
      pushIfRemoteChanged(
        'display_name',
        t('settings:dalton.teamCatalog.displayNameLabel'),
        baseline.displayName,
        latestModel.display_name,
        displayName.trim() || modelKey.trim(),
      );
      pushIfRemoteChanged(
        'model_id',
        t('settings:dalton.teamCatalog.modelIdLabel'),
        baseline.modelId,
        latestModel.model_id,
        modelId.trim(),
      );
      pushIfRemoteChanged(
        'provider',
        t('settings:dalton.teamCatalog.providerLabel'),
        baseline.provider,
        latestModel.provider,
        provider.trim(),
      );
      if (selectedProviderMeta?.isVariant && baseline.variantBaseUrl) {
        pushIfRemoteChanged(
          'variant_base_url',
          t('settings:dalton.teamCatalog.variantBaseUrlLabel'),
          baseline.variantBaseUrl,
          (selectedProviderMeta.baseUrl ?? '').trim(),
          variantBaseUrl.trim(),
        );
      }
      pushIfRemoteChanged(
        'visible',
        t('settings:dalton.teamCatalog.visibleInPickerLabel'),
        visLabel(baseline.visible),
        visLabel(latestModel.visible),
        visLabel(visible),
      );
      pushIfRemoteChanged(
        'prompt_guidance',
        t('settings:dalton.teamCatalog.promptGuidanceLabel'),
        baseline.promptGuidance,
        latestModel.prompt_guidance ?? '',
        promptGuidance.trim(),
      );
      pushIfRemoteChanged(
        'context',
        t('settings:dalton.teamCatalog.contextWindowLabel'),
        baseline.contextWindow,
        latestModel.context != null ? String(latestModel.context) : '',
        contextWindow.trim(),
      );
      const latestMax =
        (latestModel.parameters?.max_tokens as number | undefined) ??
        (latestModel.parameters?.max_output_tokens as number | undefined);
      pushIfRemoteChanged(
        'max_tokens',
        t('settings:dalton.teamCatalog.maxOutputLabel'),
        baseline.maxTokens,
        latestMax != null ? String(latestMax) : '',
        maxTokens.trim(),
      );
      pushIfRemoteChanged(
        'compaction_profile',
        t('settings:dalton.teamCatalog.compactionProfileLabel'),
        baseline.compactionProfile,
        latestModel.compaction_profile ?? '',
        compactionProfile,
      );
      pushIfRemoteChanged(
        'input_modalities',
        t('settings:dalton.teamCatalog.modalitiesLabel'),
        baseline.modalities.join(', '),
        (latestModel.input_modalities?.length ? latestModel.input_modalities : ['text']).join(', '),
        modalities.join(', '),
      );
      const lr = latestModel.reasoning;
      const latestHasReasoning = Boolean(lr && Array.isArray(lr.efforts) && lr.efforts.length > 0);
      pushIfRemoteChanged(
        'reasoning',
        t('settings:dalton.teamCatalog.reasoningEnabledLabel'),
        formatReasoningSummary(
          baseline.hasReasoning,
          baseline.reasoningEfforts,
          baseline.reasoningDefault,
          baseline.reasoningWrite,
        ),
        formatReasoningSummary(
          latestHasReasoning,
          lr?.efforts ?? [],
          lr?.default ?? '',
          typeof lr?.write === 'string' ? lr.write : '',
        ),
        formatReasoningSummary(reasoningEnabled, reasoningEfforts, reasoningDefault, reasoningWrite),
      );
      pushIfRemoteChanged(
        'intelligence',
        t('settings:dalton.teamCatalog.ratingIntelligence'),
        baseline.intelligence,
        latestModel.intelligence != null ? String(latestModel.intelligence) : '',
        intelligence,
      );
      pushIfRemoteChanged(
        'speed',
        t('settings:dalton.teamCatalog.ratingSpeed'),
        baseline.speed,
        latestModel.speed != null ? String(latestModel.speed) : '',
        speed,
      );
      pushIfRemoteChanged(
        'plain_language',
        t('settings:dalton.teamCatalog.ratingPlainLanguage'),
        baseline.plainLanguage,
        latestModel.plain_language != null ? String(latestModel.plain_language) : '',
        plainLanguage,
      );
      const latestCashCached =
        latestModel.pricing?.cached_input != null
          ? String(latestModel.pricing.cached_input)
          : latestModel.pricing?.cache_read != null
            ? String(latestModel.pricing.cache_read)
            : '';
      pushIfRemoteChanged(
        'pricing',
        t('settings:dalton.teamCatalog.colPricing'),
        formatRateTuple(baseline.cashInput, baseline.cashCachedInput, baseline.cashOutput),
        formatRateTuple(
          latestModel.pricing?.input != null ? String(latestModel.pricing.input) : '',
          latestCashCached,
          latestModel.pricing?.output != null ? String(latestModel.pricing.output) : '',
        ),
        formatRateTuple(cashInput, cashCachedInput, cashOutput),
      );
      const latestPm =
        (latestModel.dalton_meta?.billing === 'subscription'
          ? latestModel.dalton_meta?.notional_pricing
          : latestModel.pricing_meta) ?? null;
      pushIfRemoteChanged(
        'pricing_meta',
        t('settings:dalton.teamCatalog.priceSourceLabel'),
        [baseline.priceSourceKind, baseline.priceVerifiedAt, baseline.priceSourceUrl, baseline.priceNote]
          .filter(Boolean)
          .join(' · '),
        [
          latestPm?.source_kind || 'official',
          latestPm?.verified_at ?? '',
          latestPm?.source_url ?? '',
          latestPm?.note ?? latestPm?.source ?? '',
        ]
          .filter(Boolean)
          .join(' · '),
        [priceSourceKind, priceVerifiedAt.trim(), priceSourceUrl.trim(), priceNote.trim()]
          .filter(Boolean)
          .join(' · '),
      );
      const latestNp = latestModel.dalton_meta?.notional_pricing;
      const latestNotionalCached =
        latestNp?.cached_input != null
          ? String(latestNp.cached_input)
          : latestNp?.cache_read != null
            ? String(latestNp.cache_read)
            : '';
      pushIfRemoteChanged(
        'notional_pricing',
        t('settings:dalton.teamCatalog.notionalInputLabel'),
        formatRateTuple(baseline.notionalInput, baseline.notionalCachedInput, baseline.notionalOutput),
        formatRateTuple(
          latestNp?.input != null ? String(latestNp.input) : '',
          latestNotionalCached,
          latestNp?.output != null ? String(latestNp.output) : '',
        ),
        formatRateTuple(notionalInput, notionalCachedInput, notionalOutput),
      );
      const latestTc: ToolCallingTriState =
        latestModel.dalton_meta?.tool_calling === true
          ? 'true'
          : latestModel.dalton_meta?.tool_calling === false
            ? 'false'
            : 'unknown';
      pushIfRemoteChanged(
        'tool_calling',
        t('settings:dalton.teamCatalog.toolCallingLabel'),
        baseline.toolCalling,
        latestTc,
        toolCalling,
      );
      const latestCurated = curatedSetup
        ? curatedSetup.includes(latestModel.model_key)
        : effectiveCuratedCandidates.includes(latestModel.model_key);
      pushIfRemoteChanged(
        'curated_setup',
        t('settings:dalton.teamCatalog.curatedToggleLabel'),
        String(baseline.inCuratedSetup),
        String(latestCurated),
        String(inCuratedSetup),
      );
    } else if (isVariantOnly && variant && latestVariant) {
      pushIfRemoteChanged(
        'display_name',
        t('settings:dalton.teamCatalog.variantDisplayNameLabel'),
        variant.display_name,
        latestVariant.display_name,
        variantDisplayName.trim() || variantSlug.trim(),
      );
      pushIfRemoteChanged(
        'parent_provider',
        t('settings:dalton.teamCatalog.variantParentLabel'),
        variant.parent_provider,
        latestVariant.parent_provider,
        variantParent,
      );
      pushIfRemoteChanged(
        'sdk',
        t('settings:dalton.teamCatalog.variantSdkLabel'),
        variant.sdk,
        latestVariant.sdk,
        variantSdk,
      );
      pushIfRemoteChanged(
        'base_url',
        t('settings:dalton.teamCatalog.variantBaseUrlLabel'),
        variant.base_url,
        latestVariant.base_url,
        variantBaseUrl.trim(),
      );
    }
    return items;
  })();

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
                {!conflictCatalogSynced && (
                  <div
                    className="rounded border px-2.5 py-1.5 text-[11px] flex flex-wrap items-center justify-between gap-2"
                    style={{
                      borderColor: 'var(--color-border-muted)',
                      backgroundColor: 'var(--color-bg-card)',
                    }}
                    data-testid={
                      revisionConflict.refreshStatus === 'failed'
                        ? 'edit-sheet-conflict-refresh-failed'
                        : 'edit-sheet-conflict-syncing'
                    }
                  >
                    <span>
                      {revisionConflict.refreshStatus === 'failed'
                        ? t('settings:dalton.teamCatalog.revisionConflictRefreshFailed', {
                            latestRevision: revisionConflict.requiredRevision,
                          })
                        : t('settings:dalton.teamCatalog.revisionConflictSyncing', {
                            latestRevision: revisionConflict.requiredRevision,
                          })}
                    </span>
                    {revisionConflict.refreshStatus === 'failed' && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        onClick={() =>
                          void beginConflictRecovery(
                            revisionConflict.requiredRevision,
                            revisionConflict.operation,
                          )
                        }
                        data-testid="edit-sheet-conflict-refresh-btn"
                      >
                        {t('settings:dalton.teamCatalog.revisionConflictRefreshAction')}
                      </Button>
                    )}
                  </div>
                )}
                {conflictCatalogSynced && !isVariantOnly && latestModel && (
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
                {conflictCatalogSynced && isVariantOnly && latestVariant && (
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
                {conflictCatalogSynced && conflictedFields.length > 0 && (
                  <div
                    className="rounded border px-2.5 py-1.5 text-[11px] space-y-1"
                    style={{
                      borderColor: 'var(--color-border-muted)',
                      backgroundColor: 'var(--color-bg-card)',
                    }}
                    data-testid="edit-sheet-conflict-fields"
                  >
                    <div className="font-medium">
                      {t('settings:dalton.teamCatalog.revisionConflictFieldsHeading')}
                    </div>
                    <ul className="space-y-1">
                      {conflictedFields.map((field) => (
                        <li
                          key={field.key}
                          className="font-mono break-words"
                          data-testid={`edit-sheet-conflict-field-${field.key}`}
                        >
                          <span className="font-semibold">{field.label}:</span>{' '}
                          <span>
                            {t('settings:dalton.teamCatalog.revisionConflictFieldLatest', {
                              value: field.latestValue,
                            })}
                          </span>
                          {field.draftValue !== undefined && (
                            <span>
                              {' · '}
                              {t('settings:dalton.teamCatalog.revisionConflictFieldDraft', {
                                value: field.draftValue,
                              })}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <Button
                    type="button"
                    size="sm"
                    disabled={busy || !conflictCatalogSynced}
                    onClick={() => {
                      if (!conflictCatalogSynced) return;
                      const nextRev = resolvedLatestRevision;
                      const op = revisionConflict.operation;
                      const wasConfirmedLastCurated = Boolean(revisionConflict.confirmedLastCurated);
                      setAcknowledgedRevision(nextRev);
                      setRevisionConflict(null);
                      if (op === 'restore_default') {
                        void handleRestoreDefault(nextRev);
                      } else if (op === 'save_variant') {
                        void handleSaveVariantOnly(nextRev);
                      } else {
                        void handleSaveModel({
                          overrideExpectedRevision: nextRev,
                          confirmedLastCurated: wasConfirmedLastCurated,
                        });
                      }
                    }}
                    data-testid="edit-sheet-conflict-resubmit-btn"
                  >
                    {revisionConflict.operation === 'restore_default'
                      ? t('settings:dalton.teamCatalog.revisionConflictResubmitRestoreDefault', {
                          latestRevision: resolvedLatestRevision,
                        })
                      : t('settings:dalton.teamCatalog.revisionConflictResubmit', {
                          latestRevision: resolvedLatestRevision,
                        })}
                  </Button>
                  {(latestModel || latestVariant) && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={busy || !conflictCatalogSynced}
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
                          setConfirmLastCurated(null);
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
                          setPriceEstimated(next === 'estimated');
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
                          onChange={(e) => setCashInput(e.target.value)}
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
                          onChange={(e) => setCashCachedInput(e.target.value)}
                          className={inputCls}
                          data-testid="edit-model-cash-cached"
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
                          onChange={(e) => setCashOutput(e.target.value)}
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
                          onChange={(e) => setCashCache5m(e.target.value)}
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
                          onChange={(e) => setCashCache1h(e.target.value)}
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
                            data-testid="edit-model-notional-cached"
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
                        onChange={(e) => setPriceVerifiedAt(e.target.value)}
                        placeholder="2026-11-25"
                        className={inputCls}
                        data-testid="edit-model-price-verified-at"
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="price-source-url" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.priceSourceUrlLabel')}
                      </label>
                      <Input
                        id="price-source-url"
                        value={priceSourceUrl}
                        onChange={(e) => setPriceSourceUrl(e.target.value)}
                        placeholder="https://..."
                        className={inputCls}
                        data-testid="edit-model-price-source-url"
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor="price-note" className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                        {t('settings:dalton.teamCatalog.priceNoteLabel')}
                      </label>
                      <Input
                        id="price-note"
                        value={priceNote}
                        onChange={(e) => setPriceNote(e.target.value)}
                        className={inputCls}
                        data-testid="edit-model-price-note"
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
                                data-testid={`reasoning-effort-chip-${eff}`}
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
                            data-testid="edit-model-reasoning-default-select"
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
                        setConfirmLastCurated(null);
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
                    onClick={() => setConfirmLastCurated(null)}
                  >
                    {t('common.cancel')}
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    disabled={busy || conflictSyncPending}
                    onClick={() =>
                      void handleSaveModel({
                        overrideExpectedRevision: confirmLastCurated.expectedRevision,
                        confirmedLastCurated: true,
                      })
                    }
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
                    disabled={busy || conflictSyncPending}
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
                disabled={busy || conflictSyncPending}
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
              disabled={busy || conflictSyncPending}
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
