/**
 * Unauthenticated visits to thread or workspace file deep links preserve the
 * validated in-app path + `?file=` query across login (both AuthContext and
 * standalone LocalAuthProvider sign-in flows), reject open redirects, and
 * resolve gateway-emitted `?file=` paths identically to web `agentPaths`.
 */
import React, { useCallback, useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AuthContext, _localDevValue } from '@/contexts/AuthContext';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { clearAuthToken } from '@/lib/authToken';
import { LocalAuthProvider } from '@/lib/dalton/LocalAuthProvider';
import { queryKeys } from '@/lib/queryKeys';
import { setStorageUser, userLocalStorage } from '@/lib/userStorage';
import { computeAgentArtifactRouting } from '@/pages/ChatAgent/utils/agentArtifactRouting';
import {
  normalizeAgentPath,
  type ComputerFolders,
} from '@/pages/ChatAgent/utils/agentPaths';

vi.mock('@/hooks/useAppearance', () => ({ useSyncAppearance: () => {} }));
vi.mock('@/hooks/useSetupGate', () => ({ setupSkipped: () => false }));
vi.mock('@/hooks/useOnboardingRoute', () => ({
  useOnboardingRoute: () => ({ kind: 'done' }),
  useShellOnboardingRoute: () => ({ kind: 'done' }),
}));
vi.mock('@/pages/Login/LoginPage', async () => {
  const { useLocation: useRouterLocation } = await import('react-router');
  const { useAuth } = await import('@/contexts/AuthContext');
  return {
    default: function MockLoginPage() {
      const loc = useRouterLocation();
      const { loginWithEmail } = useAuth();
      return (
        <div>
          <div data-testid="login-screen">
            {loc.pathname}
            {loc.search}
          </div>
          <button
            type="button"
            onClick={() => {
              void loginWithEmail('analyst@example.com', 'password123');
            }}
          >
            Sign in
          </button>
        </div>
      );
    },
  };
});
vi.mock('@/components/Main/Main', () => ({
  default: function MockMain() {
    const loc = useLocation();
    return (
      <div
        data-testid="main-screen"
        data-file-param={new URLSearchParams(loc.search).get('file') ?? ''}
      >
        {loc.pathname}
        {loc.search}
        {loc.hash}
      </div>
    );
  },
  preloadRouteChunk: () => {},
}));
vi.mock('@/pages/SharedChat/SharePage', () => ({
  default: function MockSharePage() {
    const loc = useLocation();
    return (
      <div data-testid="share-screen">
        {loc.pathname}
        {loc.search}
        {loc.hash}
      </div>
    );
  },
}));
vi.mock('@/pages/Legal/Legal', () => ({
  default: function MockLegalPage() {
    const loc = useLocation();
    return <div data-testid="legal-screen">{loc.pathname}</div>;
  },
}));
vi.mock('@/pages/Monitors/PublicMonitorPage', () => ({
  default: function MockPublicMonitorPage() {
    const loc = useLocation();
    return <div data-testid="public-monitor-screen">{loc.pathname}</div>;
  },
}));
vi.mock('@/pages/Setup/SetupWizard', () => ({
  default: function MockSetupWizard() {
    const loc = useLocation();
    return (
      <div data-testid="setup-screen">
        {loc.pathname}
        {loc.search}
      </div>
    );
  },
}));
vi.mock('@/pages/Onboarding', () => ({
  OnboardingProvider: ({ children }: { children: React.ReactNode }) => children,
  OnboardingHostGate: () => null,
}));
vi.mock('@/lib/threadLifecycle/ThreadLifecycleFeed', () => ({ ThreadLifecycleFeed: () => null }));
vi.mock('@/components/WarmHome', () => ({ WarmHome: () => null }));
vi.mock('@/pages/Plugins/components/ConnectConsentHost', () => ({ ConnectConsentHost: () => null }));
vi.mock('@/pages/Plugins/components/ConnectLandingHost', () => ({ ConnectLandingHost: () => null }));
vi.mock('@/components/Sidebar/AppSidebar', () => ({ default: () => null }));

