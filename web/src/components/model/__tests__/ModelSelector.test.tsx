import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { ModelSelector } from '../ModelSelector';
import type { ProviderModelsData } from '../types';

vi.mock('@/hooks/usePayerLabel', async () => {
  const { payerLabel } = await import('@/lib/modelPayer');
  const { useTranslation } = await import('react-i18next');
  return { usePayerLabel: () => { const { t } = useTranslation(); return (payer: import('@/lib/modelPayer').Payer) => payerLabel(t, payer); } };
});

const sampleModels: Record<string, ProviderModelsData> = {
  openai: {
    display_name: 'OpenAI',
    models: ['gpt-4o', 'gpt-4o-mini'],
  },
  anthropic: {
    display_name: 'Anthropic',
    models: ['claude-sonnet-4-20250514'],
  },
};

describe('ModelSelector', () => {
  const defaultProps = {
    label: 'Default Model',
    description: 'The primary model for analysis',
    value: '',
    onChange: vi.fn(),
    models: sampleModels,
  };

  it('renders options grouped by provider using optgroup', () => {
    render(<ModelSelector {...defaultProps} />);

    // Label and description are rendered
    expect(screen.getByText('Default Model')).toBeInTheDocument();
    expect(screen.getByText('The primary model for analysis')).toBeInTheDocument();

    // optgroups present
    const optgroups = document.querySelectorAll('optgroup');
    expect(optgroups).toHaveLength(2);
    expect(optgroups[0]).toHaveAttribute('label', 'OpenAI');
    expect(optgroups[1]).toHaveAttribute('label', 'Anthropic');

    // Model options present within groups
    expect(screen.getByRole('option', { name: 'gpt-4o' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'gpt-4o-mini' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'claude-sonnet-4-20250514' })).toBeInTheDocument();
  });

  it('shows label and description', () => {
    render(<ModelSelector {...defaultProps} />);

    expect(screen.getByText('Default Model')).toBeInTheDocument();
    expect(screen.getByText('The primary model for analysis')).toBeInTheDocument();
  });

  it('offers a choice beside the placeholder that is not a model', () => {
    const onChange = vi.fn();
    render(
      <ModelSelector
        {...defaultProps}
        onChange={onChange}
        placeholder="Same as Default model"
        autoOption={{ value: '__auto__', label: 'Auto (gpt-4o-mini)' }}
      />,
    );

    const select = document.querySelector('select')!;
    expect([...select.options].slice(0, 2).map((o) => o.textContent)).toEqual([
      'Same as Default model',
      'Auto (gpt-4o-mini)',
    ]);
    fireEvent.change(select, { target: { value: '__auto__' } });
    expect(onChange).toHaveBeenCalledWith('__auto__');
  });

  it('fires onChange with selected model', () => {
    const onChange = vi.fn();
    render(<ModelSelector {...defaultProps} onChange={onChange} />);

    const select = document.querySelector('select')!;
    fireEvent.change(select, { target: { value: 'gpt-4o-mini' } });

    expect(onChange).toHaveBeenCalledWith('gpt-4o-mini');
  });

  it('prints the authored display name while the key stays the value', () => {
    render(
      <ModelSelector {...defaultProps} metadata={{ 'gpt-4o': { display_name: 'GPT-4o' } }} />,
    );

    expect(screen.getByRole('option', { name: 'GPT-4o' })).toHaveValue('gpt-4o');
    expect(screen.getByRole('option', { name: 'gpt-4o-mini' })).toBeInTheDocument();
  });

  it('shows "No models available" when no models', () => {
    render(
      <ModelSelector
        {...defaultProps}
        models={{}}
      />,
    );

    expect(screen.getByText('No models available')).toBeInTheDocument();
    expect(document.querySelector('select')).toBeNull();
  });

  // DALTON-HOOK(antigravity-series): series collapse, default effort pick, companion effort select, unavailable saved echo, and custom shadow isolation
  it('collapses Antigravity variants, selects default_effort, switches effort, preserves missing effort, and keeps custom shadows separate', async () => {
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
        efforts: ['low', 'medium', 'high', 'xhigh'],
        variants: {
          low: 'gemini-4-neon-low-oauth',
          medium: 'gemini-4-neon-medium-oauth',
          high: 'gemini-4-neon-high-oauth',
          xhigh: 'gemini-4-neon-xhigh-oauth',
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
    const rawMetadata = {
      'gemini-4-neon-low-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-neon-medium-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-neon-high-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-neon-xhigh-oauth': { provider: 'antigravity-oauth' },
      'gpt-oss-120b-medium-oauth': { provider: 'antigravity-oauth' },
    };
    const agModels: Record<string, ProviderModelsData> = {
      'antigravity-oauth': {
        display_name: 'Google Antigravity',
        models: [
          'gemini-4-neon-low-oauth',
          'gemini-4-neon-medium-oauth',
          'gemini-4-neon-high-oauth',
          'gpt-oss-120b-medium-oauth',
        ],
      },
    };
    const onChange = vi.fn();

    const { rerender } = render(
      <ModelSelector
        label="Default Model"
        value=""
        onChange={onChange}
        models={agModels}
        metadata={rawMetadata}
        rawMetadata={rawMetadata}
        seriesCatalog={seriesCatalog}
      />,
    );

    const modelSelect = screen.getByLabelText('Default Model') as HTMLSelectElement;
    const optionTexts = [...modelSelect.options].map((o) => o.textContent);
    expect(optionTexts).toEqual(['Select a model...', 'Gemini 4 Neon', 'GPT-OSS 120B']);

    // Selecting Gemini 4 Neon picks default_effort (high)
    fireEvent.change(modelSelect, { target: { value: 'gemini-4-neon-high-oauth' } });
    expect(onChange).toHaveBeenCalledWith('gemini-4-neon-high-oauth');

    // Rerender with an old stored unavailable variant (xhigh) -> missingLabel + Extra High in effort select
    rerender(
      <ModelSelector
        label="Default Model"
        value="gemini-4-neon-xhigh-oauth"
        onChange={onChange}
        models={agModels}
        metadata={rawMetadata}
        rawMetadata={rawMetadata}
        seriesCatalog={seriesCatalog}
        missingLabel={(v) => (v === 'gemini-4-neon-xhigh-oauth' ? 'Gemini 4 Neon · Extra High' : v)}
      />,
    );
    const effortSelect = screen.getByLabelText('Default Model Effort') as HTMLSelectElement;
    expect(effortSelect.value).toBe('xhigh');
    expect([...effortSelect.options].map((o) => o.value)).toEqual(['low', 'medium', 'high', 'xhigh']);

    fireEvent.change(effortSelect, { target: { value: 'medium' } });
    expect(onChange).toHaveBeenCalledWith('gemini-4-neon-medium-oauth');

    // Single-effort GPT-OSS 120B shows no companion effort select
    rerender(
      <ModelSelector
        label="Default Model"
        value="gpt-oss-120b-medium-oauth"
        onChange={onChange}
        models={agModels}
        metadata={rawMetadata}
        rawMetadata={rawMetadata}
        seriesCatalog={seriesCatalog}
      />,
    );
    expect(screen.queryByLabelText('Default Model Effort')).toBeNull();

    // Same-named custom model is NOT collapsed into the Antigravity series
    rerender(
      <ModelSelector
        label="Default Model"
        value=""
        onChange={onChange}
        models={{
          'antigravity-oauth': {
            display_name: 'Google Antigravity',
            models: ['gemini-4-neon-low-oauth', 'gemini-4-neon-medium-oauth', 'gpt-oss-120b-medium-oauth'],
          },
          openai: { display_name: 'OpenAI', models: ['gemini-4-neon-high-oauth'] },
        }}
        metadata={{
          ...rawMetadata,
          'gemini-4-neon-high-oauth': { provider: 'openai', is_custom_model: true, display_name: 'Custom Neon' },
        }}
        rawMetadata={rawMetadata}
        seriesCatalog={seriesCatalog}
        customModels={[{ name: 'gemini-4-neon-high-oauth', provider: 'openai', model_id: 'custom' }]}
      />,
    );
    expect(screen.getByRole('option', { name: 'Gemini 4 Neon' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Custom Neon' })).toBeInTheDocument();
  });

  // DALTON-HOOK(antigravity-series): background slot preferLowEffort + hideEffortPicker picks -low (or lowest) and keeps saved values
  it('picks -low (or lowest effort) without an effort picker or effort suffix when preferLowEffort and hideEffortPicker are set', async () => {
    const { buildSeriesCatalog } = await import('@/lib/antigravitySeries');
    const seriesCatalog = buildSeriesCatalog([
      {
        series_id: 'antigravity-oauth:gemini-4-hydrogen',
        series_prefix: 'gemini-4-hydrogen',
        provider: 'antigravity-oauth',
        display_name: 'Gemini 4 Hydrogen',
        qualified_display_name: 'Gemini 4 Hydrogen (Antigravity)',
        default_effort: 'high',
        default_model: 'gemini-4-hydrogen-high-oauth',
        efforts: ['low', 'medium', 'high'],
        variants: {
          low: 'gemini-4-hydrogen-low-oauth',
          medium: 'gemini-4-hydrogen-medium-oauth',
          high: 'gemini-4-hydrogen-high-oauth',
        },
      },
      {
        series_id: 'antigravity-oauth:gemini-4-carbon',
        series_prefix: 'gemini-4-carbon',
        provider: 'antigravity-oauth',
        display_name: 'Gemini 4 Carbon',
        qualified_display_name: 'Gemini 4 Carbon (Antigravity)',
        default_effort: 'high',
        default_model: 'gemini-4-carbon-high-oauth',
        efforts: ['medium', 'high'],
        variants: {
          medium: 'gemini-4-carbon-medium-oauth',
          high: 'gemini-4-carbon-high-oauth',
        },
      },
    ]);
    // DALTON-HOOK(antigravity-series): assert preferLowEffort picks -low (or lowest) with no effort UI
    const rawMetadata = {
      'gemini-4-hydrogen-low-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-hydrogen-medium-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-hydrogen-high-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-carbon-medium-oauth': { provider: 'antigravity-oauth' },
      'gemini-4-carbon-high-oauth': { provider: 'antigravity-oauth' },
    };
    const agModels: Record<string, ProviderModelsData> = {
      'antigravity-oauth': {
        display_name: 'Google Antigravity',
        models: [
          'gemini-4-hydrogen-low-oauth',
          'gemini-4-hydrogen-medium-oauth',
          'gemini-4-hydrogen-high-oauth',
          'gemini-4-carbon-medium-oauth',
          'gemini-4-carbon-high-oauth',
        ],
      },
    };
    const onChange = vi.fn();

    const { rerender } = render(
      <ModelSelector
        label="Background model"
        value=""
        onChange={onChange}
        models={agModels}
        metadata={rawMetadata}
        rawMetadata={rawMetadata}
        seriesCatalog={seriesCatalog}
        hideEffortPicker
        preferLowEffort
      />,
    );

    const select = screen.getByLabelText('Background model') as HTMLSelectElement;
    expect([...select.options].map((o) => ({ value: o.value, text: o.textContent }))).toEqual([
      { value: '', text: 'Select a model...' },
      { value: 'gemini-4-hydrogen-low-oauth', text: 'Gemini 4 Hydrogen' },
      { value: 'gemini-4-carbon-medium-oauth', text: 'Gemini 4 Carbon' },
    ]);
    expect(screen.queryByLabelText(/Effort/i)).toBeNull();
    expect(document.body.textContent).not.toContain('Low');

    fireEvent.change(select, { target: { value: 'gemini-4-hydrogen-low-oauth' } });
    expect(onChange).toHaveBeenCalledWith('gemini-4-hydrogen-low-oauth');

    // DALTON-HOOK(antigravity-series): already-saved non-low variant stays displayed as series name without effort picker or rewrite
    onChange.mockClear();
    rerender(
      <ModelSelector
        label="Background model"
        value="gemini-4-hydrogen-high-oauth"
        onChange={onChange}
        models={agModels}
        metadata={rawMetadata}
        rawMetadata={rawMetadata}
        seriesCatalog={seriesCatalog}
        hideEffortPicker
        preferLowEffort
      />,
    );
    expect(select.value).toBe('gemini-4-hydrogen-high-oauth');
    expect(select.selectedOptions[0].textContent).toBe('Gemini 4 Hydrogen');
    expect(screen.queryByLabelText(/Effort/i)).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });
});
