import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { NativeModelsCard } from '../NativeModelsCard';
import { NativeSyncDiffDialog } from '../NativeSyncDiffDialog';
import type {
  NativeModelAddedItem,
  NativeModelDeprecatedItem,
  NativeModelsRetireResponse,
  NativeModelsStatusResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  retireResult: {
    run_id: 'r-1',
    results: [{ key: 'legacy/m', status: 'in_use', in_use_by: ['lead'] }],
    retired_keys: [],
    retired_at: '2026-11-16T00:00:00Z',
  } as NativeModelsRetireResponse,
  statusData: {
    overlay: { exists: false, path: '', model_count: 0 },
    last_check: null,
    providers: [
      {
        provider: 'anthropic',
        display_name: 'Anthropic',
        status: 'ok',
        model_count: 12,
      },
    ],
    diff: {
      added: [],
      deprecated: [
        { key: 'legacy/m', display_name: 'Legacy M', provider: 'anthropic', in_use_by: [] },
      ],
      unchanged: 5,
    },
    retired: [],
  } as NativeModelsStatusResponse,
  retireMutate: vi.fn<(p: { keys: string[] }) => Promise<NativeModelsRetireResponse>>(),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    isPm: true,
    callerTier: 'admin',
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => {
    const snapshot = {
      tier: 'admin' as const,
      status: 'ready' as const,
      authMode: 'local' as const,
      isPlatform: false,
      isKnown: true,
      isConfirming: false,
      isMultiUserAuth: true,
      isSingleUserOss: false,
      isAdmin: true,
      isInternal: true,
      isExternal: false,
      canActAsOwner: true,
      canPublishPublic: true,
      canInvite: true,
      isTeamCredentialsShared: true,
      canManageCredentials: true,
      isPm: true,
      colleagueSharingEnabled: false,
    };
    return {
      ...snapshot,
      getLatestIdentity: () => snapshot,
      isConfirmingNow: () => false,
      revalidate: vi.fn(async () => undefined),
    };
  },
}));

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
}));

vi.mock('@/hooks/useLocale', () => ({
  useLocale: () => 'en-US',
}));

vi.mock('../../../hooks/useOpenClaw', () => ({
  useNativeModelsStatus: () => ({
    data: h.statusData,
    isLoading: false,
    isError: false,
  }),
  useNativeModelsCheck: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useNativeModelsSync: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useNativeModelsRetire: () => ({
    mutateAsync: (p: { keys: string[] }) => h.retireMutate(p),
    isPending: false,
  }),
  useNativeModelsRestore: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => {
      if (!opts) return key;
      const parts: string[] = [];
      if ('count' in opts) parts.push(`count=${String(opts.count)}`);
      if ('keys' in opts) parts.push(`keys=${String(opts.keys)}`);
      if ('model' in opts) parts.push(`model=${String(opts.model)}`);
      if ('roles' in opts) parts.push(`roles=${String(opts.roles)}`);
      return parts.length > 0 ? `${key}:${parts.join(',')}` : key;
    },
  }),
}));

function addedItem(overrides: Partial<NativeModelAddedItem>): NativeModelAddedItem {
  return {
    provider: 'anthropic',
    upstream_id: 'claude-sonnet-4-6-20260501',
    suggested_key: 'anthropic/claude-sonnet-4-6',
    display_name: 'Claude Sonnet 4.6',
    conflict: false,
    previously_retired: false,
    ...overrides,
  };
}

