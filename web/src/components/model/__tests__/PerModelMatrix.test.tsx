import { fireEvent, render, screen } from '@testing-library/react'; // DALTON-HOOK(antigravity-series)
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi } from 'vitest';
import { PerModelMatrix } from '../PerModelMatrix';
import { queryKeys } from '@/lib/queryKeys';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';

/**
 * A tuning cell has to name what "Default" resolves to, and it reads that off
 * the preferences cache the same way the server reads the stored row. These pin
 * the three layers in order: the model's own answer, the account value that
 * outranks it, and the fail-safe when neither declares one.
 */

vi.mock('@/pages/Dashboard/utils/api', () => ({
  getPreferences: vi.fn(async () => ({})),
  updatePreferences: vi.fn(async () => ({})),
}));

const profilesCatalog = {
  aggressive: { token_threshold: 100000 },
  moderate: { token_threshold: 130000 },
  extended: { token_threshold: 200000 },
  economy: { token_threshold: 250000 },
  relaxed: { token_threshold: 300000 },
  spacious: { token_threshold: 400000 },
  expansive: { token_threshold: 500000 },
  maximal: { token_threshold: 750000 },
};

const metadata: Record<string, ModelMetadataEntry> = {
  'wide-model': { provider: 'p', compaction_profile: 'relaxed' },
  'narrow-model': { provider: 'p', compaction_profile: 'moderate' },
  'unknown-window': { provider: 'p' },
  'lean-model': { provider: 'p', prompt_guidance: 'lean' },
  'native-claude': { provider: 'p', native_compaction: 'anthropic' },
  'native-gpt': { provider: 'p', native_compaction: 'openai' },
};

/** A row only appears for a model that already carries some override, so give
 *  each one an unrelated setting and leave the cell under test inheriting. */
const PROFILES = Object.fromEntries(
  Object.keys(metadata).map((m) => [m, { reasoning_effort: 'high' }]),
);

function renderMatrix(
  account: Record<string, unknown> = {},
  over: {
    validModelNames?: Set<string>;
    profiles?: Record<string, Record<string, unknown>>;
    providerCompaction?: boolean;
  } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  queryClient.setQueryData(queryKeys.user.preferences(), {
    model_preference: { ...account, profiles: { ...PROFILES, ...over.profiles } },
  });
  queryClient.setQueryData(queryKeys.features.list(), [
    { key: 'provider_compaction', enabled: over.providerCompaction ?? true },
  ]);
  return render(
    <QueryClientProvider client={queryClient}>
      <PerModelMatrix
        metadata={metadata}
        validModelNames={over.validModelNames ?? new Set(Object.keys(metadata))}
        compactionProfiles={profilesCatalog}
      />
    </QueryClientProvider>,
  );
}

/** The placeholder option (value "") is the "what do I get if I leave this
 *  alone" surface; the other options are the override list and always present. */
const inheritedLabel = (model: string, column: string) => {
  const cell = screen.getByLabelText(`${model} ${column}`) as HTMLSelectElement;
  return [...cell.options].find((o) => o.value === '')?.textContent ?? '';
};

describe('PerModelMatrix context cell', () => {
  it('names the preset each model derives, rather than a bare Default', () => {
    renderMatrix();
    expect(inheritedLabel('wide-model', 'Context')).toBe('Default (Relaxed)');
    expect(inheritedLabel('narrow-model', 'Context')).toBe('Default (Moderate)');
  });

  it('stays a bare Default when the model declares no window', () => {
    renderMatrix();
    expect(inheritedLabel('unknown-window', 'Context')).toBe('Default');
  });

  it('lets the account default outrank what the model derives', () => {
    renderMatrix({ compaction_profile: 'aggressive' });
    expect(inheritedLabel('wide-model', 'Context')).toBe('Default (Aggressive)');
  });
});

