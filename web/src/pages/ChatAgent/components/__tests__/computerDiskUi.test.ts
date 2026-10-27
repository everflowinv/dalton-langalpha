/**
 * Which size a low disk points to. Medium and Large share a 10 GB disk, so the
 * next size up is not always more disk, and the plan may leave a size out.
 *
 * `@/config/hostMode` is mocked to platform mode: a size the plan leaves out
 * only exists there, and `web/.env` would otherwise decide the mode for the run.
 */
import { describe, it, expect, vi } from 'vitest';

import type { ComputerProviderOption, ComputerTierSpec, WorkspaceQuota } from '@/types/api';

vi.mock('@/config/hostMode', () => ({
  HOST_MODE: 'platform',
  isPlatformMode: true,
  APP_ENTRY_PATH: '/app',
}));

import { diskUpgradeTier, largerDiskTiers } from '../computerDiskUi';
import { offeredTiers } from '../tierUi';

// As `GET /computers/providers` lists them: Daytona runs three sizes, E2B four.
const BASE_SPECS: ComputerTierSpec[] = [
  { tier: 'standard', cpu: 1, memory_gib: 2, disk_gib: 5 },
  { tier: 'performance', cpu: 2, memory_gib: 4, disk_gib: 10 },
  { tier: 'max', cpu: 2, memory_gib: 8, disk_gib: 10 },
];
const DAYTONA: ComputerProviderOption = { kind: 'daytona', is_default: true, allowed: null, tiers: BASE_SPECS };
const E2B: ComputerProviderOption = {
  kind: 'e2b',
  is_default: false,
  allowed: true,
  tiers: [...BASE_SPECS, { tier: 'xlarge', cpu: 4, memory_gib: 8, disk_gib: 20 }],
};

const PRO: WorkspaceQuota = {
  performance: { used: 0, limit: 0 },
  max: { used: 0, limit: 1 },
  xlarge: { used: 0, limit: 0 },
  always_on: null,
};
const FREE: WorkspaceQuota = {
  performance: { used: 0, limit: 0 },
  max: { used: 0, limit: 0 },
  xlarge: { used: 0, limit: 0 },
  always_on: null,
};

describe('largerDiskTiers', () => {
  it('passes over a size up that adds no disk', () => {
    expect(largerDiskTiers('performance', offeredTiers(DAYTONA))).toEqual([]);
    expect(largerDiskTiers('performance', offeredTiers(E2B))).toEqual(['xlarge']);
    expect(largerDiskTiers('standard', offeredTiers(E2B))).toEqual(['performance', 'max', 'xlarge']);
  });

  it('falls back to the next size up when the server lists no specs', () => {
    expect(largerDiskTiers('performance', offeredTiers({ ...DAYTONA, tiers: undefined }))).toEqual(['max']);
    expect(largerDiskTiers('max', offeredTiers(undefined))).toEqual([]);
  });
});

describe('diskUpgradeTier', () => {
  it('points Medium on E2B to Extra Large, and Medium on Daytona nowhere', () => {
    expect(diskUpgradeTier('performance', offeredTiers(E2B), undefined)).toBe('xlarge');
    expect(diskUpgradeTier('performance', offeredTiers(DAYTONA), undefined)).toBeNull();
  });

  it('points a Pro plan on Small past the Medium it lacks to Large', () => {
    expect(diskUpgradeTier('standard', offeredTiers(DAYTONA), PRO)).toBe('max');
  });

  it('points nowhere when the plan allows no size with more disk', () => {
    expect(diskUpgradeTier('standard', offeredTiers(E2B), FREE)).toBeNull();
  });
});
