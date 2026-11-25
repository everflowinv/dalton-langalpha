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

describe('useCapabilities build-time initial state and server authority', () => {
  it('renders Everflow defaults immediately on first paint without waiting when VITE_DALTON_PROFILE is not core', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'everflow');
    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    // Never resolves during first paint: Everflow build must not wait or flash loading state.
    let resolveFetch!: (val: unknown) => void;
    mockApiGet.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve as (val: unknown) => void;
        }) as ReturnType<typeof api.get>,
    );

    const { useCapabilities } = await import('../useCapabilities');

    function Probe() {
      const caps = useCapabilities();
      return (
        <div>
          <span data-testid="loading">{String(caps.isLoading)}</span>
          <span data-testid="profile">{caps.profile}</span>
          <span data-testid="brand">{caps.brandName}</span>
          <span data-testid="feishu">{String(caps.hasCapability('channel_feishu'))}</span>
          <span data-testid="data-jobs">{String(caps.hasCapability('data_jobs'))}</span>
          <span data-testid="transcription">{String(caps.hasCapability('transcription'))}</span>
        </div>
      );
    }

    render(<Probe />, { wrapper: createWrapper() });

    // Immediate first paint assertions (no waitFor):
    expect(screen.getByTestId('loading')).toHaveTextContent('false');
    expect(screen.getByTestId('profile')).toHaveTextContent('everflow');
    expect(screen.getByTestId('brand')).toHaveTextContent('Everflow');
    expect(screen.getByTestId('feishu')).toHaveTextContent('true');
    expect(screen.getByTestId('data-jobs')).toHaveTextContent('true');
    expect(screen.getByTestId('transcription')).toHaveTextContent('false');

    // Once backend responds, server payload becomes authoritative.
    resolveFetch({
      data: {
        profile: 'everflow',
        capabilities: ['channel_feishu', 'transcription'],
        brand_name: 'Everflow',
      },
    });

    await waitFor(() => {
      expect(screen.getByTestId('data-jobs')).toHaveTextContent('false');
      expect(screen.getByTestId('transcription')).toHaveTextContent('true');
    });
  });

  it('starts with all capabilities disabled and loading placeholder in Core builds, then applies server payload', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    vi.stubEnv('VITE_BRAND_NAME', '');
    vi.stubEnv('VITE_BRAND_MARK', 'langalpha');
    vi.resetModules();

    let resolveFetch!: (val: unknown) => void;
    mockApiGet.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve as (val: unknown) => void;
        }) as ReturnType<typeof api.get>,
    );

    const { useCapabilities } = await import('../useCapabilities');

    function Probe() {
      const caps = useCapabilities();
      return (
        <div>
          <span data-testid="loading">{String(caps.isLoading)}</span>
          <span data-testid="profile">{caps.profile}</span>
          <span data-testid="brand">{caps.brandName}</span>
          <span data-testid="feishu">{String(caps.hasCapability('channel_feishu'))}</span>
          <span data-testid="archive">{String(caps.hasCapability('conversation_archive'))}</span>
          <span data-testid="data-jobs">{String(caps.hasCapability('data_jobs'))}</span>
        </div>
      );
    }

    render(<Probe />, { wrapper: createWrapper() });

    // Core build starts closed with lightweight loading state.
    expect(screen.getByTestId('loading')).toHaveTextContent('true');
    expect(screen.getByTestId('profile')).toHaveTextContent('core');
    expect(screen.getByTestId('brand')).toHaveTextContent('Dalton');
    expect(screen.getByTestId('feishu')).toHaveTextContent('false');
    expect(screen.getByTestId('archive')).toHaveTextContent('false');
    expect(screen.getByTestId('data-jobs')).toHaveTextContent('false');

    // Server enables one capability explicitly -> UI follows server truth.
    resolveFetch({
      data: {
        profile: 'core',
        capabilities: ['channel_feishu'],
        brand_name: 'Acme Research',
      },
    });

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
      expect(screen.getByTestId('brand')).toHaveTextContent('Acme Research');
      expect(screen.getByTestId('feishu')).toHaveTextContent('true');
      expect(screen.getByTestId('archive')).toHaveTextContent('false');
    });
  });

  it('defaults tickerLogosEnabled by build profile before load and follows server ticker_logos after load', async () => {
    // 1. Core build: false before load, follows server true once loaded.
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    vi.stubEnv('VITE_BRAND_NAME', '');
    vi.stubEnv('VITE_BRAND_MARK', 'langalpha');
    vi.resetModules();

    let resolveCore!: (val: unknown) => void;
    mockApiGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCore = resolve as (val: unknown) => void;
        }) as ReturnType<typeof api.get>,
    );

    const coreMod = await import('../useCapabilities');

    function CoreProbe() {
      const caps = coreMod.useCapabilities();
      return <span data-testid="ticker-logos">{String(caps.tickerLogosEnabled)}</span>;
    }

    const coreView = render(<CoreProbe />, { wrapper: createWrapper() });
    expect(screen.getByTestId('ticker-logos')).toHaveTextContent('false');

    resolveCore({
      data: {
        profile: 'core',
        capabilities: [],
        ticker_logos: true,
      },
    });
    await waitFor(() => {
      expect(screen.getByTestId('ticker-logos')).toHaveTextContent('true');
    });
    coreView.unmount();

    // 2. Everflow build: true before load, follows server value (false when false or omitted) once loaded.
    vi.stubEnv('VITE_DALTON_PROFILE', 'everflow');
    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    let resolveEverflow!: (val: unknown) => void;
    mockApiGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveEverflow = resolve as (val: unknown) => void;
        }) as ReturnType<typeof api.get>,
    );

    const efMod = await import('../useCapabilities');

    function EfProbe() {
      const caps = efMod.useCapabilities();
      return <span data-testid="ticker-logos">{String(caps.tickerLogosEnabled)}</span>;
    }

    render(<EfProbe />, { wrapper: createWrapper() });
    expect(screen.getByTestId('ticker-logos')).toHaveTextContent('true');

    resolveEverflow({
      data: {
        profile: 'everflow',
        capabilities: [],
        ticker_logos: false,
      },
    });
    await waitFor(() => {
      expect(screen.getByTestId('ticker-logos')).toHaveTextContent('false');
    });
  });

  it('falls back to build-time defaults without throwing when no QueryClientProvider is mounted', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'everflow');
    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.resetModules();

    const { useCapabilities } = await import('../useCapabilities');

    function BareProbe() {
      const caps = useCapabilities();
      return (
        <div>
          <span data-testid="loading">{String(caps.isLoading)}</span>
          <span data-testid="oauth">{String(caps.hasCapability('native_oauth_sync'))}</span>
          <span data-testid="profile">{caps.profile}</span>
        </div>
      );
    }

    render(<BareProbe />);
    expect(screen.getByTestId('loading')).toHaveTextContent('false');
    expect(screen.getByTestId('oauth')).toHaveTextContent('true');
    expect(screen.getByTestId('profile')).toHaveTextContent('everflow');
    expect(mockApiGet).not.toHaveBeenCalled();
  });
});
