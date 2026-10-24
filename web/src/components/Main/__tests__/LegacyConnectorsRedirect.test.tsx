import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router';
import Main, { LegacyConnectorsRedirect } from '../Main';

// DALTON-HOOK(hide-trading): mock Main dependencies to test /orders route redirect
vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => false }));
vi.mock('@/hooks/useSyncUserLocale', () => ({ useSyncUserLocale: () => {} }));
vi.mock('@/components/NetworkBanner/NetworkBanner', () => ({ default: () => null }));
vi.mock('@/components/ui/ContextOverflowPill', () => ({ ContextOverflowPill: () => null }));
vi.mock('@/pages/Dashboard/DashboardRouter', () => ({
  default: () => <div data-testid="dashboard-page">Dashboard</div>,
}));
vi.mock('@/pages/ChatAgent/ChatAgent', () => ({
  default: () => <div data-testid="chat-page">Chat</div>,
}));
vi.mock('@/pages/Orders/Orders', () => ({
  default: () => <div data-testid="orders-page">Orders</div>,
}));

/**
 * The /connectors alias is a compat contract, not cosmetics: OAuth
 * `return_to` values parked in Redis before the Plugins rename still point at
 * /connectors with the ?mcp_connected / ?mcp_error params the landing toast
 * reads — so the redirect must carry search and hash intact.
 */

function LocationProbe() {
  const { pathname, search, hash } = useLocation();
  return <div data-testid="probe">{pathname + search + hash}</div>;
}

function renderAt(entry: string) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/connectors" element={<LegacyConnectorsRedirect />} />
        <Route path="/plugins" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
  return screen.getByTestId('probe').textContent;
}

describe('LegacyConnectorsRedirect', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('redirects /connectors to /plugins preserving search and hash', () => {
    expect(renderAt('/connectors?mcp_connected=srv&tab=servers#frag')).toBe(
      '/plugins?mcp_connected=srv&tab=servers#frag',
    );
  });

  it('redirects a bare /connectors to a bare /plugins', () => {
    expect(renderAt('/connectors')).toBe('/plugins');
  });

  // DALTON-HOOK(hide-trading): /orders route redirects and never mounts Orders page
  it('redirects /orders away from /orders and does not mount Orders page', async () => {
    render(
      <MemoryRouter initialEntries={['/orders']}>
        <Main />
        <LocationProbe />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByTestId('probe').textContent).not.toBe('/orders');
    });
    expect(screen.queryByTestId('orders-page')).not.toBeInTheDocument();
  });

  it('redirects /orders to /chat when VITE_DALTON_MODE=1', async () => {
    vi.stubEnv('VITE_DALTON_MODE', '1');
    render(
      <MemoryRouter initialEntries={['/orders']}>
        <Main />
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument();
    expect(screen.queryByTestId('orders-page')).not.toBeInTheDocument();
    expect(screen.getByTestId('probe').textContent).toBe('/chat');
  });
});
