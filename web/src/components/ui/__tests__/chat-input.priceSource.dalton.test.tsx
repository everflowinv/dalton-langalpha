import type { ComponentProps } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { ChatInputModelMenu } from '../chat-input.modelMenu';
import type { ModelTuning } from '../chat-input.modelTuning';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';
import { modelGroups } from '@/lib/modelGroups';
import type { Payer } from '@/lib/modelPayer';

vi.mock('@/hooks/usePayerLabel', async () => {
  const { payerLabel } = await import('@/lib/modelPayer');
  const { useTranslation } = await import('react-i18next');
  return {
    usePayerLabel: () => {
      const { t } = useTranslation();
      return (payer: Payer) => payerLabel(t, payer);
    },
  };
});
vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => false }));
vi.mock('@/hooks/useAccount', () => ({
  useAccount: () => ({
    organization: null,
    policy: null,
    unknown: false,
    suspended: false,
    organizationName: null,
    setterName: () => ({ start: '', mid: '' }),
  }),
}));

const metadata: Record<string, ModelMetadataEntry> = {
  'model-official': {
    display_name: 'Model Official',
    provider: 'openai',
    access_type: 'api_key',
    reasoning_efforts: ['low', 'high'],
    reasoning_effort_default: 'high',
    pricing: { input: 2, output: 10 },
    pricing_meta: { source_kind: 'official', estimated: false },
  },
  'model-estimated': {
    display_name: 'Model Estimated',
    provider: 'openai',
    access_type: 'api_key',
    pricing: { input: 1.5, output: 6 },
    pricing_meta: { source_kind: 'official', estimated: true },
  },
  'model-third-party': {
    display_name: 'Model Third Party',
    provider: 'openai',
    access_type: 'api_key',
    pricing: { input: 3, output: 12 },
    pricing_meta: { source_kind: 'third_party', estimated: false },
  },
  'model-sub-estimated': {
    display_name: 'Model Sub Estimated',
    provider: 'z-ai-cn-coding',
    access_type: 'coding_plan',
    notional_pricing: { estimated: true },
  },
  'model-no-source': {
    display_name: 'Model Plain',
    provider: 'openai',
    access_type: 'api_key',
    pricing: { input: 0.5, output: 1.5 },
  },
};

const payers: Record<string, Payer> = {
  'model-official': 'own_key',
  'model-estimated': 'own_key',
  'model-third-party': 'own_key',
  'model-sub-estimated': 'subscription:z-ai-cn-coding',
  'model-no-source': 'own_key',
};

const groups = modelGroups({
  models: {
    openai: {
      display_name: 'OpenAI',
      models: ['model-official', 'model-estimated', 'model-third-party', 'model-no-source'],
    },
    'z-ai-cn-coding': {
      display_name: 'GLM Coding Plan',
      models: ['model-sub-estimated'],
    },
  },
  payers,
});

const tuning: ModelTuning = {
  reasoningEffort: 'high',
  defaultEffort: 'high',
  onReasoningEffortChange: vi.fn(),
  fastMode: false,
  onFastModeChange: vi.fn(),
};

function openPicker(overrides: Partial<ComponentProps<typeof ChatInputModelMenu>> = {}) {
  render(
    <MemoryRouter>
      <ChatInputModelMenu
        selectedModel="model-official"
        metadata={metadata}
        payers={payers}
        groups={groups}
        favorites={[
          'model-official',
          'model-estimated',
          'model-third-party',
          'model-sub-estimated',
          'model-no-source',
        ]}
        recent={[]}
        onToggleFavorite={vi.fn()}
        onSelectModel={vi.fn()}
        rowStatus={() => ({})}
        dropdownDirection="up"
        container={null}
        tuning={tuning}
        {...overrides}
      />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByTitle('Select model'));
  return screen.getByRole('dialog');
}

describe('ModelPicker price source badges', () => {
  it('renders Official, Estimated, and Third-party badges beside rows and API price, omitting models with no source', () => {
    const dialog = openPicker();

    expect(within(dialog).getByTestId('model-picker-price-source-model-official')).toHaveTextContent(
      /Official|官方/,
    );
    expect(within(dialog).getByTestId('model-picker-price-source-model-estimated')).toHaveTextContent(
      /Estimated|估算/,
    );
    expect(within(dialog).getByTestId('model-picker-price-source-model-third-party')).toHaveTextContent(
      /Third-party|第三方/,
    );
    expect(within(dialog).getByTestId('model-picker-price-source-model-sub-estimated')).toHaveTextContent(
      /Estimated|估算/,
    );
    expect(within(dialog).queryByTestId('model-picker-price-source-model-no-source')).toBeNull();

    // Right-hand tuning pane API price line shows the selected model's badge too
    expect(within(dialog).getByTestId('tuning-pane-price-source')).toHaveTextContent(/Official|官方/);
  });

  it('omits the tuning pane price source badge when the selected model has no price source metadata', () => {
    const dialog = openPicker({ selectedModel: 'model-no-source' });
    expect(within(dialog).getByText('$0.50 in · $1.50 out')).toBeInTheDocument();
    expect(within(dialog).queryByTestId('tuning-pane-price-source')).toBeNull();
  });
});
