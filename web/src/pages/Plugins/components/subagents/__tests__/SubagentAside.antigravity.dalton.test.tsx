import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { SubagentView } from '@/pages/ChatAgent/utils/api';
import type { SubagentEdit } from '@/hooks/useSubagents';
import type { AntigravitySeriesEntry } from '@/api/model';
import { buildSeriesCatalog } from '@/lib/antigravitySeries';
import { EffortBlock, ModelBlock, type SubagentModels } from '../SubagentAside';

vi.mock('@/hooks/useClaudeCode', () => ({ useClaudeCodeSetup: () => ({ setup: null }) }));

const prefState = vi.hoisted(() => ({ preferences: {} as Record<string, unknown> }));
vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: prefState.preferences }),
}));

const NEON_SERIES: AntigravitySeriesEntry = {
  series_id: 'gemini-4-neon',
  series_prefix: 'gemini-4-neon-',
  display_name: 'Gemini 4 Neon',
  qualified_display_name: 'Gemini 4 Neon (Antigravity)',
  provider: 'antigravity-oauth',
  default_effort: 'high',
  default_model: 'gemini-4-neon-high-oauth',
  efforts: ['low', 'medium', 'high', 'xhigh'],
  variants: {
    low: 'gemini-4-neon-low-oauth',
    medium: 'gemini-4-neon-medium-oauth',
    high: 'gemini-4-neon-high-oauth',
    xhigh: 'gemini-4-neon-xhigh-oauth',
  },
};

const GPT_OSS_SERIES: AntigravitySeriesEntry = {
  series_id: 'gpt-oss-120b',
  series_prefix: 'gpt-oss-120b-',
  display_name: 'GPT-OSS 120B',
  qualified_display_name: 'GPT-OSS 120B (Antigravity)',
  provider: 'antigravity-oauth',
  default_effort: 'medium',
  default_model: 'gpt-oss-120b-medium-oauth',
  efforts: ['medium'],
  variants: {
    medium: 'gpt-oss-120b-medium-oauth',
  },
};

const METADATA: SubagentModels['metadata'] = {
  'gemini-4-neon-low-oauth': {
    display_name: 'Gemini 4 Neon (Low)',
    provider: 'antigravity-oauth',
    reasoning_efforts: ['low'],
  },
  'gemini-4-neon-medium-oauth': {
    display_name: 'Gemini 4 Neon (Medium)',
    provider: 'antigravity-oauth',
    reasoning_efforts: ['medium'],
  },
  'gemini-4-neon-high-oauth': {
    display_name: 'Gemini 4 Neon (High)',
    provider: 'antigravity-oauth',
    reasoning_efforts: ['high'],
  },
  'gemini-4-neon-xhigh-oauth': {
    display_name: 'Gemini 4 Neon (XHigh)',
    provider: 'antigravity-oauth',
    reasoning_efforts: ['xhigh'],
  },
  'gpt-oss-120b-medium-oauth': {
    display_name: 'GPT-OSS 120B (Medium)',
    provider: 'antigravity-oauth',
    reasoning_efforts: ['medium'],
  },
  'gpt-5.4': {
    display_name: 'GPT-5.4',
    provider: 'openai',
    reasoning_efforts: ['low', 'medium', 'high'],
  },
};

function makeModels(): SubagentModels {
  const seriesCatalog = buildSeriesCatalog([NEON_SERIES, GPT_OSS_SERIES]);
  return {
    models: {
      'antigravity-oauth': {
        models: [
          'gemini-4-neon-low-oauth',
          'gemini-4-neon-medium-oauth',
          'gemini-4-neon-high-oauth',
          'gemini-4-neon-xhigh-oauth',
          'gpt-oss-120b-medium-oauth',
        ],
      },
      openai: { models: ['gpt-5.4'] },
    },
    lockedModels: {},
    payers: {},
    metadata: METADATA,
    customModels: [],
    seriesCatalog,
    rawApiResponse: {
      model_metadata: METADATA,
      antigravity_series: [NEON_SERIES, GPT_OSS_SERIES],
    } as unknown as SubagentModels['rawApiResponse'],
  } as unknown as SubagentModels;
}

describe('SubagentAside Antigravity series integration', () => {
  beforeEach(() => {
    prefState.preferences = {};
  });

  it('collapses Antigravity variants in ModelBlock, hides inline effort picker, and clears effort on pick', () => {
    const writes: SubagentEdit[] = [];
    const view = {
      name: 'research',
      model: null,
      effort: 'low',
      problems: [],
      base: null,
    } as unknown as SubagentView;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ModelBlock view={view} data={{} as never} models={makeModels()} write={(e) => writes.push(e)} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const modelSelect = screen.getByRole('combobox') as HTMLSelectElement;
    const labels = [...modelSelect.options].map((o) => o.textContent);
    expect(labels.filter((l) => l === 'Gemini 4 Neon')).toHaveLength(1);
    expect(labels.some((l) => l?.includes('Gemini 4 Neon ('))).toBe(false);
    expect(screen.queryByLabelText(/thinking effort|reasoning effort/i)).not.toBeInTheDocument();

    fireEvent.change(modelSelect, { target: { value: 'gemini-4-neon-high-oauth' } });
    expect(writes).toEqual([{ model: 'gemini-4-neon-high-oauth', effort: null }]);
  });

  it('switches suffixed model key directly in EffortBlock while keeping effort null', () => {
    const writes: SubagentEdit[] = [];
    const view = {
      name: 'research',
      model: 'gemini-4-neon-medium-oauth',
      effort: null,
      problems: [],
      base: null,
    } as unknown as SubagentView;

    render(
      <MemoryRouter>
        <EffortBlock view={view} data={{} as never} models={makeModels()} write={(e) => writes.push(e)} />
      </MemoryRouter>,
    );

    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select.value).toBe('medium');

    fireEvent.change(select, { target: { value: 'high' } });
    expect(writes).toEqual([{ model: 'gemini-4-neon-high-oauth', effort: null }]);
  });

  it('hides the EffortBlock switcher for single-variant Antigravity series like GPT-OSS 120B', () => {
    const view = {
      name: 'research',
      model: 'gpt-oss-120b-medium-oauth',
      effort: null,
      problems: [],
      base: null,
    } as unknown as SubagentView;

    render(
      <MemoryRouter>
        <EffortBlock view={view} data={{} as never} models={makeModels()} write={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.getByText(/GPT-OSS 120B/)).toBeInTheDocument();
  });
});
