/**
 * Reading and writing the model-configuration bag.
 *
 * Model configuration has its own `model_preference` column. The legacy
 * `other_preference` is still read underneath so a tab open across the deploy,
 * or a rollback to before that column, keeps resolving; the server does the
 * same on its side. Drop the fallback with the server's.
 */

// DALTON-HOOK(settings-section): zod for the Dalton model_preference keys below
import * as z from 'zod/mini';
import type { CustomModelEntry } from '@/components/model/types';
import type { ModelProfile } from './modelTuning';

export interface PreferencesLike {
  other_preference?: Record<string, unknown> | null;
  model_preference?: Record<string, unknown> | null;
}

/**
 * What a change of the default model does to existing threads: `ask` puts the
 * question to the user each time, the other two answer it in advance.
 */
export type DefaultModelScope = 'ask' | 'new_threads' | 'existing_threads';

/** The answer one write carries (`apply_default_to`), never stored itself. */
export type ApplyDefaultTo = Exclude<DefaultModelScope, 'ask'>;

/** What the Background model runs with no Background default saved: the primary default, or the
 *  deployment's Background model (Auto). Absent reads as `primary`. */
export type BackgroundFollows = 'primary' | 'deployment';

/** A sub-provider the user defined, as stored. */
export interface CustomProviderEntry {
  name: string;
  parent_provider?: string;
  use_response_api?: boolean;
}

/**
 * The stored model bag: catalog, routing, and the account-wide tuning a model
 * inherits when its own profile is silent.
 *
 * Extends `ModelProfile` because the tuning fields are the same settings
 * at the account layer that a profile overrides per model.
 */
export interface ModelPreferences extends ModelProfile {
  preferred_model?: string | null;
  preferred_background_model?: string | null;
  compaction_model?: string | null;
  summarization_model?: string | null;
  fetch_model?: string | null;
  fallback_models?: string[] | null;
  custom_models?: CustomModelEntry[] | null;
  custom_providers?: CustomProviderEntry[] | null;
  // A per-key `null` is how one model's overrides are dropped, the same delete
  // the server applies (`profiles[<model>]: None`), so the value side is
  // nullable as well as the whole map.
  profiles?: Record<string, ModelProfile | null> | null;
  /** Absent reads as `ask`. */
  default_model_scope?: DefaultModelScope | null;
  background_follows?: BackgroundFollows | null;
  // DALTON-HOOK(settings-section): Dalton model settings stored in model_preference
  dalton_audit_model?: string | null;
  dalton_language_model?: string | null;
  dalton_audit_fallback_models?: string[] | null;
  dalton_audit_timeout_s?: number | null;
  dalton_assist_model?: string | null;
  dalton_assist_fallback_models?: string[] | null;
  dalton_reviewer_models?: string[] | null;
  dalton_extract_model?: string | null;
  dalton_notify_enabled?: boolean | null;
  dalton_notify_targets?: string[] | null;
  dalton_notify_overrides?: Record<string, boolean> | null;
}

// DALTON-HOOK(settings-section): Zod boundary schema for Dalton model_preference keys
const daltonStrOrNull = z.catch(z.nullable(z.optional(z.string())), undefined);
const daltonStrListOrNull = z.catch(
  z.nullable(z.optional(z.array(z.string()))),
  undefined,
);
const daltonBoolOrNull = z.catch(z.nullable(z.optional(z.boolean())), undefined);
const daltonBoolMapOrNull = z.catch(
  z.nullable(z.optional(z.record(z.string(), z.boolean()))),
  undefined,
);
const daltonTimeoutOrNull = z.catch(
  z.nullable(
    z.optional(
      z.int().check(z.minimum(60), z.maximum(1800)),
    ),
  ),
  undefined,
);

export const DaltonModelPrefsSchema = z.object({
  dalton_audit_model: daltonStrOrNull,
  dalton_language_model: daltonStrOrNull,
  dalton_audit_fallback_models: daltonStrListOrNull,
  dalton_audit_timeout_s: daltonTimeoutOrNull,
  dalton_assist_model: daltonStrOrNull,
  dalton_assist_fallback_models: daltonStrListOrNull,
  dalton_reviewer_models: daltonStrListOrNull,
  dalton_extract_model: daltonStrOrNull,
  dalton_notify_enabled: daltonBoolOrNull,
  dalton_notify_targets: daltonStrListOrNull,
  dalton_notify_overrides: daltonBoolMapOrNull,
});

/**
 * The keys migration 034 moved out of `other_preference`, and so the only ones
 * that may still be found there on a row written before the move.
 *
 * The twin of `MOVED_MODEL_KEYS` in `src/llms/preferences.py`. Narrower than
 * `MODEL_PREF_KEYS` below on purpose: that one routes a write, this one names
 * what has a pre-move copy at all.
 */
