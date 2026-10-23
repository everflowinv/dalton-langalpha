import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { DaltonProviderStatusItem } from '../../../utils/daltonApi';

vi.mock('../../../hooks/useProviderStatus', () => ({
  useProviderProbe: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

import { ProviderRow } from '../ProviderRow';

const baseMuseItem: DaltonProviderStatusItem = {
  provider: 'muse-oauth',
  display_name: 'Meta Muse',
  access_type: 'oauth',
  billing: 'subscription',
  enabled: true,
  configured: true,
  credential: {
    source: 'oauth',
    email: 'analyst@dalton.test',
    plan_type: 'Pro',
  },
  recent_errors: [],
  errors_24h: 0,
  calls_24h: 3,
  used_by_roles: [],
  warnings: [],
  tos_risk: 'high',
  default_probe_model: 'muse-spark-1.3-oauth',
};

describe('ProviderRow (muse-oauth subscription & usage)', () => {
  it('renders subscription status and 5h/weekly usage in local time when present', () => {
    const windowResetIso = '2026-10-23T12:00:00Z';
    const weeklyResetIso = '2026-10-29T00:00:00Z';
    const expectedWindowLocal = new Date(windowResetIso).toLocaleString().replace(/\s+/g, " ");
    const expectedWeeklyLocal = new Date(weeklyResetIso).toLocaleString().replace(/\s+/g, " ");

    const item: DaltonProviderStatusItem = {
      ...baseMuseItem,
      is_subs_active: true,
      subs_tier_name: 'Pro',
      subs_usage: {
        window: {
          used_percent: 25,
          window_duration_mins: 300,
          resets_at: windowResetIso,
        },
        weekly: {
          used_percent: 60.5,
          resets_at: weeklyResetIso,
        },
        tier: 'Pro',
      },
    };

    render(<ProviderRow item={item} />);

    const usageLine = screen.getByTestId('dalton-muse-subs-usage');
    expect(usageLine).toHaveTextContent('订阅：有效 (Pro)');
    expect(usageLine).toHaveTextContent(
      `5 小时窗口已用 25%，${expectedWindowLocal} 重置`,
    );
    expect(usageLine).toHaveTextContent(
      `本周已用 60.5%，${expectedWeeklyLocal} 重置`,
    );
  });

  it('omits the subscription/usage row when fields are missing', () => {
    render(<ProviderRow item={baseMuseItem} />);
    expect(screen.queryByTestId('dalton-muse-subs-usage')).toBeNull();
  });
});
