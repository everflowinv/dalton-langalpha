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
  return meta?.sdk === 'claude_code' || meta?.provider === 'claude_code';
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

/** Remove Claude Code and credits-only models from provider groups under Dalton. */
export function filterDaltonProviderModels(
  groups: Record<string, ProviderModelsData>,
  metadata: Record<string, DaltonModelMeta | undefined>,
  payers: Record<string, Payer>,
  dalton: boolean,
): Record<string, ProviderModelsData> {
  if (!dalton) return groups;
  const out: Record<string, ProviderModelsData> = {};
  for (const [provider, data] of Object.entries(groups)) {
    if (provider === 'claude_code' || provider === 'claude-code') continue;
    const models = (data.models ?? []).filter(
      (m) => !isClaudeCodeMeta(metadata[m]) && payers[m] !== 'credits',
    );
    if (models.length > 0) {
      out[provider] = { ...data, models };
    }
  }
  return out;
}
