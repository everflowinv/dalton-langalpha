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
import type { UnsavedFile, WorkspaceQuota } from '@/types/api';

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
    expect(radio('Performance')).toBeChecked();

    rerender(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={onSubmit} busy={false} quota={NOT_ON_PLAN} />,
    );
    expect(radio('Performance')).toBeDisabled();
    expect(radio('Performance')).not.toBeChecked();
    expect(radio('Standard')).toBeChecked();
    expect(apply()).toBeDisabled();
    await userEvent.click(apply());
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('keeps the step up, and Apply, when the plan allows it', () => {
    renderWithProviders(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} quota={ON_PLAN} />,
    );
    expect(radio('Performance')).toBeChecked();
    expect(apply()).toBeEnabled();
  });

  it('does not overwrite a pick the user made before the quota landed', async () => {
    const { rerender } = renderWithProviders(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} quota={undefined} />,
    );
    await userEvent.click(radio('Max'));
    rerender(
      <ChangeSpecDialog target={LOW_DISK} onClose={() => {}} onSubmit={() => {}} busy={false} quota={NOT_ON_PLAN} />,
    );
    expect(radio('Max')).toBeChecked();
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
    expect(radio('Standard')).toBeChecked();
    // Current is the tier it runs at, not the one the row still names.
    expect(radio('Standard')).toHaveAccessibleDescription(/^Current/);
    expect(radio('Performance')).not.toHaveAccessibleDescription(/Current/);
    expect(apply()).toBeDisabled();
    await userEvent.click(radio('Performance'));
    expect(apply()).toBeEnabled();
  });
});

describe('ChangeSpecDialog, the current tier', () => {
  const HEALTHY = { ...LOW_DISK, disk: null };

  it('reads "Current", and holds Apply until another tier is picked', async () => {
    renderWithProviders(
      <ChangeSpecDialog target={HEALTHY} onClose={() => {}} onSubmit={() => {}} busy={false} quota={ON_PLAN} />,
    );
    expect(radio('Standard')).toBeChecked();
    expect(radio('Standard')).toHaveAccessibleDescription(/^Current/);
    expect(radio('Max')).not.toHaveAccessibleDescription(/Current/);
    expect(apply()).toBeDisabled();

    await userEvent.click(radio('Performance'));
    expect(apply()).toBeEnabled();
    await userEvent.click(radio('Standard'));
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
    expect(radio('Performance')).toHaveAccessibleDescription(/^Current/);
    expect(radio('Performance')).not.toHaveAccessibleDescription(/1 of 2 left/);
  });
});
