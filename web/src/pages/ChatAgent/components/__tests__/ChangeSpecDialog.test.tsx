/**
 * The seed a low-disk machine opens the dialog on must be a tier the plan
 * allows. The quota that decides that arrives after the dialog opens, so the
 * dialog is rendered first without it and then handed the answer.
 *
 * `@/config/hostMode` is mocked to platform mode: a blocked tier only exists
 * there, and `web/.env` would otherwise decide the mode for the run.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '@/test/utils';
import type { ComputerProviderOption, UnsavedFile, WorkspaceQuota } from '@/types/api';

vi.mock('@/config/hostMode', () => ({
  HOST_MODE: 'platform',
  isPlatformMode: true,
  APP_ENTRY_PATH: '/app',
}));

import ChangeSpecDialog from '../ChangeSpecDialog';

const LOW_DISK = {
  name: 'Alpha Research',
  resource_tier: 'standard',
  status: 'running',
  spec_change: null,
  disk: {
    used_bytes: 9.6 * 1024 ** 3,
    total_bytes: 10 * 1024 ** 3,
    free_bytes: 0.4 * 1024 ** 3,
    measured_at: '2026-09-24T18:00:00Z',
    level: 'critical',
  },
} as const;

const NOT_ON_PLAN: WorkspaceQuota = { performance: { used: 0, limit: 0 }, max: null, always_on: null };
const ON_PLAN: WorkspaceQuota = { performance: { used: 0, limit: 2 }, max: null, always_on: null };

// As `GET /computers/providers` lists them: Daytona runs three sizes, E2B four.
const DAYTONA: ComputerProviderOption = {
  kind: 'daytona',
  is_default: true,
  allowed: null,
  tiers: [
    { tier: 'standard', cpu: 1, memory_gib: 2, disk_gib: 5 },
    { tier: 'performance', cpu: 2, memory_gib: 4, disk_gib: 10 },
    { tier: 'max', cpu: 2, memory_gib: 8, disk_gib: 10 },
  ],
};
const E2B: ComputerProviderOption = {
  kind: 'e2b',
  is_default: false,
  allowed: true,
  tiers: [...(DAYTONA.tiers ?? []), { tier: 'xlarge', cpu: 4, memory_gib: 8, disk_gib: 20 }],
};

function radio(name: string) {
  return screen.getByRole('radio', { name: new RegExp(`^${name}`) });
}

function apply() {
  return screen.getByRole('button', { name: 'Back up and change spec' });
}

describe('ChangeSpecDialog, seeding on a low-disk machine', () => {
  it('falls back to the current tier once the quota says the step up is not on the plan', async () => {
    const onSubmit = vi.fn();
    const { rerender } = renderWithProviders(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={onSubmit} busy={false} quota={undefined} />,
    );
    // Before the quota lands the step up is the seed, as on a plan that allows it.
    expect(radio('Medium')).toBeChecked();

    rerender(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={onSubmit} busy={false} quota={NOT_ON_PLAN} />,
    );
    expect(radio('Medium')).toBeDisabled();
    expect(radio('Medium')).not.toBeChecked();
    expect(radio('Small')).toBeChecked();
    expect(apply()).toBeDisabled();
    await userEvent.click(apply());
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('keeps the step up, and Apply, when the plan allows it', () => {
    renderWithProviders(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} quota={ON_PLAN} />,
    );
    expect(radio('Medium')).toBeChecked();
    expect(apply()).toBeEnabled();
  });

  it('does not overwrite a pick the user made before the quota landed', async () => {
    const { rerender } = renderWithProviders(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} quota={undefined} />,
    );
    await userEvent.click(radio('Large'));
    rerender(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} quota={NOT_ON_PLAN} />,
    );
    expect(radio('Large')).toBeChecked();
    expect(apply()).toBeEnabled();
  });
});

describe('ChangeSpecDialog, after an interrupted change', () => {
  // The row still reads the target until the next claim reverts it.
  const INTERRUPTED = {
    ...LOW_DISK,
    disk: null,
    resource_tier: 'performance',
    spec_change: {
      target_tier: 'performance',
      from_tier: 'standard',
      state: 'failed',
      error: { code: 'interrupted', message: 'interrupted', files: [] as UnsavedFile[] },
      started_at: '2026-09-24T18:00:00Z',
    },
  } as const;

  it('treats the tier it came from as current, so the target can be retried', async () => {
    renderWithProviders(
      <ChangeSpecDialog target={INTERRUPTED} onClose={() => {}} onSubmit={() => {}} busy={false} quota={ON_PLAN} />,
    );
    expect(radio('Small')).toBeChecked();
    // Current is the tier it runs at, not the one the row still names.
    expect(radio('Small')).toHaveAccessibleDescription(/^Current/);
    expect(radio('Medium')).not.toHaveAccessibleDescription(/Current/);
    expect(apply()).toBeDisabled();
    await userEvent.click(radio('Medium'));
    expect(apply()).toBeEnabled();
  });
});

describe('ChangeSpecDialog, the current tier', () => {
  const HEALTHY = { ...LOW_DISK, disk: null };

  it('reads "Current", and holds Apply until another tier is picked', async () => {
    renderWithProviders(
      <ChangeSpecDialog target={HEALTHY} onClose={() => {}} onSubmit={() => {}} busy={false} quota={ON_PLAN} />,
    );
    expect(radio('Small')).toBeChecked();
    expect(radio('Small')).toHaveAccessibleDescription(/^Current/);
    expect(radio('Large')).not.toHaveAccessibleDescription(/Current/);
    expect(apply()).toBeDisabled();

    await userEvent.click(radio('Medium'));
    expect(apply()).toBeEnabled();
    await userEvent.click(radio('Small'));
    expect(apply()).toBeDisabled();
  });

  it("keeps Current over the plan's count, which the current tier doesn't draw on", () => {
    const quota: WorkspaceQuota = { performance: { used: 1, limit: 2 }, max: null, always_on: null };
    renderWithProviders(
      <ChangeSpecDialog
        target={{ ...HEALTHY, resource_tier: 'performance' }}
        onClose={() => {}}
        onSubmit={() => {}}
        busy={false}
        quota={quota}
      />,
    );
    expect(radio('Medium')).toHaveAccessibleDescription(/^Current/);
    expect(radio('Medium')).not.toHaveAccessibleDescription(/1 of 2 left/);
  });
});

describe("ChangeSpecDialog, the provider's sizes", () => {
  const HEALTHY = { ...LOW_DISK, disk: null };
  const LARGE_LOW_DISK = { ...LOW_DISK, resource_tier: 'max' } as const;

  it('describes each size with the machine the provider runs', () => {
    renderWithProviders(
      <ChangeSpecDialog target={HEALTHY} onClose={() => {}} onSubmit={() => {}} busy={false} provider={E2B} />,
    );
    expect(radio('Large')).toHaveAccessibleDescription(/^2 vCPU\s*·\s*8 GB RAM\s*·\s*10 GB disk$/);
    expect(radio('Extra Large')).toHaveAccessibleDescription(/^4 vCPU\s*·\s*8 GB RAM\s*·\s*20 GB disk$/);
  });

  it('offers Extra Large only where the provider runs it', () => {
    renderWithProviders(
      <ChangeSpecDialog target={HEALTHY} onClose={() => {}} onSubmit={() => {}} busy={false} provider={DAYTONA} />,
    );
    expect(screen.getAllByRole('radio')).toHaveLength(3);
    expect(screen.queryByRole('radio', { name: /^Extra Large/ })).not.toBeInTheDocument();
  });

  it('seeds a low-disk Large machine on the next size only where there is one', () => {
    const { unmount } = renderWithProviders(
      <ChangeSpecDialog target={LARGE_LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} provider={DAYTONA} />,
    );
    expect(radio('Large')).toBeChecked();
    unmount();

    renderWithProviders(
      <ChangeSpecDialog target={LARGE_LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} provider={E2B} />,
    );
    expect(radio('Extra Large')).toBeChecked();
  });

  it('holds Extra Large when it is not on the plan, and says where to get it', () => {
    renderWithProviders(
      <ChangeSpecDialog
        target={LARGE_LOW_DISK}
        onClose={() => {}}
        onSubmit={() => {}}
        busy={false}
        provider={E2B}
        quota={{ ...ON_PLAN, xlarge: { used: 0, limit: 0 } }}
      />,
    );
    expect(radio('Extra Large')).toBeDisabled();
    expect(radio('Extra Large')).toHaveAccessibleDescription(/^Not on your plan/);
    expect(radio('Large')).toBeChecked();
    expect(screen.getByTestId('spec-plan-cta')).toBeInTheDocument();
  });

  it('seeds a low-disk Small machine on a Pro plan past the Medium it lacks to Large', () => {
    renderWithProviders(
      <ChangeSpecDialog
        target={LOW_DISK}
        onClose={() => {}}
        onSubmit={() => {}}
        busy={false}
        provider={DAYTONA}
        quota={{ ...NOT_ON_PLAN, max: { used: 0, limit: 1 }, xlarge: { used: 0, limit: 0 } }}
      />,
    );
    expect(radio('Medium')).toBeDisabled();
    expect(radio('Large')).toBeChecked();
    expect(apply()).toBeEnabled();
  });

  it('does not tell a plan at the top size to upgrade for the sizes it leaves out', () => {
    renderWithProviders(
      <ChangeSpecDialog
        target={HEALTHY}
        onClose={() => {}}
        onSubmit={() => {}}
        busy={false}
        provider={DAYTONA}
        quota={{ ...NOT_ON_PLAN, max: { used: 0, limit: 0 }, xlarge: { used: 0, limit: 1 } }}
      />,
    );
    expect(radio('Medium')).toBeDisabled();
    expect(radio('Large')).toBeDisabled();
    expect(screen.queryByTestId('spec-plan-cta')).not.toBeInTheDocument();
  });

  it('shows names alone and keeps the current size for a provider no longer offered', () => {
    renderWithProviders(
      <ChangeSpecDialog
        target={{ ...HEALTHY, resource_tier: 'xlarge' }}
        onClose={() => {}}
        onSubmit={() => {}}
        busy={false}
        provider={undefined}
      />,
    );
    expect(screen.getAllByRole('radio').map((r) => r.getAttribute('value'))).toEqual([
      'standard',
      'performance',
      'max',
      'xlarge',
    ]);
    expect(radio('Extra Large')).toBeChecked();
    expect(radio('Extra Large')).toHaveAccessibleDescription(/^Current$/);
    expect(radio('Small')).not.toHaveAccessibleDescription(/vCPU/);
  });
});
