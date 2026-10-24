import { describe, expect, it, vi } from 'vitest';
import { renderHookWithProviders } from '@/test/utils';
import { catalogServer, httpCatalogServer } from '@/test/factories';
import type { CatalogServer } from '@/pages/ChatAgent/utils/api';
import { parseDetail } from '../../../utils/detailParam';
import { ACCOUNTS, readSection } from '../sections';
import { useShellData } from '../useShellData';

// DALTON-HOOK(hide-trading): brokerages section and summary are hidden in Dalton
let servers: CatalogServer[] = [];
const useBrokeragesSpy = vi.fn(() => ({ data: [{ name: 'ibkr' }, { name: 'robinhood' }] }));
vi.mock('@/hooks/useMcpServers', () => ({
  useMcpCatalog: () => ({ data: { servers } }),
  useBuiltinMcpServers: () => ({ data: { servers: [] } }),
  useBrokerages: () => useBrokeragesSpy(),
}));
vi.mock('@/hooks/usePlugins', () => ({ usePlugins: () => ({ data: undefined }) }));
vi.mock('@/hooks/useSkills', () => ({ useSkills: () => ({ data: undefined }) }));
vi.mock('@/hooks/useUserVault', () => ({ useUserVaultSecrets: () => ({ data: undefined }) }));

describe('the rail and sections hide brokerages in Dalton', () => {
  it('does not fetch brokerages and returns null for brokerages summary', () => {
    useBrokeragesSpy.mockClear();
    servers = [
      httpCatalogServer({ name: 'ibkr', oauth_status: null }),
      catalogServer({ name: 'robinhood', oauth_status: null }),
    ];
    const { result } = renderHookWithProviders(() => useShellData());
    expect(result.current.brokerages).toBeNull();
    expect(useBrokeragesSpy).not.toHaveBeenCalled();
  });

  it('excludes brokerages from ACCOUNTS and falls back ?tab=brokerages and ?detail=brokerage:* to plugins', () => {
    expect(ACCOUNTS).not.toContain('brokerages');
    expect(readSection(new URLSearchParams('tab=brokerages'))).toBe('plugins');
    expect(readSection(new URLSearchParams('detail=brokerage:ibkr'))).toBe('plugins');
    expect(parseDetail(new URLSearchParams('detail=brokerage:ibkr'))).toBeNull();
  });
});
