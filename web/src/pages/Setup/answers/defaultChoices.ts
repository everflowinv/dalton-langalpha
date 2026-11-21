/**
 * The models the default step offers for the answers, and the setup it
 * recommends from them: a conversation model and a Background one, each with
 * the reason it was picked, read from the manifest's ratings (or, for the
 * deployment's models, the deployment's own picks, and for Claude Code, its
 * newest Sonnet, which suits any Claude plan).
 */
import { CLAUDE_CODE, claudeCodeStartModel } from '@/lib/claudeCode';
import type { Payer } from '@/lib/modelPayer';
import { listedFor } from '@/lib/connectionModels';
import type { ConnectionOf } from '@/lib/providerCatalog';
import type { CustomModelEntry } from '@/components/model/types';
// DALTON-HOOK(dalton-setup): Dalton recommended models, curated setup candidates, and Claude Code / credits filtering
// DALTON-HOOK(antigravity-series): series-level collapsing & recommendation resolution for onboarding
import {
  DALTON_BACKGROUND_MODEL,
  DALTON_BACKGROUND_SERIES,
  DALTON_DEFAULT_MODEL,
  DALTON_DEFAULT_SERIES,
  isDaltonAllowedModel,
  resolveDaltonSeriesDefaultModel,
  resolveDaltonSetupCandidates,
} from '@/config/daltonSetup';
import {
  inspectSavedAntigravityModel,
  sameAntigravitySeries,
  type AntigravitySeriesCatalog,
  type SelectableAntigravitySeries,
} from '@/lib/antigravitySeries';
import { resolveEffort } from '@/lib/modelTuning';
import type { ModelsAnswer } from './setupSteps';

/** How many models the step lists; the rest are in Settings. */
export const DEFAULT_CHOICES_SHOWN = 5;

export type RatedMetadata = Record<string, { sdk?: string; model_id?: string; provider?: string; intelligence?: number; speed?: number } | undefined>;

export interface DefaultChoicesInput {
  models: ModelsAnswer;
  /** The models the user can run, by provider. */
  offered: Record<string, { models?: string[] }>;
  /** The models that can run Background work; a conversation-only one (Claude Code) is not one. */
  slotModels: readonly string[];
  payers: Record<string, Payer>;
  /** Providers connected with the user's own sign-in or key. */
  ownProviders: readonly string[];
  metadata: RatedMetadata;
  customModels: readonly CustomModelEntry[];
  connectionOf: ConnectionOf;
  systemDefault: string | null;
  systemBackground: string | null;
  /** The saved default, which connecting in setup set. */
  preferred: string | null;
  // DALTON-HOOK(dalton-setup): Dalton mode recommends Neon & Hydrogen without credits or Claude Code
  dalton?: boolean;
  // DALTON-HOOK(antigravity-series): series catalog & selectable series for collapsing onboarding candidates
  seriesCatalog?: AntigravitySeriesCatalog;
  selectableSeries?: readonly SelectableAntigravitySeries[];
}

export type PrimaryWhy = 'strongest' | 'strongestFastest' | 'deployment' | 'daltonRecommended' | 'claudeCode';
export type BackgroundWhy = 'fastest' | 'only' | 'deployment' | 'daltonRecommended' | 'claudeCode' | 'claudeCodeCredits';

export interface RecommendedSetup {
  primary: string | null;
  primaryWhy: PrimaryWhy | null;
  /** Null leaves Background unset, which runs the deployment's Background model. */
  background: string | null;
  backgroundWhy: BackgroundWhy | null;
}

export interface DefaultChoices {
  list: string[];
  initial: string | null;
  recommended: RecommendedSetup;
}

const rating = (value: number | undefined) => value ?? -1;

