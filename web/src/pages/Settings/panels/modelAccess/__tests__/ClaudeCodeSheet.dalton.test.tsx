import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import type { ClaudeCodeSetup } from '@/api/model';
import type { canRemoteControl } from '@/lib/desktop';
import type { openComputersPanel } from '@/pages/ChatAgent/hooks/computerPanelStore';
import type { useFeatures, useSetFeatureOverride } from '@/hooks/useFeatures';
import type { useClaudeCodeComputers, useClaudeCodeStatus } from '@/hooks/useClaudeCode';
import type { useAllModels } from '@/hooks/useAllModels';
import type { useDefaultModel } from '@/hooks/useSlotDefaultModels';
import type { useClaudeCodeCustody } from '@/hooks/useClaudeCodeCustody';

const toggleMock = vi.fn<ReturnType<typeof useSetFeatureOverride>['mutateAsync']>();
const openComputersMock = vi.fn<typeof openComputersPanel>();

vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: vi.fn<() => Partial<ReturnType<typeof useFeatures>>>(() => ({
    data: [
      {
        key: 'claude_code_runtime',
        label: 'Claude Code',
        description: '',
        tradeoffs: null,
        enabled: false,
        gate: 'opt_in',
        min_tier: null,
        user_override: null,
      },
    ],
  })),
  useSetFeatureOverride: () => ({ mutateAsync: toggleMock, isPending: false }),
}));
vi.mock('@/hooks/useClaudeCode', () => ({
  useClaudeCodeStatus: vi.fn<() => Partial<ReturnType<typeof useClaudeCodeStatus>>>(() => ({ enabled: false })),
  useClaudeCodeComputers: vi.fn<() => Partial<ReturnType<typeof useClaudeCodeComputers>>>(() => ({
    cliComputer: null,
    computers: [],
    clisMax: null,
  })),
  useSetComputerClis: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: vi.fn<() => Partial<ReturnType<typeof useAllModels>>>(() => ({
    models: {},
    metadata: {},
    isLoading: false,
  })),
}));
vi.mock('@/hooks/useSlotDefaultModels', () => ({
  useDefaultModel: vi.fn<typeof useDefaultModel>(() => null),
}));
vi.mock('@/hooks/useClaudeCodeCustody', () => ({
  useClaudeCodeCustody: vi.fn<() => Partial<ReturnType<typeof useClaudeCodeCustody>>>(() => ({ custody: null })),
}));
vi.mock('@/lib/desktop', () => ({
  canRemoteControl: vi.fn<typeof canRemoteControl>(() => false),
}));
vi.mock('@/pages/ChatAgent/hooks/computerPanelStore', () => ({
  openComputersPanel: openComputersMock,
  useComputersPanel: () => ({ specComputerId: null }),
}));
vi.mock('@/pages/ChatAgent/hooks/useMediumAction', () => ({
  useMediumAction: () => null,
}));
vi.mock('../ClaudeCodeHostedChoice', () => ({ ClaudeCodeHostedChoice: () => null }));
vi.mock('../ClaudeCodePlanOutChoice', () => ({ ClaudeCodePlanOutChoice: () => null }));

describe('ClaudeCodeSheet in Dalton build', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('hides the desktop app download link and points Learn more to /help/concepts/claude-code/#requirements', async () => {
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { ClaudeCodeSheet } = await import('../ClaudeCodeSheet');
    const setup: ClaudeCodeSetup = {
      state: 'no_computer',
      on: false,
      computers: [],
      saved_token_runs: false,
      cloud: false,
      too_small: [],
    };

    renderWithProviders(
      <ClaudeCodeSheet
        setup={setup}
        tokenRows={[]}
        onRemoveToken={vi.fn()}
        onCover={vi.fn()}
      />,
      { route: '/settings?tab=model' },
    );

    expect(screen.queryByRole('link', { name: /Get the Mac app/i })).toBeNull();
    expect(screen.getByRole('button', { name: 'Add SSH computer' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Learn more/i })).toHaveAttribute(
      'href',
      '/help/concepts/claude-code/#requirements',
    );
  });
});