const MOVED_KEYS: readonly (keyof ModelPreferences)[] = [
  'preferred_model',
  'preferred_background_model',
  'compaction_model',
  'summarization_model',
  'fetch_model',
  'fallback_models',
  'custom_models',
  'custom_providers',
  'compaction_profile',
  'reasoning_effort',
  'fast_mode',
];

export const MOVED_MODEL_KEYS: ReadonlySet<string> = new Set<string>(MOVED_KEYS);

/**
 * Which column each preference key is written to.
 *
 * One table rather than a comment repeated at every write site: a key put in
 * the wrong column still reads back today, because the reader merges both, and
 * only vanishes the day the legacy fallback above is dropped. `starred_models`,
 * `search_provider` and `search_depth` are the model-adjacent keys that stay in
 * `other_preference` by design, so they are absent here on purpose.
 */
export const MODEL_PREF_KEYS: ReadonlySet<string> = new Set<string>([
  ...MOVED_KEYS,
  // None ever lived in `other_preference`: `profiles`, `default_model_scope`,
  // `background_follows` and `native_compaction` are new, and `prompt_guidance`
  // appears there only if 034's downgrade puts it back. They route to the model
  // column without having a copy to read underneath.
  'profiles',
  'prompt_guidance',
  'default_model_scope',
  'background_follows',
  'native_compaction',
  // DALTON-HOOK(settings-section): route Dalton model settings to model_preference
  'dalton_audit_model',
  'dalton_language_model',
  'dalton_audit_fallback_models',
  'dalton_audit_timeout_s',
  'dalton_assist_model',
  'dalton_assist_fallback_models',
  'dalton_reviewer_models',
  'dalton_extract_model',
  'dalton_notify_enabled',
  'dalton_notify_targets',
  'dalton_notify_overrides',
]);

/**
 * The model bag, new column over legacy. Never null, so call sites can index it.
 *
 * The twin of `get_model_preference` in
 * `src/server/services/llm/user_models.py`: the legacy column is read
 * underneath, but only for the keys that moved out of it. Spreading the two
 * bags whole pulled in keys other services write to that column. Clearing a
 * key clears both columns at the write, so absence in the new column is the
 * only thing that reaches the copy underneath. The server's bag also carries
 * the three keys that never moved, because its resolver reads them; here every
 * reader of those takes them off `other_preference` itself.
 */
export function modelPrefs(prefs: unknown): ModelPreferences {
  const p = (prefs ?? {}) as PreferencesLike;
  const current = p.model_preference ?? {};
  const merged: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(p.other_preference ?? {})) {
    if (MOVED_MODEL_KEYS.has(key) && !(key in current)) merged[key] = value;
  }
  for (const [key, value] of Object.entries(current)) {
    if (value !== null && value !== undefined) merged[key] = value;
  }
  // DALTON-HOOK(settings-section): sanitize Dalton model preference keys at the boundary
  const daltonParsed = DaltonModelPrefsSchema.safeParse(merged);
  if (daltonParsed.success) {
    for (const [k, v] of Object.entries(daltonParsed.data)) {
      if (k in merged) {
        if (v === undefined || v === null) {
          delete merged[k];
        } else {
          merged[k] = v;
        }
      }
    }
  }
  return merged as ModelPreferences;
}

/**
 * One model's tuning overrides (`model_preference.profiles[<model>]`).
 *
 * Only ever in the new column: `profiles` never lived in the legacy one, so
 * there is nothing to fall back to. An absent or malformed entry reads as no
 * overrides.
 */
export function modelProfile(prefs: unknown, model: string | null | undefined): ModelProfile {
  if (!model) return {};
  const profiles = (prefs as PreferencesLike | null)?.model_preference?.profiles;
  if (typeof profiles !== 'object' || profiles === null) return {};
  const entry = (profiles as Record<string, unknown>)[model];
  return typeof entry === 'object' && entry !== null ? (entry as ModelProfile) : {};
}

/** A flat patch of preference keys, whichever column they end up in. */
export type PreferencePatch = ModelPreferences & Record<string, unknown>;

/**
 * Route a flat patch to the column each key belongs to.
 *
 * Callers write what they mean (`{ preferred_model, starred_models }`) and this
 * decides where it lands, so the split is stated once instead of hand-rolled
 * differently at each write site. A column with nothing in it is omitted rather
 * than sent empty: the server merges what it receives, and an empty object is
 * still a write.
 */