function best(pool: readonly string[], first: (m: string) => number, second: (m: string) => number): { model: string; tied: boolean } | null {
  if (pool.length === 0) return null;
  const top = Math.max(...pool.map(first));
  if (top < 0) return null;
  const tops = pool.filter((m) => first(m) === top);
  const model = tops.reduce((a, b) => (second(b) > second(a) ? b : a));
  // Tied only where the second rating is what told them apart.
  return { model, tied: new Set(tops.map(second)).size > 1 };
}

/** The highest intelligence rating in \`pool\`, the faster on a tie; null where none is rated. */
export function strongestModel(pool: readonly string[], metadata: RatedMetadata): { model: string; tied: boolean } | null {
  return best(pool, (m) => rating(metadata[m]?.intelligence), (m) => rating(metadata[m]?.speed));
}

type PrimaryPick = Pick<RecommendedSetup, 'primary' | 'primaryWhy'>;
type BackgroundPick = Pick<RecommendedSetup, 'background' | 'backgroundWhy'>;

function strongestPick(input: DefaultChoicesInput, pool: string[]): PrimaryPick {
  const strongest = strongestModel(pool, input.metadata);
  return {
    primary: strongest?.model ?? pool[0] ?? null,
    primaryWhy: strongest ? (strongest.tied ? 'strongestFastest' : 'strongest') : null,
  };
}

/** The fastest model Background can run other than the primary, else the
 *  primary itself where it is the only one. */
function fastestOtherPick(input: DefaultChoicesInput, pool: string[], primary: string | null): BackgroundPick {
  const runnable = pool.filter((m) => input.slotModels.includes(m));
  const others = runnable.filter((m) => m !== primary);
  if (others.length === 0) {
    return primary && runnable.includes(primary) ? { background: primary, backgroundWhy: 'only' } : { background: null, backgroundWhy: null };
  }
  const fastest = best(others, (m) => rating(input.metadata[m]?.speed), (m) => rating(input.metadata[m]?.intelligence));
  return { background: fastest?.model ?? others[0], backgroundWhy: fastest ? 'fastest' : null };
}

/** Served by the deployment, as LangAlpha's models are: never a key or plan the user connected. */
const platformServed = (input: DefaultChoicesInput, model: string) => input.payers[model] === 'credits' || input.payers[model] === 'server_key';

const daltonAllowed = (input: DefaultChoicesInput, model: string) =>
  isDaltonAllowedModel(input.metadata[model], input.payers[model]);