describe('NativeSyncDiffDialog & NativeModelsCard', () => {
  beforeEach(() => {
    h.retireMutate.mockReset().mockImplementation(async () => h.retireResult);
  });

  it('submits suggested_key (not provider:upstream_id) and initializes/prunes selection when opened or updated', async () => {
    const onSync = vi.fn<(models: string[]) => Promise<void>>(async () => undefined);
    const onClose = vi.fn();
    const initialAdded = [
      addedItem({
        provider: 'anthropic',
        upstream_id: 'claude-sonnet-4-6-20260501',
        suggested_key: 'anthropic/claude-sonnet-4-6',
      }),
    ];
    const deprecated: NativeModelDeprecatedItem[] = [];

    const { rerender } = render(
      <NativeSyncDiffDialog
        open={false}
        onClose={onClose}
        added={[]}
        deprecated={deprecated}
        unchangedCount={4}
        onSync={onSync}
      />,
    );

    // Opening dialog with populated list initializes selection by suggested_key
    rerender(
      <NativeSyncDiffDialog
        open
        onClose={onClose}
        added={initialAdded}
        deprecated={deprecated}
        unchangedCount={4}
        onSync={onSync}
      />,
    );

    fireEvent.click(screen.getByTestId('native-models-apply-btn'));
    await waitFor(() =>
      expect(onSync).toHaveBeenCalledWith(['anthropic/claude-sonnet-4-6']),
    );
    expect(onSync).not.toHaveBeenCalledWith(['anthropic:claude-sonnet-4-6-20260501']);

    // Pruning: remove old item from list and add a new suggested_key
    onSync.mockClear();
    rerender(
      <NativeSyncDiffDialog
        open
        onClose={onClose}
        added={[
          addedItem({
            provider: 'openai',
            upstream_id: 'gpt-5.4-2026',
            suggested_key: 'openai/gpt-5.4',
            display_name: 'GPT 5.4',
          }),
        ]}
        deprecated={deprecated}
        unchangedCount={3}
        onSync={onSync}
      />,
    );

    // Toggle GPT 5.4 on cleanly after pruning removed stale anthropic selection
    fireEvent.click(screen.getByRole('switch', { name: 'GPT 5.4' }));
    fireEvent.click(screen.getByTestId('native-models-apply-btn'));
    await waitFor(() => expect(onSync).toHaveBeenCalledWith(['openai/gpt-5.4']));
  });

  it('passes count to providerOk/retireSelected and surfaces in_use warning instead of fake success', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <NativeModelsCard />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText('dalton.nativeModels.providerOk:count=12'),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('native-models-view-diff'));
    fireEvent.click(screen.getByRole('switch', { name: 'Legacy M' }));

    const retireBtn = screen.getByTestId('native-models-retire-btn');
    expect(retireBtn).toHaveTextContent('dalton.nativeModels.retireSelected:count=1');
    fireEvent.click(retireBtn);

    const confirmBtns = await screen.findAllByRole('button', {
      name: 'dalton.nativeModels.retireConfirm',
    });
    fireEvent.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => expect(h.retireMutate).toHaveBeenCalledWith({ keys: ['legacy/m'] }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'dalton.nativeModels.retireInUseItem:model=legacy/m,roles=lead',
    );
  });

  it('uses warning notice and explains every skipped item (in_use & not_deprecated) on partial retire', async () => {
    h.statusData = {
      ...h.statusData,
      diff: {
        added: [],
        deprecated: [
          { key: 'ok/m', display_name: 'Ok M', provider: 'anthropic', in_use_by: [] },
          { key: 'keep/m', display_name: 'Keep M', provider: 'anthropic', in_use_by: [] },
        ],
        unchanged: 2,
      },
    };
    h.retireResult = {
      run_id: 'r-mix',
      retired_keys: ['ok/m'],
      retired_at: '2026-11-16T00:00:00Z',
      results: [
        { key: 'ok/m', status: 'retired' },
        { key: 'keep/m', status: 'not_deprecated' },
        { key: 'busy/m', status: 'in_use', in_use_by: ['lead', 'audit'] },
      ],
    };

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <NativeModelsCard />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByTestId('native-models-view-diff'));
    fireEvent.click(screen.getByRole('switch', { name: 'Ok M' }));
    fireEvent.click(screen.getByRole('switch', { name: 'Keep M' }));
    fireEvent.click(screen.getByTestId('native-models-retire-btn'));

    const confirmBtns = await screen.findAllByRole('button', {
      name: 'dalton.nativeModels.retireConfirm',
    });
    fireEvent.click(confirmBtns[confirmBtns.length - 1]);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('dalton.nativeModels.retireDone:count=1');
    expect(alert).toHaveTextContent(
      'dalton.nativeModels.retireInUseItem:model=busy/m,roles=lead, audit',
    );
    expect(alert).toHaveTextContent(
      'dalton.nativeModels.retireNotDeprecatedItem:model=keep/m',
    );
  });
});
