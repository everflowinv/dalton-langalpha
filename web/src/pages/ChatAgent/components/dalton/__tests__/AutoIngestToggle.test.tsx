import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '@/api/client';

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const mockFetchAutoIngest = vi.fn();
const mockUpdateAutoIngest = vi.fn();

vi.mock('../../../utils/api/workspaces', () => ({
  fetchAutoIngest: (...args: unknown[]) => mockFetchAutoIngest(...args),
  updateAutoIngest: (...args: unknown[]) => mockUpdateAutoIngest(...args),
}));

const mockApiGet = vi.mocked(api.get);

function createWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.clearAllMocks();
  vi.resetModules();
});

describe('AutoIngestToggle capability gating (Batch 3)', () => {
  it('stays hidden in Core when neither company_wiki nor sales_notes is enabled', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    vi.resetModules();

    mockApiGet.mockResolvedValue({
      data: { profile: 'core', capabilities: ['researchdata_bridge'] },
    } as Awaited<ReturnType<typeof api.get>>);
    mockFetchAutoIngest.mockResolvedValue(false);

    const { AutoIngestToggle } = await import('../AutoIngestToggle');
    render(<AutoIngestToggle workspaceId="ws-core-1" />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledTimes(1);
    });
    expect(screen.queryByTestId('dalton-auto-ingest-toggle')).toBeNull();
    expect(mockFetchAutoIngest).not.toHaveBeenCalled();
  });

  it('renders in Core when company_wiki alone is enabled (without researchdata_bridge)', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    vi.resetModules();

    mockApiGet.mockResolvedValue({
      data: { profile: 'core', capabilities: ['company_wiki'] },
    } as Awaited<ReturnType<typeof api.get>>);
    mockFetchAutoIngest.mockResolvedValue(true);

    const { AutoIngestToggle } = await import('../AutoIngestToggle');
    render(<AutoIngestToggle workspaceId="ws-wiki-1" />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByTestId('dalton-auto-ingest-toggle')).toBeTruthy();
    });
    expect(mockFetchAutoIngest).toHaveBeenCalledWith('ws-wiki-1');
  });

  it('renders when sales_notes alone is enabled', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    vi.resetModules();

    mockApiGet.mockResolvedValue({
      data: { profile: 'core', capabilities: ['sales_notes'] },
    } as Awaited<ReturnType<typeof api.get>>);
    mockFetchAutoIngest.mockResolvedValue(false);

    const { AutoIngestToggle } = await import('../AutoIngestToggle');
    render(<AutoIngestToggle workspaceId="ws-sales-1" />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByTestId('dalton-auto-ingest-toggle')).toBeTruthy();
    });
    expect(mockFetchAutoIngest).toHaveBeenCalledWith('ws-sales-1');
  });
});