/** How each answer picks its primary and its Background. */
const RULES: Record<ModelsAnswer, {
  primary: (input: DefaultChoicesInput, pool: string[]) => PrimaryPick;
  background: (input: DefaultChoicesInput, pool: string[], primary: string | null) => BackgroundPick;
}> = {
  langalpha_models: {
    primary: (input, pool) => {
      // DALTON-HOOK(dalton-setup): prefer Neon series default in Dalton deployments
      // DALTON-HOOK(antigravity-series): resolve Neon default variant via antigravity_series
      if (input.dalton) {
        const daltonPrimary = resolveDaltonSeriesDefaultModel(DALTON_DEFAULT_SERIES, pool, {
          seriesCatalog: input.seriesCatalog,
          selectableSeries: input.selectableSeries,
          fallbackModel: DALTON_DEFAULT_MODEL,
        });
        if (daltonPrimary) return { primary: daltonPrimary, primaryWhy: 'daltonRecommended' };
        if (input.systemDefault && pool.includes(input.systemDefault) && daltonAllowed(input, input.systemDefault)) {
          return { primary: input.systemDefault, primaryWhy: 'daltonRecommended' };
        }
        return strongestPick(input, pool);
      }
      return input.systemDefault && pool.includes(input.systemDefault)
        ? { primary: input.systemDefault, primaryWhy: 'deployment' } : strongestPick(input, pool);
    },
    background: (input, pool, primary) => {
      // DALTON-HOOK(dalton-setup): prefer Hydrogen low variant for background in Dalton deployments
      // DALTON-HOOK(antigravity-series): resolve Hydrogen -low variant via antigravity_series
      if (input.dalton) {
        const allowedSlots = input.slotModels.filter((m) => daltonAllowed(input, m));
        const daltonBg = resolveDaltonSeriesDefaultModel(DALTON_BACKGROUND_SERIES, allowedSlots, {
          seriesCatalog: input.seriesCatalog,
          selectableSeries: input.selectableSeries,
          fallbackModel: DALTON_BACKGROUND_MODEL,
          preferLowEffort: true,
        });
        if (daltonBg) {
          return { background: daltonBg, backgroundWhy: 'daltonRecommended' };
        }
        if (input.systemBackground && input.slotModels.includes(input.systemBackground) && daltonAllowed(input, input.systemBackground)) {
          return { background: input.systemBackground, backgroundWhy: 'daltonRecommended' };
        }
        return fastestOtherPick(input, pool, primary);
      }
      return input.systemBackground && input.slotModels.includes(input.systemBackground)
        && platformServed(input, input.systemBackground)
        ? { background: input.systemBackground, backgroundWhy: 'deployment' } : fastestOtherPick(input, pool, primary);
    },
  },
  // Claude Code runs conversations only, so Background stays the deployment's.
  claude_code: {
    primary: (input, pool) => {
      const primary = claudeCodeStartModel(pool, input.metadata, null);
      return { primary, primaryWhy: primary ? 'claudeCode' : null };
    },
    background: (input) => ({
      background: null,
      backgroundWhy: !input.systemBackground ? null
        : input.payers[input.systemBackground] === 'credits' ? 'claudeCodeCredits' : 'claudeCode',
    }),
  },
  subscription_or_key: {
    // DALTON-HOOK(dalton-setup): prefer Neon & Hydrogen series defaults under Dalton
    // DALTON-HOOK(antigravity-series): resolve Neon & Hydrogen via antigravity_series
    primary: (input, pool) => {
      const daltonPrimary = input.dalton
        ? resolveDaltonSeriesDefaultModel(DALTON_DEFAULT_SERIES, pool, {
            seriesCatalog: input.seriesCatalog,
            selectableSeries: input.selectableSeries,
            fallbackModel: DALTON_DEFAULT_MODEL,
          })
        : null;
      return daltonPrimary
        ? { primary: daltonPrimary, primaryWhy: 'daltonRecommended' }
        : strongestPick(input, pool);
    },
    background: (input, pool, primary) => {
      const allowedSlots = input.slotModels.filter((m) => daltonAllowed(input, m));
      const daltonBg = input.dalton
        ? resolveDaltonSeriesDefaultModel(DALTON_BACKGROUND_SERIES, allowedSlots, {
            seriesCatalog: input.seriesCatalog,
            selectableSeries: input.selectableSeries,
            fallbackModel: DALTON_BACKGROUND_MODEL,
            preferLowEffort: true,
          })
        : null;
      return daltonBg
        ? { background: daltonBg, backgroundWhy: 'daltonRecommended' }
        : fastestOtherPick(input, pool, primary);
    },
  },
};

function recommend(
  input: DefaultChoicesInput,
  primaryPool: string[],
  backgroundPool: string[],
  effectiveAnswer: ModelsAnswer,
): RecommendedSetup {
  const rule = RULES[effectiveAnswer];
  const primary = rule.primary(input, primaryPool);
  return { ...primary, ...rule.background(input, backgroundPool, primary.primary) };
}

