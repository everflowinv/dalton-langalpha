import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ChatInput from '../chat-input';
import { ChatInputRegistry, ContextBus } from '@/lib/contextBus';
import { buildSelectableAntigravitySeries, buildSeriesCatalog } from '@/lib/antigravitySeries';
import { queryKeys } from '@/lib/queryKeys';
import type { CustomModelEntry, ProviderModelsData } from '@/components/model/types';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

const SERIES_CATALOG = buildSeriesCatalog([
  {
    series_id: 'antigravity-oauth:gemini-4-neon',
    series_prefix: 'gemini-4-neon',
    provider: 'antigravity-oauth',
    display_name: 'Gemini 4 Neon',
    qualified_display_name: 'Gemini 4 Neon (Antigravity)',
    default_effort: 'high',
    default_model: 'gemini-4-neon-high-oauth',
    efforts: ['low', 'medium', 'high', 'xhigh'],
    variants: {
      low: 'gemini-4-neon-low-oauth',
      medium: 'gemini-4-neon-medium-oauth',
      high: 'gemini-4-neon-high-oauth',
      xhigh: 'gemini-4-neon-xhigh-oauth',
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
  {
    series_id: 'antigravity-oauth:gpt-oss-120b',
    series_prefix: 'gpt-oss-120b',
    provider: 'antigravity-oauth',
    display_name: 'GPT-OSS 120B',
    qualified_display_name: 'GPT-OSS 120B (Antigravity)',
    default_effort: 'medium',
    default_model: 'gpt-oss-120b-medium-oauth',
    efforts: ['medium'],
    variants: { medium: 'gpt-oss-120b-medium-oauth' },
  },
]);

const RAW_META: Record<string, ModelMetadataEntry> = {
  'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['low'] },
  'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['medium'] },
  'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['high'] },
  'gemini-4-neon-xhigh-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['xhigh'] },
  'gemini-4-carbon-low-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['low'] },
  'gemini-4-carbon-medium-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['medium'] },
  'gemini-4-carbon-high-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['high'] },
  'gpt-oss-120b-medium-oauth': { provider: 'antigravity-oauth', reasoning_efforts: ['medium'] },
};

const h = vi.hoisted(() => ({
  models: {} as Record<string, ProviderModelsData>,
  metadata: {} as Record<string, ModelMetadataEntry>,
  customModels: [] as CustomModelEntry[],
  preferences: { model_preference: { preferred_model: 'gemini-4-neon-medium-oauth' }, other_preference: {} } as Record<string, unknown>,
  validModelNames: new Set<string>(),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: h.preferences, isLoading: false, isLoaded: true }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    models: h.models,
    slotModels: h.models,
    metadata: h.metadata,
    payers: Object.fromEntries([...h.validModelNames].map((m) => [m, 'subscription:antigravity-oauth'])),
    validModelNames: h.validModelNames,
    catalogModelNames: h.validModelNames,
    customModels: h.customModels,
    rawApiResponse: { model_metadata: RAW_META },
    seriesCatalog: SERIES_CATALOG,
    selectableSeries: buildSelectableAntigravitySeries(SERIES_CATALOG, h.models, {
      rawMetadata: RAW_META,
      customModels: h.customModels,
    }),
    selectableSlotSeries: [],
    systemDefaults: { default_model: 'gemini-4-neon-high-oauth' },
    offPlanModels: new Set(),
    isLoading: false,
  }),
}));

vi.mock('@/hooks/useFavoriteModels', () => ({
  useFavoriteModels: () => ({
    models: ['gemini-4-neon-low-oauth', 'gemini-4-neon-high-oauth'],
    add: vi.fn(),
    remove: vi.fn(),
    removeMany: vi.fn(),
  }),
}));