import App from '../App';

function TestAuthProvider({
  initialLoggedIn,
  children,
}: {
  initialLoggedIn: boolean;
  children: React.ReactNode;
}) {
  const [loggedIn, setLoggedIn] = useState(initialLoggedIn);
  const loginWithEmail = useCallback(async () => {
    setLoggedIn(true);
  }, []);
  return (
    <AuthContext
      value={{
        ..._localDevValue,
        isInitialized: true,
        isLoggedIn: loggedIn,
        userId: loggedIn ? 'u-1' : null,
        needsBootstrap: false,
        loginWithEmail,
      }}
    >
      {children}
    </AuthContext>
  );
}

function renderAppAt(
  initialEntry: string,
  initialLoggedIn = false,
  options?: { queryClient?: QueryClient },
) {
  const qc = options?.queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <TestAuthProvider initialLoggedIn={initialLoggedIn}>
          <MemoryRouter initialEntries={[initialEntry]}>
            <App />
          </MemoryRouter>
        </TestAuthProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  localStorage.clear();
  sessionStorage.clear();
  setStorageUser(null);
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = vi.fn();
  }
});

afterEach(() => {
  clearAuthToken();
  setStorageUser(null);
  localStorage.clear();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe('deep-link preservation across login', () => {
  it('preserves thread file deep link (/chat/t/:id?file=...) across login and restores it after sign-in', async () => {
    const deepLink = '/chat/t/11111111-2222-3333-4444-555555555555?file=results%2FQ3%20%E6%8A%A5%E5%91%8A.md';
    renderAppAt(deepLink, false);

    const loginScreen = await screen.findByTestId('login-screen');
    expect(loginScreen.textContent).toBe(
      `/?redirect=${encodeURIComponent(deepLink)}`,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe(deepLink);
    expect(mainScreen.getAttribute('data-file-param')).toBe('results/Q3 报告.md');
  });

  it('preserves workspace file deep link (/chat/:workspaceId?file=...) across login and restores it after sign-in', async () => {
    const deepLink = '/chat/20cc68e8-d057-41f4-9bb1-57aa8d310704?file=results%2Fa%2Bb.md';
    renderAppAt(deepLink, false);

    const loginScreen = await screen.findByTestId('login-screen');
    expect(loginScreen.textContent).toBe(
      `/?redirect=${encodeURIComponent(deepLink)}`,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe(deepLink);
    expect(mainScreen.getAttribute('data-file-param')).toBe('results/a+b.md');
  });

  it('restores rooted file deep link through real LocalAuthProvider loginWithEmail flow', async () => {
    const deepLink =
      '/chat/t/11111111-2222-3333-4444-555555555555?file=%2Fhome%2Fworkspace%2FResearch%2Fresults%2Freport.md';
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith('/api/v1/dalton/bootstrap/status')) {
        return new Response(JSON.stringify({ needs_bootstrap: false }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/v1/dalton/auth/local/refresh')) {
        return new Response(JSON.stringify({ detail: 'not signed in' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/v1/dalton/auth/local/login')) {
        return new Response(
          JSON.stringify({
            access_token: 'hdr.eyJzdWIiOiJ1LWxvY2FsLTEifQ.sig',
            token_type: 'bearer',
            expires_in: 900,
            user: {
              user_id: 'u-local-1',
              email: 'analyst@example.com',
              username: null,
              tier: 'member',
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <LocalAuthProvider>
            <MemoryRouter initialEntries={[deepLink]}>
              <App />
            </MemoryRouter>
          </LocalAuthProvider>
        </ThemeProvider>
      </QueryClientProvider>,
    );

    const loginScreen = await screen.findByTestId('login-screen');
    expect(loginScreen.textContent).toBe(
      `/?redirect=${encodeURIComponent(deepLink)}`,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe(deepLink);
    expect(mainScreen.getAttribute('data-file-param')).toBe(
      '/home/workspace/Research/results/report.md',
    );
  });

  it.each([
    '/?redirect=https://evil.example/phish',
    '/?redirect=//evil.example/phish',
    '/?redirect=//in-app.invalid/chat',
    '/?redirect=/\\evil.example/phish',
    '/?redirect=%2F%3Floop%3D1',
  ])('rejects open redirect %s and falls back to /dashboard after login', async (entry) => {
    renderAppAt(entry, true);

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe('/dashboard');
  });

  it('preserves settings deep links (/settings?tab=...#anchor) across login and restores query + hash', async () => {
    const deepLink = '/settings?tab=teamChannels#feishu';
    renderAppAt(deepLink, false);

    const loginScreen = await screen.findByTestId('login-screen');
    expect(loginScreen.textContent).toBe(`/?redirect=${encodeURIComponent(deepLink)}`);

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe(deepLink);
  });

  it('preserves protected /setup/* deep links across login', async () => {
    const deepLink = '/setup/model?step=default';
    renderAppAt(deepLink, false);

    const loginScreen = await screen.findByTestId('login-screen');
    expect(loginScreen.textContent).toBe(`/?redirect=${encodeURIComponent(deepLink)}`);

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const setupScreen = await screen.findByTestId('setup-screen');
    expect(setupScreen.textContent).toBe(deepLink);
  });

  it.each([
    ['/s/public-thread-token', 'share-screen'],
    ['/a/public-app-token?path=sub%2Findex.html', 'share-screen'],
    ['/m/public-pulse', 'public-monitor-screen'],
    ['/legal', 'legal-screen'],
  ])('renders pre-login public route %s without redirecting to login', async (route, testId) => {
    renderAppAt(route, false);

    const screenEl = await screen.findByTestId(testId);
    expect(screenEl.textContent).toBe(route);
    expect(screen.queryByTestId('login-screen')).toBeNull();
  });

  it('restores a members-only /s/:shareToken link after sign-in via ?redirect=', async () => {
    const shareLink = '/s/private-share-token?as=member#turn-2';
    renderAppAt(`/?redirect=${encodeURIComponent(shareLink)}`, false);

    const loginScreen = await screen.findByTestId('login-screen');
    expect(loginScreen.textContent).toBe(`/?redirect=${encodeURIComponent(shareLink)}`);

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const shareScreen = await screen.findByTestId('share-screen');
    expect(shareScreen.textContent).toBe(shareLink);
  });

  it('resumes a valid saved front page tab when page_tabs is enabled and no ?redirect= is given', async () => {
    setStorageUser('u-1');
    userLocalStorage.setItem(
      'app-page-tabs',
      JSON.stringify({
        activeId: 'tab-1',
        tabs: [{ id: 'tab-1', path: '/market' }],
      }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(queryKeys.features.list(), [{ key: 'page_tabs', enabled: true }]);

    renderAppAt('/', true, { queryClient: qc });

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe('/market');
  });

  it('prefers explicit ?redirect= deep link over a saved front page tab when page_tabs is enabled', async () => {
    setStorageUser('u-1');
    userLocalStorage.setItem(
      'app-page-tabs',
      JSON.stringify({
        activeId: 'tab-1',
        tabs: [{ id: 'tab-1', path: '/market' }],
      }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(queryKeys.features.list(), [{ key: 'page_tabs', enabled: true }]);

    const deepLink = '/chat/t/11111111-2222-3333-4444-555555555555?file=results%2Freport.md';
    renderAppAt(`/?redirect=${encodeURIComponent(deepLink)}`, true, { queryClient: qc });

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe(deepLink);
  });

  it.each([
    '/..//evil.example/phish',
    '/%2F%2Fevil.example/phish',
    '/./?loop=1',
  ])('falls back to /dashboard when saved page tab path is unsafe (%s)', async (unsafePath) => {
    setStorageUser('u-1');
    userLocalStorage.setItem(
      'app-page-tabs',
      JSON.stringify({
        activeId: 'tab-1',
        tabs: [{ id: 'tab-1', path: unsafePath }],
      }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(queryKeys.features.list(), [{ key: 'page_tabs', enabled: true }]);

    renderAppAt('/', true, { queryClient: qc });

    const mainScreen = await screen.findByTestId('main-screen');
    expect(mainScreen.textContent).toBe('/dashboard');
  });
});

describe('gateway ?file= encoding and rooted-path parity with agentPaths', () => {
  const researchFolders: ComputerFolders = {
    dirName: 'Research',
    siblings: [{ workspaceId: '20cc68e8-d057-41f4-9bb1-57aa8d310704', dirName: 'NVDA' }],
  };

  it('routes rooted sandbox, sibling, custom-root, and former-root ?file= values to the exact workspace file', () => {
    const fileDest = (raw: string, folders: ComputerFolders | null) => {
      const dest = computeAgentArtifactRouting(raw, undefined, folders);
      expect(dest.kind).toBe('panel');
      if (dest.kind !== 'panel' || dest.target.kind !== 'file' || !('path' in dest.target)) {
        throw new Error(`expected panel file destination, got ${JSON.stringify(dest)}`);
      }
      return { workspaceId: dest.workspaceId, path: dest.target.path };
    };

    // 1. Current workspace folder folded off when rooted `/home/workspace/Research/...` is preserved
    const ownFile = new URLSearchParams('?file=%2Fhome%2Fworkspace%2FResearch%2Fresults%2Freport.md').get('file')!;
    const ownRouting = fileDest(ownFile, researchFolders);
    expect(ownRouting.workspaceId).toBeNull();
    expect(normalizeAgentPath(ownRouting.path)).toBe('results/report.md');

    // 2. Sibling workspace folder routed to sibling workspace id instead of nested subdirectory
    const siblingFile = new URLSearchParams('?file=%2Fhome%2Fworkspace%2FNVDA%2Fresults%2Freport.md').get('file')!;
    const siblingRouting = fileDest(siblingFile, researchFolders);
    expect(siblingRouting.workspaceId).toBe('20cc68e8-d057-41f4-9bb1-57aa8d310704');
    expect(normalizeAgentPath(siblingRouting.path)).toBe('results/report.md');

    // 3. Rootless / flat sandbox workspace still opens `results/report.md`
    const flatFile = new URLSearchParams('?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md').get('file')!;
    const flatRouting = fileDest(flatFile, null);
    expect(normalizeAgentPath(flatRouting.path)).toBe('results/report.md');

    // 4. Custom computer root and former (moved-off) root resolve via ComputerFolders
    const customFolders: ComputerFolders = {
      rootDir: '/home/someone/langalpha',
      otherRoots: ['/srv/old'],
      leftRoots: ['/srv/old'],
      dirName: 'Research',
      siblings: [],
    };
    const customFile = new URLSearchParams('?file=%2Fhome%2Fsomeone%2Flangalpha%2FResearch%2Fresults%2Freport.md').get('file')!;
    expect(fileDest(customFile, customFolders).path).toBe('results/report.md');

    const movedFile = new URLSearchParams('?file=%2Fsrv%2Fold%2FResearch%2Fresults%2Freport.md').get('file')!;
    expect(fileDest(movedFile, customFolders).path).toBe('results/report.md');

    // 5. `+`, Chinese, spaces, and parentheses round-trip identically via URLSearchParams
    expect(new URLSearchParams('?file=results%2Fa%2Bb.md').get('file')).toBe('results/a+b.md');
    expect(
      new URLSearchParams('?file=results%2F%E4%B8%AD%E6%96%87%20%E6%8A%A5%E5%91%8A%20%281%29.pdf').get('file'),
    ).toBe('results/中文 报告 (1).pdf');
  });
});