export function defaultChoices(input: DefaultChoicesInput): DefaultChoices {
  const of = (provider: string) => listedFor(provider, input.offered, input.metadata, input.customModels, input.connectionOf);
  const effectiveAnswer: ModelsAnswer = input.dalton && input.models === 'claude_code' ? 'langalpha_models' : input.models;
  let pool: string[];
  if (effectiveAnswer === 'claude_code') pool = of(CLAUDE_CODE);
  else if (effectiveAnswer === 'subscription_or_key') pool = input.ownProviders.flatMap(of);
  else {
    pool = Object.values(input.offered)
      .flatMap((p) => p.models ?? [])
      // DALTON-HOOK(dalton-setup): Dalton team credentials offer OAuth/provider models, excluding Claude Code & credits
      .filter((m) => (input.dalton ? daltonAllowed(input, m) : platformServed(input, m)));
  }
  if (input.dalton) {
    pool = pool.filter((m) => daltonAllowed(input, m));
  }
  const rawPool = [...new Set(pool)];
  const seriesOpts = {
    seriesCatalog: input.seriesCatalog,
    rawMetadata: input.metadata,
    customModels: input.customModels,
  };
  // DALTON-HOOK(antigravity-series): collapse Antigravity variants in candidate pool to 1 representative key per series
  const seriesPoolBuckets = new Map<string, { efforts: string[]; variants: Map<string, string>; defaultEffort: string }>();
  for (const m of rawPool) {
    const hit = inspectSavedAntigravityModel(m, seriesOpts);
    if (!hit) continue;
    let bucket = seriesPoolBuckets.get(hit.series.series_id);
    if (!bucket) {
      bucket = { efforts: [], variants: new Map(), defaultEffort: hit.series.default_effort };
      seriesPoolBuckets.set(hit.series.series_id, bucket);
    }
    bucket.efforts.push(hit.effort);
    bucket.variants.set(hit.effort, m);
  }
  const seenSeries = new Set<string>();
  const collapsedPool: string[] = [];
  for (const m of rawPool) {
    const hit = inspectSavedAntigravityModel(m, seriesOpts);
    if (!hit) {
      collapsedPool.push(m);
      continue;
    }
    const sid = hit.series.series_id;
    if (seenSeries.has(sid)) continue;
    seenSeries.add(sid);
    const sel = input.selectableSeries?.find((s) => s.seriesId === sid);
    if (sel && rawPool.includes(sel.defaultVariantKey)) {
      collapsedPool.push(sel.defaultVariantKey);
      continue;
    }
    const bucket = seriesPoolBuckets.get(sid)!;
    const chosenEffort = resolveEffort(bucket.defaultEffort, bucket.efforts) ?? bucket.efforts[0];
    const rep = (chosenEffort ? bucket.variants.get(chosenEffort) : undefined) ?? m;
    collapsedPool.push(rep);
  }
  pool = collapsedPool;

  // DALTON-HOOK(dalton-setup): Dalton /setup/default shows the fixed 5 curated candidates in order when available
  const curated = input.dalton ? resolveDaltonSetupCandidates(rawPool) : [];
  const primaryPool = curated.length > 0 ? curated : pool;

  // DALTON-HOOK(antigravity-series): preserve saved suffixed variant while keeping 1 card per series
  const recommended = recommend(input, primaryPool, pool, effectiveAnswer);
  // Connecting saved the model it starts on, which is the recommended one; a
  // default saved on an earlier run of setup is kept (including legacy suffixed
  // Antigravity variants belonging to an available series).
  const sameAs = (a: string, b: string | null) => a === b || sameAntigravitySeries(a, b, seriesOpts);
  const canKeepPreferred = input.models !== 'langalpha_models' || !!input.dalton;
  if (curated.length > 0) {
    const saved = canKeepPreferred && input.preferred && rawPool.includes(input.preferred)
      && primaryPool.some((m) => sameAs(m, input.preferred))
      ? input.preferred : null;
    const initial = saved ?? recommended.primary;
    return { list: curated, initial, recommended };
  }
  const saved = canKeepPreferred && input.preferred && rawPool.includes(input.preferred)
    ? input.preferred : null;
  const initial = saved ?? recommended.primary;
  const rest = pool.filter((m) => !sameAs(m, initial) && !sameAs(m, recommended.primary));
  const head = [initial, recommended.primary].filter((m, i, all): m is string => (
    !!m && all.findIndex((other) => sameAs(m, other)) === i
  ));
  return { list: [...head, ...rest].slice(0, DEFAULT_CHOICES_SHOWN), initial, recommended };
}
