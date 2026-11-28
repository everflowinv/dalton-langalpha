import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import type { CapabilityId } from '@/hooks/useCapabilities';

let mockCapabilities: ReadonlySet<CapabilityId> = new Set<CapabilityId>();
let mockCoverageUnitsEnabled = false;

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({
      profile: mockCapabilities.has('colleague_sharing') ? ('everflow' as const) : ('core' as const),
      capabilities: mockCapabilities,
      hasCapability: (req: Parameters<typeof actual.hasRequiredCapability>[1]) =>
        actual.hasRequiredCapability(mockCapabilities, req),
      brandName: 'Dalton',
      fingerprint: 'fp',
      tickerLogosEnabled: false,
      isLoading: false,
      isLoaded: true,
      isCoreBuild: !mockCapabilities.has('colleague_sharing'),
      isEverflowBuild: mockCapabilities.has('colleague_sharing'),
    }),
  };
});

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: (enabled: boolean) => ({
    data: enabled ? { enabled: mockCoverageUnitsEnabled } : undefined,
    isLoading: false,
  }),
}));

vi.mock('../DaltonCoverageCard', () => ({
  DaltonCoverageCard: () => <div data-testid="mock-team-coverage-units-card">Team Coverage Units</div>,
}));

import type {
  WatchlistTierPoliciesResponse,
  fetchWatchlistTierPolicies,
  previewWatchlistTierPolicy,
  reconcileWatchlist,
  resetWatchlistTierPolicy,
  updateWatchlistTierPolicy,
} from '@/lib/dalton/watchlist';

const mockFetchPolicies = vi.fn<typeof fetchWatchlistTierPolicies>();
const mockPreviewPolicy = vi.fn<typeof previewWatchlistTierPolicy>();
const mockUpdatePolicy = vi.fn<typeof updateWatchlistTierPolicy>();
const mockResetPolicy = vi.fn<typeof resetWatchlistTierPolicy>();
const mockReconcile = vi.fn<typeof reconcileWatchlist>();

vi.mock('@/lib/dalton/watchlist', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/dalton/watchlist')>();
  return {
    ...actual,
    fetchWatchlistTierPolicies: (...args: Parameters<typeof fetchWatchlistTierPolicies>) =>
      mockFetchPolicies(...args),
    previewWatchlistTierPolicy: (...args: Parameters<typeof previewWatchlistTierPolicy>) =>
      mockPreviewPolicy(...args),
    updateWatchlistTierPolicy: (...args: Parameters<typeof updateWatchlistTierPolicy>) =>
      mockUpdatePolicy(...args),
    resetWatchlistTierPolicy: (...args: Parameters<typeof resetWatchlistTierPolicy>) =>
      mockResetPolicy(...args),
    reconcileWatchlist: (...args: Parameters<typeof reconcileWatchlist>) =>
      mockReconcile(...args),
  };
});

import { DaltonCoveragePanel } from '../DaltonCoveragePanel';
import { DaltonTierPolicyCard } from '../DaltonTierPolicyCard';

const DEFAULT_API_POLICIES: WatchlistTierPoliciesResponse = {
  ok: true,
  user_id: 'u-1',
  policies: {
    core: {
      tier: 'core',
      policy_version: 1,
      watch_enabled: true,
      watch_cron: '*/30 6-20 * * 1-5',
      delta_enabled: true,
      delta_cron: '0 11 * * 2-6',
      delta_timezone: 'Asia/Shanghai',
      move_enabled: true,
      move_threshold_pct: 4,
      earnings_stages: ['tm3', 't0', 'tp1'],
    },
    watch: {
      tier: 'watch',
      policy_version: 1,
      watch_enabled: true,
      watch_cron: '30 7 * * 1-5',
      delta_enabled: true,
      delta_cron: '0 11 * * 6',
      delta_timezone: 'Asia/Shanghai',
      move_enabled: true,
      move_threshold_pct: 4,
      earnings_stages: ['t0'],
    },
    radar: {
      tier: 'radar',
      policy_version: 1,
      watch_enabled: false,
      watch_cron: '30 7 * * 1-5',
      delta_enabled: false,
      delta_cron: '0 11 * * 6',
      delta_timezone: 'Asia/Shanghai',
      move_enabled: false,
      move_threshold_pct: 6,
      earnings_stages: [],
    },
  },
};

