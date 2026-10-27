/**
 * The low-disk banner's call to action: a spec change only to a size with
 * more disk that the plan allows, and the way to a bigger plan when the plan
 * allows none.
 *
 * `@/config/hostMode` is mocked to platform mode: a size the plan leaves out
 * only exists there, and `web/.env` would otherwise decide the mode for the run.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';

import { renderWithProviders } from '@/test/utils';
import type { Computer, ComputerTierSpec, WorkspaceQuota } from '@/types/api';

vi.mock('@/config/hostMode', () => ({
  HOST_MODE: 'platform',
  isPlatformMode: true,
  APP_ENTRY_PATH: '/app',
}));

vi.mock('../../utils/api', () => ({
  getComputerProviders: vi.fn(),
  getWorkspaceQuota: vi.fn(),
}));

import { getComputerProviders, getWorkspaceQuota } from '../../utils/api';
import { DiskWarning } from '../DiskWarning';

const mockProviders = getComputerProviders as Mock;
const mockQuota = getWorkspaceQuota as Mock;

// As `GET /computers/providers` lists them: Daytona runs three sizes, E2B four.
const BASE_SPECS: ComputerTierSpec[] = [
  { tier: 'standard', cpu: 1, memory_gib: 2, disk_gib: 5 },
  { tier: 'performance', cpu: 2, memory_gib: 4, disk_gib: 10 },
  { tier: 'max', cpu: 2, memory_gib: 8, disk_gib: 10 },
];

const FREE: WorkspaceQuota = {
  performance: { used: 0, limit: 0 },
  max: { used: 0, limit: 0 },
  xlarge: { used: 0, limit: 0 },
  always_on: null,
};
const MAX_PLAN: WorkspaceQuota = { ...FREE, xlarge: { used: 0, limit: 1 } };

function machine(overrides: Partial<Computer> = {}): Computer {
  return {
    computer_id: 'c-1',
    user_id: 'u-1',
    kind: 'daytona',
    name: 'Alpha Research',
    status: 'running',
    resource_tier: 'standard',
    is_always_on: false,
    is_primary: true,
    root_dir: '/home/workspace',
    ssh: null,
    disk: {
      used_bytes: 4_500_000_000,
      total_bytes: 5_000_000_000,
      free_bytes: 500_000_000,
      measured_at: '2026-10-23T18:00:00Z',
      level: 'warning',
    },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockProviders.mockResolvedValue({
    providers: [
      { kind: 'daytona', is_default: true, allowed: null, tiers: BASE_SPECS },
      {
        kind: 'e2b',
        is_default: false,
        allowed: true,
        tiers: [...BASE_SPECS, { tier: 'xlarge', cpu: 4, memory_gib: 8, disk_gib: 20 }],
      },
    ],
  });
});

// Both fetches: until the sizes land, the next size up is assumed to have
// more disk, so a banner judged on the quota alone can offer a change.
async function settled() {
  await waitFor(() => {
    expect(mockQuota).toHaveBeenCalled();
    expect(mockProviders).toHaveBeenCalled();
  });
  await act(async () => {});
}

describe('DiskWarning, the call to action', () => {
  it('points a Free plan to a bigger plan rather than a spec it cannot pick', async () => {
    mockQuota.mockResolvedValue(FREE);
    renderWithProviders(<DiskWarning computer={machine()} />);

    expect(await screen.findByTestId('disk-plan-cta')).toHaveTextContent('Need a bigger spec than your plan includes?');
    expect(screen.queryByRole('button', { name: 'Change spec' })).not.toBeInTheDocument();
  });

  it('offers nothing to a Medium on Daytona, where no size has more disk', async () => {
    mockQuota.mockResolvedValue({ ...FREE, max: { used: 0, limit: 1 } });
    renderWithProviders(<DiskWarning computer={machine({ resource_tier: 'performance' })} />);
    await settled();

    expect(screen.getByTestId('disk-warning')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Change spec' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('disk-plan-cta')).not.toBeInTheDocument();
  });

  it('offers the change to a Medium on E2B, where Extra Large has more disk', async () => {
    mockQuota.mockResolvedValue(MAX_PLAN);
    renderWithProviders(<DiskWarning computer={machine({ kind: 'e2b', resource_tier: 'performance' })} />);

    expect(await screen.findByRole('button', { name: 'Change spec' })).toBeInTheDocument();
  });

  it('does not tell a Max plan on Daytona to upgrade', async () => {
    mockQuota.mockResolvedValue(MAX_PLAN);
    renderWithProviders(<DiskWarning computer={machine()} />);
    await settled();

    expect(screen.queryByRole('button', { name: 'Change spec' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('disk-plan-cta')).not.toBeInTheDocument();
  });
});