describe('PerModelMatrix guidance cell', () => {
  it('names the level the model declares for itself', () => {
    renderMatrix();
    expect(inheritedLabel('lean-model', 'Guidance')).toBe('Default (Concise)');
  });

  it('falls back to the server fail-safe when nobody declares one', () => {
    renderMatrix();
    expect(inheritedLabel('wide-model', 'Guidance')).toBe('Default (Thorough)');
  });

  it('lets the account default outrank the model declaration', () => {
    renderMatrix({ prompt_guidance: 'lean' });
    expect(inheritedLabel('wide-model', 'Guidance')).toBe('Default (Concise)');
  });
});

describe('PerModelMatrix rows', () => {
  it('keeps an unreachable profile on screen, inert but resettable', () => {
    // Hiding it left overrides the user could neither see nor clear, and
    // nothing scrubs them: the server sweep only fires on an unresolvable
    // *selected* model, so a profile on its own never reaches it. They are
    // live again the moment the model is, which is why the row cannot just
    // disappear. Its cells have no ladder to offer, so it states that and
    // keeps only the reset.
    renderMatrix({}, { validModelNames: new Set(['wide-model']) });
    expect(screen.getByText('wide-model')).toBeTruthy();
    expect(screen.getByText('narrow-model')).toBeTruthy();
    expect(screen.getByLabelText('Remove overrides narrow-model')).toBeTruthy();
    expect(screen.queryByLabelText('narrow-model Effort')).toBeNull();
    // The reachable row still renders its controls.
    expect(screen.getByLabelText('wide-model Effort')).toBeTruthy();
  });
});

describe('PerModelMatrix provider compaction cell', () => {
  const column = 'Provider compaction';

  it('appears only for a model whose provider can compact for it', () => {
    renderMatrix();
    expect(screen.getByLabelText(`native-claude ${column}`)).toBeTruthy();
    expect(screen.getByLabelText(`native-gpt ${column}`)).toBeTruthy();
    expect(screen.queryByLabelText(`wide-model ${column}`)).toBeNull();
  });

  it('names the default: on for every provider', () => {
    renderMatrix();
    expect(inheritedLabel('native-claude', column)).toBe('Default (On)');
    expect(inheritedLabel('native-gpt', column)).toBe('Default (On)');
  });

  it('lets the account value outrank the default', () => {
    renderMatrix({ native_compaction: false });
    expect(inheritedLabel('native-gpt', column)).toBe('Default (Off)');
  });

  it('shows the per-model choice over both', () => {
    renderMatrix(
      { native_compaction: true },
      { profiles: { 'native-claude': { native_compaction: false } } },
    );
    expect((screen.getByLabelText(`native-claude ${column}`) as HTMLSelectElement).value).toBe('off');
  });

  it('is hidden while the provider_compaction feature is off', () => {
    renderMatrix({}, { providerCompaction: false });
    expect(screen.queryByText(column)).toBeNull();
    expect(screen.queryByLabelText(`native-claude ${column}`)).toBeNull();
    expect(screen.queryByText(/Claude summarizes the conversation itself/)).toBeNull();
  });

  it('states the trade-off of each provider a reachable row takes, once', () => {
    renderMatrix({}, { validModelNames: new Set(['native-claude', 'wide-model']) });
    expect(screen.getByText(/Claude summarizes the conversation itself/)).toBeTruthy();
    expect(screen.queryByText(/OpenAI compacts the conversation/)).toBeNull();
  });
});