beforeEach(() => {
  mockCapabilities = new Set<CapabilityId>();
  mockCoverageUnitsEnabled = false;
  mockFetchPolicies.mockReset().mockResolvedValue(DEFAULT_API_POLICIES);
  mockPreviewPolicy.mockReset();
  mockUpdatePolicy.mockReset();
  mockResetPolicy.mockReset();
  mockReconcile.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('DaltonCoveragePanel & DaltonTierPolicyCard', () => {
  it('shows DaltonTierPolicyCard (#dalton-tier-policies) and hides team coverage units in Core mode', async () => {
    mockCapabilities = new Set<CapabilityId>(); // no colleague_sharing
    mockCoverageUnitsEnabled = true;

    renderWithProviders(<DaltonCoveragePanel />);
    const card = await screen.findByTestId('dalton-tier-policy-card');
    expect(card).toBeInTheDocument();
    expect(card.id).toBe('dalton-tier-policies');
    expect(screen.queryByTestId('mock-team-coverage-units-card')).toBeNull();
  });

  it('shows both DaltonTierPolicyCard and DaltonCoverageCard when colleague_sharing and coverageUnitsEnabled are true', async () => {
    mockCapabilities = new Set<CapabilityId>(['colleague_sharing']);
    mockCoverageUnitsEnabled = true;

    renderWithProviders(<DaltonCoveragePanel />);
    expect(await screen.findByTestId('dalton-tier-policy-card')).toBeInTheDocument();
    expect(screen.getByTestId('mock-team-coverage-units-card')).toBeInTheDocument();
  });

  it('previews, confirms, and commits a tier policy change with server snapshot', async () => {
    mockPreviewPolicy.mockResolvedValueOnce({
      ok: true,
      action: 'watchlist_tier_policy',
      raw_action: 'set_tier_policy',
      snapshot_id: 'snap-123',
      requires_confirmation: true,
      expected_policy_version: 1,
      expected_roster_hash: 'hash-1',
      target_tickers: ['NVDA'],
      affected_tickers: [{ ticker: 'NVDA' }],
      affected_workspaces: [{ ticker: 'NVDA', workspace_id: 'ws-1', task_changes: [] }],
      task_summary: { create_count: 0, update_count: 1, resume_count: 0, pause_count: 0 },
      card_title: 'Confirm Core Policy',
    });
    mockUpdatePolicy.mockResolvedValueOnce({
      ok: true,
      action: 'set_tier_policy',
      tier: 'core',
      policy: { ...DEFAULT_API_POLICIES.policies.core, policy_version: 2, watch_cron: '0 6-20 * * 1-5' },
      reconcile_status: 'completed',
    });

    renderWithProviders(<DaltonTierPolicyCard />);
    const presetSelect = await screen.findByTestId('tier-policy-watch-preset');
    fireEvent.change(presetSelect, { target: { value: '0 6-20 * * 1-5' } });

    const saveBtn = screen.getByTestId('tier-policy-save-btn');
    expect(saveBtn).not.toBeDisabled();
    fireEvent.click(saveBtn);

    expect(await screen.findByTestId('tier-policy-confirm-modal')).toBeInTheDocument();
    expect(mockPreviewPolicy).toHaveBeenCalledWith(
      expect.objectContaining({
        tier: 'core',
        action: 'set_tier_policy',
        watch_cron: '0 6-20 * * 1-5',
      }),
    );

    fireEvent.click(screen.getByTestId('tier-policy-confirm-btn'));
    await waitFor(() => {
      expect(mockUpdatePolicy).toHaveBeenCalledWith('core', {
        snapshot_id: 'snap-123',
        expected_policy_version: 1,
        expected_roster_hash: 'hash-1',
        override_user_pause: false,
      });
    });
  });

  it('validates 5-field advanced cron client-side and supports reset to default flow', async () => {
    renderWithProviders(<DaltonTierPolicyCard />);
    await screen.findByTestId('tier-policy-watch-preset');

    // Open advanced cron and type an invalid 4-field expression
    fireEvent.click(screen.getByTestId('tier-policy-watch-advanced-toggle'));
    const cronInput = screen.getByTestId('tier-policy-watch-cron-input');
    fireEvent.change(cronInput, { target: { value: '*/15 6-20 * *' } });
    fireEvent.click(screen.getByTestId('tier-policy-save-btn'));

    expect(await screen.findByTestId('dalton-tier-policy-notice')).toBeInTheDocument();
    expect(mockPreviewPolicy).not.toHaveBeenCalled();

    // Reset flow
    mockPreviewPolicy.mockResolvedValueOnce({
      ok: true,
      action: 'watchlist_tier_policy',
      raw_action: 'reset_tier_policy',
      snapshot_id: 'snap-reset-1',
      requires_confirmation: true,
      expected_policy_version: 1,
      expected_roster_hash: 'hash-r',
      target_tickers: [],
      affected_tickers: [],
      affected_workspaces: [],
      task_summary: {},
    });
    mockResetPolicy.mockResolvedValueOnce({
      ok: true,
      action: 'reset_tier_policy',
      tier: 'core',
      policy: { ...DEFAULT_API_POLICIES.policies.core, policy_version: 2 },
      reconcile_status: 'completed',
    });

    fireEvent.click(screen.getByTestId('tier-policy-reset-btn'));
    expect(await screen.findByTestId('tier-policy-confirm-modal')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('tier-policy-confirm-btn'));
    await waitFor(() => {
      expect(mockResetPolicy).toHaveBeenCalledWith('core', {
        snapshot_id: 'snap-reset-1',
        expected_policy_version: 1,
        expected_roster_hash: 'hash-r',
        override_user_pause: false,
      });
    });
  });
});
