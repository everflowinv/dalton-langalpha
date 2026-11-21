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
import {
  DALTON_BACKGROUND_MODEL,
  DALTON_DEFAULT_MODEL,
  isDaltonAllowedModel,
  resolveDaltonSetupCandidates,
} from '@/config/daltonSetup';
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
  // DALTON-HOOK(dalton-setup): Dalton mode recommends Neon High & Hydrogen High without credits or Claude Code
  dalton?: boolean;
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
      // DALTON-HOOK(dalton-setup): prefer Neon High in Dalton deployments
      if (input.dalton) {
        if (pool.includes(DALTON_DEFAULT_MODEL)) return { primary: DALTON_DEFAULT_MODEL, primaryWhy: 'daltonRecommended' };
        if (input.systemDefault && pool.includes(input.systemDefault) && daltonAllowed(input, input.systemDefault)) {
          return { primary: input.systemDefault, primaryWhy: 'daltonRecommended' };
        }
        return strongestPick(input, pool);
      }
      return input.systemDefault && pool.includes(input.systemDefault)
        ? { primary: input.systemDefault, primaryWhy: 'deployment' } : strongestPick(input, pool);
    },
    background: (input, pool, primary) => {
      // DALTON-HOOK(dalton-setup): prefer Hydrogen High for background in Dalton deployments
      if (input.dalton) {
        if (input.slotModels.includes(DALTON_BACKGROUND_MODEL) && daltonAllowed(input, DALTON_BACKGROUND_MODEL)) {
          return { background: DALTON_BACKGROUND_MODEL, backgroundWhy: 'daltonRecommended' };
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
    // DALTON-HOOK(dalton-setup): prefer Neon High & Hydrogen High under Dalton
    primary: (input, pool) => (input.dalton && pool.includes(DALTON_DEFAULT_MODEL)
      ? { primary: DALTON_DEFAULT_MODEL, primaryWhy: 'daltonRecommended' }
      : strongestPick(input, pool)),
    background: (input, pool, primary) => (input.dalton
      && input.slotModels.includes(DALTON_BACKGROUND_MODEL)
      && daltonAllowed(input, DALTON_BACKGROUND_MODEL)
      ? { background: DALTON_BACKGROUND_MODEL, backgroundWhy: 'daltonRecommended' }
      : fastestOtherPick(input, pool, primary)),
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
  pool = [...new Set(pool)];

  // DALTON-HOOK(dalton-setup): Dalton /setup/default shows the fixed 5 curated candidates in order when available
  const curated = input.dalton ? resolveDaltonSetupCandidates(pool) : [];
  const primaryPool = curated.length > 0 ? curated : pool;

  const recommended = recommend(input, primaryPool, pool, effectiveAnswer);
  // Connecting saved the model it starts on, which is the recommended one; a
  // default saved on an earlier run of setup is kept.
  const saved = input.models !== 'langalpha_models' && input.preferred && primaryPool.includes(input.preferred)
    ? input.preferred : null;
  const initial = saved ?? recommended.primary;
  if (curated.length > 0) {
    return { list: curated, initial, recommended };
  }
  const rest = pool.filter((m) => m !== initial && m !== recommended.primary);
  const head = [initial, recommended.primary].filter((m, i, all): m is string => !!m && all.indexOf(m) === i);
  return { list: [...head, ...rest].slice(0, DEFAULT_CHOICES_SHOWN), initial, recommended };
}