export function splitPreferenceWrite(patch: PreferencePatch): {
  model_preference?: Record<string, unknown>;
  other_preference?: Record<string, unknown>;
} {
  const model: Record<string, unknown> = {};
  const other: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(patch)) {
    (MODEL_PREF_KEYS.has(key) ? model : other)[key] = value;
  }
  return {
    ...(Object.keys(model).length > 0 ? { model_preference: model } : {}),
    ...(Object.keys(other).length > 0 ? { other_preference: other } : {}),
  };
}

/** The two model slots: Default, which every composer and thread starts on,
 *  and Background, the second, cheaper one. Anything else is read as Default. */
export type ModelSlot = 'default' | 'background';

/** The deployment's defaults, as the models endpoint reports them. A
 *  deployment that names no Background model reports it as "". */
export interface DeploymentModelDefaults {
  default_model?: string;
  background_model?: string;
}

/** The preference key that holds the default for `slot`. The one place a
 *  slot maps to its stored field. */
export function defaultModelKey(slot: ModelSlot | undefined): 'preferred_model' | 'preferred_background_model' {
  return slot === 'background' ? 'preferred_background_model' : 'preferred_model';
}

/**
 * The model a thread with none of its own runs on in `slot`.
 *
 * The same rule the server runs a turn on and uses to decide which threads sit
 * on the old default when the default moves, so "your default" names one model
 * on both sides. Background inherits the Default choice unless the user chose
 * Auto for it, then the deployment's background model, and a deployment
 * without one runs it on its default model.
 */
export function slotDefaultModel(
  prefs: ModelPreferences,
  deployment: DeploymentModelDefaults | null | undefined,
  slot: ModelSlot | undefined,
): string | null {
  if (slot === 'background') {
    const primary = prefs.background_follows === 'deployment' ? null : prefs.preferred_model;
    return prefs.preferred_background_model || primary
      || deployment?.background_model || deployment?.default_model || null;
  }
  return prefs.preferred_model || deployment?.default_model || null;
}

/** The Background default control's value for Auto. Not a model name, which an
 *  option value could not otherwise be told apart from. */
export const BACKGROUND_AUTO = '__auto__';

/** What the Background default control shows: the saved model, Auto, or '' for
 *  the primary. Auto beside an unset primary runs what '' does, so it shows
 *  as ''. */
export function backgroundDefaultChoice(prefs: ModelPreferences): string {
  if (prefs.preferred_background_model) return prefs.preferred_background_model;
  return prefs.background_follows === 'deployment' && prefs.preferred_model ? BACKGROUND_AUTO : '';
}

/** The threads a write moved off the old default; undefined from a server
 *  that does not count them. */
export function threadsMoved(result: unknown): number | undefined {
  const count = (result as { threads_reassigned?: unknown } | undefined)?.threads_reassigned;
  return typeof count === 'number' ? count : undefined;
}

/** The saved answer to "do existing threads follow a new default", `ask` when
 *  none is saved or the stored value is not one this client knows. */
export function readDefaultModelScope(prefs: unknown): DefaultModelScope {
  const scope = modelPrefs(prefs).default_model_scope;
  return scope === 'new_threads' || scope === 'existing_threads' ? scope : 'ask';
}

// DALTON-HOOK(settings-section): resolve per-task notify state from backend effective value or unified override precedence
export function resolveDaltonAutomationNotifyState(
  automation: {
    name: string;
    dalton_notify_category?: string | null;
    dalton_notify_effective?: boolean | null;
    dalton_notify_source?: string | null;
  },
  overrides?: Record<string, boolean> | null,
): { enabled: boolean; source: string | null } {
  const category = automation.dalton_notify_category;
  if (!category) return { enabled: false, source: null };
  if (overrides) {
    const trimmedName = automation.name.trim();
    const candidates: Array<[string, string]> = [
      [trimmedName, 'task'],
      [trimmedName.toLowerCase(), 'task'],
      [`category:${category}`, 'category'],
      [category, 'category'],
    ];
    for (const [key, src] of candidates) {
      if (key && typeof overrides[key] === 'boolean') {
        return { enabled: overrides[key], source: src };
      }
    }
  }
  if (typeof automation.dalton_notify_effective === 'boolean') {
    return {
      enabled: automation.dalton_notify_effective,
      source: automation.dalton_notify_source ?? 'default',
    };
  }
  return { enabled: true, source: 'default' };
}

export function isDaltonAutomationNotifyEnabled(
  automation: {
    name: string;
    dalton_notify_category?: string | null;
    dalton_notify_effective?: boolean | null;
    dalton_notify_source?: string | null;
  },
  overrides?: Record<string, boolean> | null,
): boolean {
  return resolveDaltonAutomationNotifyState(automation, overrides).enabled;
}