// DALTON-HOOK(antigravity-series): verify PerModelMatrix displays Series · Effort and collapses + Add model by series
describe('PerModelMatrix Antigravity series', () => {
  it('labels existing and unreachable Antigravity rows as Series · Effort and collapses + Add model', async () => {
    const { buildSeriesCatalog } = await import('@/lib/antigravitySeries');
    const seriesCatalog = buildSeriesCatalog([
      {
        series_id: 'antigravity-oauth:gemini-4-neon',
        series_prefix: 'gemini-4-neon',
        provider: 'antigravity-oauth',
        display_name: 'Gemini 4 Neon',
        qualified_display_name: 'Gemini 4 Neon (Antigravity)',
        default_effort: 'high',
        default_model: 'gemini-4-neon-high-oauth',
        efforts: ['low', 'medium', 'high'],
        variants: {
          low: 'gemini-4-neon-low-oauth',
          medium: 'gemini-4-neon-medium-oauth',
          high: 'gemini-4-neon-high-oauth',
        },
      },
      {
        series_id: 'antigravity-oauth:gemini-4-carbon',
        series_prefix: 'gemini-4-carbon',
        provider: 'antigravity-oauth',
        display_name: 'Gemini 4 Carbon',
        qualified_display_name: 'Gemini 4 Carbon (Antigravity)',
        default_effort: 'medium',
        default_model: 'gemini-4-carbon-medium-oauth',
        efforts: ['low', 'medium', 'high'],
        variants: {
          low: 'gemini-4-carbon-low-oauth',
          medium: 'gemini-4-carbon-medium-oauth',
          high: 'gemini-4-carbon-high-oauth',
        },
      },
    ]);
    const agMetadata: Record<string, ModelMetadataEntry> = {
      'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-carbon-low-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-carbon-medium-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-carbon-high-oauth': { provider: 'antigravity-oauth' },
    };
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(queryKeys.user.preferences(), {
      model_preference: {
        profiles: { 'gemini-4-neon-medium-oauth': { prompt_guidance: 'lean' } },
      },
    });
    qc.setQueryData(queryKeys.features.list(), [{ key: 'provider_compaction', enabled: false }]);

    render(
      <QueryClientProvider client={qc}>
        <PerModelMatrix
          metadata={agMetadata}
          rawMetadata={agMetadata}
          seriesCatalog={seriesCatalog}
          validModelNames={new Set([
            'gemini-4-carbon-low-oauth',
            'gemini-4-carbon-medium-oauth',
            'gemini-4-carbon-high-oauth',
          ])}
          compactionProfiles={profilesCatalog}
        />
      </QueryClientProvider>,
    );

    // Unreachable saved neon-medium row still displays as "Gemini 4 Neon · Medium"
    expect(screen.getByText('Gemini 4 Neon · Medium')).toBeTruthy();

    // "+ Add a model" collapses Carbon's 3 variants into 1 series option and lets the user choose an explicit effort before adding
    const addSelect = screen.getByLabelText('Add a model') as HTMLSelectElement;
    expect([...addSelect.options].map((o) => ({ text: o.textContent, value: o.value }))).toEqual([
      { text: '+ Add a model', value: '' },
      { text: 'Gemini 4 Carbon', value: 'series:antigravity-oauth:gemini-4-carbon' },
    ]);

    // DALTON-HOOK(antigravity-series): B4 — selecting a series shows its effort picker and adds the explicitly chosen tier
    fireEvent.change(addSelect, { target: { value: 'series:antigravity-oauth:gemini-4-carbon' } });
    const effortSelect = screen.getByLabelText('Gemini 4 Carbon Effort') as HTMLSelectElement;
    expect(effortSelect.value).toBe('medium');
    expect([...effortSelect.options].map((o) => ({ text: o.textContent, value: o.value }))).toEqual([
      { text: 'Low', value: 'low' },
      { text: 'Medium (Default)', value: 'medium' },
      { text: 'High', value: 'high' },
    ]);

    // Pick High directly and add it to the table
    fireEvent.change(effortSelect, { target: { value: 'high' } });
    fireEvent.click(screen.getByRole('button', { name: '+ Add' }));

    expect(screen.getByText('Gemini 4 Carbon · High')).toBeTruthy();
    expect(screen.queryByText('Gemini 4 Carbon · Medium')).toBeNull();
    expect(screen.queryByLabelText('Gemini 4 Carbon Effort')).toBeNull();

    // Remaining addable tiers for Carbon are now Low and Medium (High is already in the table)
    fireEvent.change(screen.getByLabelText('Add a model'), {
      target: { value: 'series:antigravity-oauth:gemini-4-carbon' },
    });
    const remainingEffortSelect = screen.getByLabelText('Gemini 4 Carbon Effort') as HTMLSelectElement;
    expect([...remainingEffortSelect.options].map((o) => o.value)).toEqual(['low', 'medium']);
  });
});