function renderComposer(props: Partial<React.ComponentProps<typeof ChatInput>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(queryKeys.user.preferences(), h.preferences);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ChatInput onSend={vi.fn()} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ChatInput Antigravity series collapsing & effort pill', () => {
  beforeEach(() => {
    ContextBus.__resetForTests();
    ChatInputRegistry.__resetForTests();
    Element.prototype.scrollIntoView = vi.fn();
    const agList = [
      'gemini-4-neon-low-oauth',
      'gemini-4-neon-medium-oauth',
      'gemini-4-neon-high-oauth',
      'gemini-4-neon-xhigh-oauth',
      'gemini-4-carbon-low-oauth',
      'gemini-4-carbon-medium-oauth',
      'gemini-4-carbon-high-oauth',
      'gpt-oss-120b-medium-oauth',
    ];
    h.models = { 'antigravity-oauth': { display_name: 'Google Antigravity', models: agList } };
    h.metadata = { ...RAW_META };
    h.customModels = [];
    h.validModelNames = new Set(agList);
  });

  it('displays saved gemini-4-neon-medium-oauth as "Gemini 4 Neon" + "Medium" and switches variant via effort menu', () => {
    const onPickModel = vi.fn();
    const { container, rerender } = renderComposer({
      model: 'gemini-4-neon-medium-oauth',
      onPickModel,
      threadModels: ['gemini-4-neon-low-oauth', 'gemini-4-neon-medium-oauth'],
    });

    const modelPill = container.querySelector('button[data-pill="model"]')!;
    const effortPill = container.querySelector('button[data-pill="effort"]')!;
    expect(modelPill).toHaveTextContent('Gemini 4 Neon');
    expect(modelPill).not.toHaveTextContent('Medium');
    expect(effortPill).toHaveTextContent('Medium');

    // Open model picker: Favorites + In thread collapses Neon to 1 row
    fireEvent.click(modelPill);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getAllByText('Gemini 4 Neon')).toHaveLength(1);

    // Search "Neon" still collapses to 1 row
    const search = within(dialog).getByRole('searchbox');
    fireEvent.change(search, { target: { value: 'Neon' } });
    expect(within(dialog).getAllByText('Gemini 4 Neon')).toHaveLength(1);

    // Clear search and switch to Google Antigravity tab -> Neon/Carbon/GPT-OSS each appear once
    fireEvent.change(search, { target: { value: '' } });
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Google Antigravity' }));
    expect(within(dialog).getAllByText('Gemini 4 Neon')).toHaveLength(1);
    expect(within(dialog).getAllByText('Gemini 4 Carbon')).toHaveLength(1);
    expect(within(dialog).getAllByText('GPT-OSS 120B')).toHaveLength(1);

    // Clicking already-selected Gemini 4 Neon keeps current suffix (no-op)
    fireEvent.click(within(dialog).getByRole('row', { name: /Gemini 4 Neon/ }));
    expect(onPickModel).not.toHaveBeenCalled();

    // Re-open and pick Gemini 4 Carbon -> picks Carbon's default_effort (medium)
    fireEvent.click(modelPill);
    const dialog2 = screen.getByRole('dialog');
    fireEvent.click(within(dialog2).getByRole('radio', { name: 'Google Antigravity' }));
    fireEvent.click(within(dialog2).getByRole('row', { name: /Gemini 4 Carbon/ }));
    expect(onPickModel).toHaveBeenCalledWith('gemini-4-carbon-medium-oauth');

    // Open effort menu on Neon Medium -> Default badge on High, clicking High writes neon-high
    fireEvent.click(effortPill);
    const menu = screen.getByRole('menu');
    const highItem = within(menu).getByRole('menuitemradio', { name: /^High/i });
    expect(highItem).toHaveTextContent('Default');
    fireEvent.click(highItem);
    expect(onPickModel).toHaveBeenCalledWith('gemini-4-neon-high-oauth');

    // Single-effort GPT-OSS 120B hides the effort pill
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ChatInput onSend={vi.fn()} model="gpt-oss-120b-medium-oauth" onPickModel={onPickModel} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(container.querySelector('button[data-pill="model"]')).toHaveTextContent('GPT-OSS 120B');
    expect(container.querySelector('button[data-pill="effort"]')).toBeNull();
  });

  it('does not collapse a same-named custom model into the Antigravity series', () => {
    h.customModels = [{ name: 'gemini-4-neon-high-oauth', provider: 'openai', model_id: 'custom' }];
    h.models = {
      'antigravity-oauth': {
        display_name: 'Google Antigravity',
        models: ['gemini-4-neon-low-oauth', 'gemini-4-neon-medium-oauth'],
      },
      openai: {
        display_name: 'OpenAI',
        models: ['gemini-4-neon-high-oauth'],
      },
    };
    h.metadata = {
      ...RAW_META,
      'gemini-4-neon-high-oauth': { provider: 'openai', is_custom_model: true, display_name: 'Custom Neon' },
    };

    const { container } = renderComposer({ model: 'gemini-4-neon-high-oauth' });
    expect(container.querySelector('button[data-pill="model"]')).toHaveTextContent('Custom Neon');
  });
});
