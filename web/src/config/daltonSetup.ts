import { CLAUDE_CODE, isClaudeCodeModel } from '@/lib/claudeCode';
import type { Payer } from '@/lib/modelPayer';
import type { ProviderModelsData } from '@/components/model/types';

/** Recommended primary default model for Dalton deployments. */
export const DALTON_DEFAULT_MODEL = 'gemini-4-neon-high-oauth';

/** Recommended background model for Dalton deployments. */
export const DALTON_BACKGROUND_MODEL = 'gemini-4-hydrogen-high-oauth';

export interface DaltonModelMeta {
  sdk?: string;
  provider?: string;
}

/** True when model metadata describes a Claude Code model (unsupported in Dalton). */
export function isClaudeCodeMeta(meta: DaltonModelMeta | undefined): boolean {
  return isClaudeCodeModel(meta) || meta?.provider === CLAUDE_CODE;
}

/** True when a model is permitted in Dalton (neither Claude Code nor credits-billed nor on a rejected provider). */
export function isDaltonAllowedModel(
  meta: DaltonModelMeta | undefined,
  payer: Payer | undefined,
  rejectedProviders?: ReadonlySet<string>,
): boolean {
  if (isClaudeCodeMeta(meta) || meta?.provider === 'platform' || payer === 'credits') return false;
  if (rejectedProviders?.size && meta?.provider && rejectedProviders.has(meta.provider)) return false;
  return true;
}

/** Omit `credits` payer labels under Dalton (Dalton does not bill via credits). */
export function filterDaltonPayers(
  payers: Record<string, Payer>,
  dalton: boolean,
): Record<string, Payer> {
  if (!dalton) return payers;
  const out: Record<string, Payer> = {};
  for (const [model, payer] of Object.entries(payers)) {
    if (payer !== 'credits') out[model] = payer;
  }
  return out;
}

/** Remove Claude Code, credits-only, and explicitly rejected-credential models from provider groups under Dalton. */
export function filterDaltonProviderModels(
  groups: Record<string, ProviderModelsData>,
  metadata: Record<string, DaltonModelMeta | undefined>,
  payers: Record<string, Payer>,
  dalton: boolean,
  rejectedProviders?: ReadonlySet<string>,
): Record<string, ProviderModelsData> {
  if (!dalton) return groups;
  const out: Record<string, ProviderModelsData> = {};
  for (const [provider, data] of Object.entries(groups)) {
    if (provider === CLAUDE_CODE || provider === 'platform') continue;
    if (rejectedProviders?.has(provider)) continue;
    const models = (data.models ?? []).filter(
      (m) => isDaltonAllowedModel(metadata[m], payers[m], rejectedProviders),
    );
    if (models.length > 0) {
      out[provider] = { ...data, models };
    }
  }
  return out;
}

/** True when at least one Dalton-allowed model is available in the catalog. */
export function hasAvailableDaltonModels(
  groups: Record<string, ProviderModelsData>,
  metadata: Record<string, DaltonModelMeta | undefined>,
  payers: Record<string, Payer>,
  dalton: boolean,
  rejectedProviders?: ReadonlySet<string>,
): boolean {
  const filtered = filterDaltonProviderModels(groups, metadata, payers, dalton, rejectedProviders);
  return Object.values(filtered).some((data) => (data.models?.length ?? 0) > 0);
}
