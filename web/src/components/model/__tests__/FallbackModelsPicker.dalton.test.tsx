import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { FallbackModelsPicker } from '../FallbackModelsPicker';
import { buildSeriesCatalog } from '@/lib/antigravitySeries';
import type { ProviderModelsData } from '../types';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';

vi.mock('@/hooks/usePayerLabel', () => ({
  usePayerLabel: () => () => 'Antigravity',
}));

const SERIES = buildSeriesCatalog([
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
]);

const RAW_META: Record<string, ModelMetadataEntry> = {
  'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth' },
  'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth' },
  'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth' },
  'gemini-4-neon-xhigh-oauth': { provider: 'antigravity-oauth' },
};

const MODELS: Record<string, ProviderModelsData> = {
  'antigravity-oauth': {
    display_name: 'Google Antigravity',
    models: [
      'gemini-4-neon-low-oauth',
      'gemini-4-neon-medium-oauth',
      'gemini-4-neon-high-oauth',
      'gemini-4-neon-xhigh-oauth',
    ],
  },
};

describe('FallbackModelsPicker Antigravity series', () => {
  it('renders selected chips as Series · Effort, collapses add panel by series, and allows default or per-effort add', () => {
    const onChange = vi.fn();
    render(
      <FallbackModelsPicker
        selected={['gemini-4-neon-medium-oauth']}
        onChange={onChange}
        models={MODELS}
        metadata={RAW_META}
        rawMetadata={RAW_META}
        seriesCatalog={SERIES}
      />,
    );

    expect(screen.getByText('Gemini 4 Neon · Medium')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /\+ Add/i }));
    // Only one "Gemini 4 Neon" series row in the dropdown
    expect(screen.getAllByRole('button', { name: 'Gemini 4 Neon' })).toHaveLength(1);

    // Clicking the series name adds the series default_effort (high)
    fireEvent.click(screen.getByRole('button', { name: 'Gemini 4 Neon' }));
    expect(onChange).toHaveBeenCalledWith(['gemini-4-neon-medium-oauth', 'gemini-4-neon-high-oauth']);

    // Clicking a specific effort pill toggles that variant
    fireEvent.click(screen.getByRole('button', { name: 'Gemini 4 Neon Low' }));
    expect(onChange).toHaveBeenCalledWith(['gemini-4-neon-medium-oauth', 'gemini-4-neon-low-oauth']);
  });

  it('keeps same-named custom models uncollapsed outside the Antigravity series', () => {
    const onChange = vi.fn();
    const customModels = [{ name: 'gemini-4-neon-high-oauth', provider: 'openai', model_id: 'custom-neon' }];
    render(
      <FallbackModelsPicker
        selected={[]}
        onChange={onChange}
        models={{
          'antigravity-oauth': {
            display_name: 'Google Antigravity',
            models: ['gemini-4-neon-low-oauth', 'gemini-4-neon-medium-oauth'],
          },
          openai: {
            display_name: 'OpenAI',
            models: ['gemini-4-neon-high-oauth'],
          },
        }}
        metadata={{
          ...RAW_META,
          'gemini-4-neon-high-oauth': { provider: 'openai', is_custom_model: true, display_name: 'Custom Neon' },
        }}
        rawMetadata={RAW_META}
        seriesCatalog={SERIES}
        customModels={customModels}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /\+ Add/i }));
    expect(screen.getByRole('button', { name: 'Custom Neon' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Gemini 4 Neon' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Gemini 4 Neon High' })).toBeNull();
  });
});
