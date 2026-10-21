import React, { type ReactElement } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// Enable platform auth code path (AuthProvider checks VITE_HOST_MODE)
// Must be set before the dynamic import below.
vi.stubEnv('VITE_HOST_MODE', 'platform');
vi.stubEnv('VITE_SUPABASE_URL', 'https://test.supabase.co');

// Mock supabase with a functional mock auth object
const mockGetSession = vi.fn().mockResolvedValue({ data: { session: null } });
const mockOnAuthStateChange = vi.fn().mockReturnValue({
  data: { subscription: { unsubscribe: vi.fn() } },
});

const mockSignUp = vi.fn().mockResolvedValue({ data: { user: null, session: null }, error: null });
const mockSignOut = vi.fn().mockResolvedValue({ error: null }); // DALTON-HOOK(dalton-auth): spy on signOut for 403 logout tests
const mockSignInWithOtp = vi.fn().mockResolvedValue({ data: { user: null, session: null }, error: null });
const mockResetPasswordForEmail = vi.fn().mockResolvedValue({ data: {}, error: null });
const mockResend = vi.fn().mockResolvedValue({ data: { user: null, session: null }, error: null });
const mockVerifyOtp = vi.fn().mockResolvedValue({ data: { user: null, session: null }, error: null });
const mockUpdateUser = vi.fn().mockResolvedValue({ data: { user: null }, error: null });

vi.mock('../../lib/supabase', () => ({
  supabase: {
    auth: {
      getSession: (...args: unknown[]) => mockGetSession(...args),
      onAuthStateChange: (...args: unknown[]) => mockOnAuthStateChange(...args),
      signInWithPassword: vi.fn(),
      signUp: (...args: unknown[]) => mockSignUp(...args),
      signInWithOAuth: vi.fn(),
      signOut: (...args: unknown[]) => mockSignOut(...args),
      signInWithOtp: (...args: unknown[]) => mockSignInWithOtp(...args),
      resetPasswordForEmail: (...args: unknown[]) => mockResetPasswordForEmail(...args),
      resend: (...args: unknown[]) => mockResend(...args),
      verifyOtp: (...args: unknown[]) => mockVerifyOtp(...args),
      updateUser: (...args: unknown[]) => mockUpdateUser(...args),
    },
  },
}));

// The shared token cache. AuthContext owns the only onAuthStateChange
// subscription, so it is the only thing that can keep this current.
const mockPublishSession = vi.fn();
const mockAdopt = vi.fn();
const mockClearAuthToken = vi.fn();

vi.mock('../../lib/authToken', () => ({
  publishSession: (...args: unknown[]) => mockPublishSession(...args),
  // The fenced adopter the two *async* reads go through. The real one captures
  // the signed-in user before the read and drops a reply that lands after it
  // changed; here it only has to record what was adopted.
  sessionAdopter: () => (session: unknown) => mockAdopt(session),
  clearAuthToken: () => mockClearAuthToken(),
}));

// The platform call a password change makes to sign connected apps out.
const mockApiPost = vi.fn();
vi.mock('@/api/client', () => ({
  api: { post: (...args: unknown[]) => mockApiPost(...args) },
}));

// Spy on the module-level nav stores so we can assert sign-out resets them.
const mockResetNavPanelExpansion = vi.fn();
const mockResetStableNavOrder = vi.fn();
const mockResetSharedWorkspaceThreads = vi.fn();

vi.mock('@/pages/ChatAgent/components/navExpansionStore', () => ({
  resetNavPanelExpansion: () => mockResetNavPanelExpansion(),
}));
vi.mock('@/pages/ChatAgent/hooks/useNavigationData', () => ({
  resetStableNavOrder: () => mockResetStableNavOrder(),
}));
vi.mock('@/lib/navThreadsStore', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/navThreadsStore')>()),
  resetSharedWorkspaceThreads: () => mockResetSharedWorkspaceThreads(),
}));

// DALTON-HOOK(dalton-auth): mock toast and daltonAuth switch for 403 logout & INITIAL_SESSION sync tests
const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

const daltonAuthState = { isDaltonAuth: false };
vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return daltonAuthState.isDaltonAuth;
  },
}));

// Dynamic import so mocks and env stubs are applied first
const { AuthProvider, useAuth } = await import('../AuthContext');
type AuthContextValue = ReturnType<typeof useAuth>;

function TestConsumer() {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="userId">{auth.userId ?? 'none'}</span>
      <span data-testid="isLoggedIn">{String(auth.isLoggedIn)}</span>
      <span data-testid="isInitialized">{String(auth.isInitialized)}</span>
    </div>
  );
}

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
  );
}

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetSession.mockResolvedValue({ data: { session: null } });
    mockOnAuthStateChange.mockReturnValue({
      data: { subscription: { unsubscribe: vi.fn() } },
    });
    mockApiPost.mockResolvedValue({ data: { revoked: 0 }, status: 200 });
  });

  describe('when no session exists', () => {
    it('shows isInitialized true and isLoggedIn false after bootstrap', async () => {
      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() =>
        expect(screen.getByTestId('isInitialized').textContent).toBe('true')
      );
      expect(screen.getByTestId('isLoggedIn').textContent).toBe('false');
      expect(screen.getByTestId('userId').textContent).toBe('none');
    });
  });

  describe('when a session exists', () => {
    it('shows isLoggedIn true and exposes userId', async () => {
      mockGetSession.mockResolvedValue({
        data: {
          session: {
            user: { id: 'user-abc' },
            access_token: 'tok-123',
          },
        },
      });

      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() =>
        expect(screen.getByTestId('isLoggedIn').textContent).toBe('true')
      );
      expect(screen.getByTestId('userId').textContent).toBe('user-abc');
    });
  });

  describe('useAuth', () => {
    it('throws when used outside AuthProvider', () => {
      function BadConsumer() {
        useAuth();
        return null;
      }

      const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
      expect(() => render(<BadConsumer />)).toThrow(
        'useAuth must be used within AuthProvider'
      );
      spy.mockRestore();
    });
  });

  describe('AuthProvider renders children', () => {
    it('renders child components', async () => {
      renderWithQueryClient(
        <AuthProvider>
          <div data-testid="child">Hello</div>
        </AuthProvider>
      );

      await waitFor(() =>
        expect(screen.getByTestId('child').textContent).toBe('Hello')
      );
    });
  });

  describe('email-flow methods', () => {
    // Capture the context value so the wrappers can be called directly.
    let auth: AuthContextValue;
    function Capture() {
      auth = useAuth();
      return null;
    }

    const CONFIRM_URL = window.location.origin + '/auth/confirm';
    const RESET_URL = window.location.origin + '/reset-password';

    beforeEach(async () => {
      renderWithQueryClient(
        <AuthProvider>
          <Capture />
        </AuthProvider>
      );
      await waitFor(() => expect(auth?.isInitialized).toBe(true));
    });

    it('signupWithEmail passes emailRedirectTo to the confirm route', async () => {
      await auth.signupWithEmail('a@b.co', 'secret123', 'Alice');
      expect(mockSignUp).toHaveBeenCalledWith({
        email: 'a@b.co',
        password: 'secret123',
        options: { data: { name: 'Alice' }, emailRedirectTo: CONFIRM_URL },
      });
    });

    it('sendMagicLink creates accounts and redirects to the confirm route', async () => {
      await auth.sendMagicLink('a@b.co');
      expect(mockSignInWithOtp).toHaveBeenCalledWith({
        email: 'a@b.co',
        options: { shouldCreateUser: true, emailRedirectTo: CONFIRM_URL },
      });
    });

    it('sendPasswordReset lands recovery links on the reset form', async () => {
      await auth.sendPasswordReset('a@b.co');
      expect(mockResetPasswordForEmail).toHaveBeenCalledWith('a@b.co', {
        redirectTo: RESET_URL,
      });
    });

    it('resendConfirmation re-sends the signup email', async () => {
      await auth.resendConfirmation('a@b.co');
      expect(mockResend).toHaveBeenCalledWith({
        type: 'signup',
        email: 'a@b.co',
        options: { emailRedirectTo: CONFIRM_URL },
      });
    });

    it('verifyEmailOtp forwards the token hash and type', async () => {
      await auth.verifyEmailOtp('hash-123', 'recovery');
      expect(mockVerifyOtp).toHaveBeenCalledWith({ token_hash: 'hash-123', type: 'recovery' });
    });

    it('updatePassword forwards the new password', async () => {
      await auth.updatePassword('newpass123');
      expect(mockUpdateUser).toHaveBeenCalledWith({ password: 'newpass123' });
    });

    it('updatePassword signs connected apps out once the password is changed', async () => {
      await auth.updatePassword('newpass123');
      expect(mockApiPost).toHaveBeenCalledWith('/api/auth/connected-apps/revoke-all');
    });

    it('a refused password change leaves connected apps alone', async () => {
      const refused = { data: { user: null }, error: { message: 'Password is too weak' } };
      mockUpdateUser.mockResolvedValueOnce(refused);
      await expect(auth.updatePassword('weak')).resolves.toBe(refused);
      expect(mockApiPost).not.toHaveBeenCalled();
    });

    it('the password change neither waits on nor fails with the sign-out', async () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const changed = { data: { user: null }, error: null };

      mockApiPost.mockReturnValueOnce(new Promise(() => {}));
      await expect(auth.updatePassword('newpass123')).resolves.toEqual(changed);

      mockApiPost.mockRejectedValueOnce(new Error('Network Error'));
      await expect(auth.updatePassword('newpass123')).resolves.toEqual(changed);
      await waitFor(() => expect(warn).toHaveBeenCalled());
      warn.mockRestore();
    });
  });

  describe('onAuthStateChange subscription', () => {
    it('subscribes to auth state changes on mount', async () => {
      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() =>
        expect(screen.getByTestId('isInitialized').textContent).toBe('true')
      );
      expect(mockOnAuthStateChange).toHaveBeenCalled();
    });

    // Regression: the module-level nav stores live on globalThis and survive
    // logout (no page reload), so they must be reset on sign-out or one user's
    // folders/thread lists leak into the next user's session on a shared tab.
    it('resets the module-level nav stores on sign-out', async () => {
      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() => expect(mockOnAuthStateChange).toHaveBeenCalled());

      const handler = mockOnAuthStateChange.mock.calls[0][0] as (
        event: string,
        session: unknown,
      ) => void;
      // Wrap in act(): the handler drives AuthProvider state updates, which
      // React 19 warns about if flushed outside an act() boundary.
      await act(async () => {
        handler('SIGNED_OUT', null);
      });

      expect(mockResetNavPanelExpansion).toHaveBeenCalledTimes(1);
      expect(mockResetStableNavOrder).toHaveBeenCalledTimes(1);
      expect(mockResetSharedWorkspaceThreads).toHaveBeenCalledTimes(1);
    });
  });

  // Regression #379: reading the session per outbound request turned one page
  // load into ~20 network refreshes, which exhausts Supabase's per-IP token
  // budget and ends in a forced sign-out. The cache is only correct if this
  // context keeps feeding it.
  describe('shared token cache', () => {
    const session = { user: { id: 'user-abc' }, access_token: 'tok-123', expires_at: 4102444800 };

    async function renderAndGetHandler() {
      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );
      await waitFor(() => expect(mockOnAuthStateChange).toHaveBeenCalled());
      return mockOnAuthStateChange.mock.calls[0][0] as (
        event: string,
        session: unknown,
      ) => void;
    }

    it('adopts the session synchronously, before the callback yields', async () => {
      // The callback runs under an exclusive lock, so it must stay non-async.
      // Asserting without awaiting is what pins that.
      const handler = await renderAndGetHandler();
      mockPublishSession.mockClear();

      act(() => {
        handler('TOKEN_REFRESHED', session);
      });

      expect(mockPublishSession).toHaveBeenCalledWith(session);
    });

    it('adopts the bootstrap session through the fence, not around it', async () => {
      // The bootstrap read is async, so a cross-tab sign-out can land between
      // the request and the handler. Publishing straight into the cache there
      // would put the departed user's token back and send every request after
      // it out as them, which is what `sessionAdopter` is for.
      mockGetSession.mockResolvedValue({ data: { session } });

      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() => expect(mockAdopt).toHaveBeenCalledWith(session));
      expect(mockPublishSession).not.toHaveBeenCalledWith(session);
    });

    it('wipes the cache on sign-out', async () => {
      const handler = await renderAndGetHandler();

      await act(async () => {
        handler('SIGNED_OUT', null);
      });

      expect(mockClearAuthToken).toHaveBeenCalledTimes(1);
    });
  });

  // DALTON-HOOK(dalton-auth): verify 403 sign-out gating on isDaltonAuth and INITIAL_SESSION sync
  describe('Dalton auth 403 sign-out and INITIAL_SESSION sync', () => {
    const session = {
      user: { id: 'user-dalton-1', email: 'revoked@dalton.org', user_metadata: {} },
      access_token: 'tok-dalton',
      expires_at: 4102444800,
    };

    beforeEach(() => {
      daltonAuthState.isDaltonAuth = false;
    });

    it('does not sign out on /auth/sync 403 or sync on INITIAL_SESSION when isDaltonAuth is false', async () => {
      const fetchSpy = vi
        .spyOn(globalThis, 'fetch')
        .mockResolvedValue(new Response(null, { status: 403 }));

      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );
      await waitFor(() => expect(mockOnAuthStateChange).toHaveBeenCalled());
      const handler = mockOnAuthStateChange.mock.calls[0][0] as (
        event: string,
        session: unknown,
      ) => void;

      await act(async () => {
        handler('INITIAL_SESSION', session);
      });
      expect(fetchSpy).not.toHaveBeenCalled();

      await act(async () => {
        handler('SIGNED_IN', session);
      });
      await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
      expect(mockSignOut).not.toHaveBeenCalled();
      expect(mockToast).not.toHaveBeenCalled();

      fetchSpy.mockRestore();
    });

    it('syncs on INITIAL_SESSION when isDaltonAuth is true and signs out with toast on 403', async () => {
      daltonAuthState.isDaltonAuth = true;
      const fetchSpy = vi
        .spyOn(globalThis, 'fetch')
        .mockResolvedValue(new Response(null, { status: 403 }));

      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );
      await waitFor(() => expect(mockOnAuthStateChange).toHaveBeenCalled());
      const handler = mockOnAuthStateChange.mock.calls[0][0] as (
        event: string,
        session: unknown,
      ) => void;

      await act(async () => {
        handler('INITIAL_SESSION', session);
      });

      await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(mockSignOut).toHaveBeenCalledTimes(1));
      expect(mockToast).toHaveBeenCalledWith({
        variant: 'destructive',
        title: '该账号无权访问',
      });

      fetchSpy.mockRestore();
      daltonAuthState.isDaltonAuth = false;
    });

    it('does not coalesce /auth/sync across users or sign out account B when account A in-flight sync returns 403', async () => {
      daltonAuthState.isDaltonAuth = true;

      const sessionA = {
        user: { id: 'user-a', email: 'a@outside.org', user_metadata: {} },
        access_token: 'tok-a',
        expires_at: 4102444800,
      };
      const sessionB = {
        user: { id: 'user-b', email: 'b@dalton.org', user_metadata: {} },
        access_token: 'tok-b',
        expires_at: 4102444800,
      };

      let resolveFetchA!: (value: Response) => void;
      const fetchAPromise = new Promise<Response>((resolve) => {
        resolveFetchA = resolve;
      });
      let resolveFetchB!: (value: Response) => void;
      const fetchBPromise = new Promise<Response>((resolve) => {
        resolveFetchB = resolve;
      });

      const fetchSpy = vi
        .spyOn(globalThis, 'fetch')
        .mockImplementation((_input, init) => {
          const authHeader = (init?.headers as Record<string, string> | undefined)?.Authorization;
          if (authHeader === 'Bearer tok-a') {
            return fetchAPromise;
          }
          return fetchBPromise;
        });

      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );
      await waitFor(() => expect(mockOnAuthStateChange).toHaveBeenCalled());
      const handler = mockOnAuthStateChange.mock.calls[0][0] as (
        event: string,
        session: unknown,
      ) => void;

      // 1. Account A signs in; A's /auth/sync is in flight
      await act(async () => {
        handler('SIGNED_IN', sessionA);
      });
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('userId').textContent).toBe('user-a');

      // 2. Switch to account B before A's /auth/sync returns -> B sends its own /auth/sync
      await act(async () => {
        handler('SIGNED_IN', sessionB);
      });
      expect(fetchSpy).toHaveBeenCalledTimes(2);
      expect(screen.getByTestId('userId').textContent).toBe('user-b');

      // 3. A's stale /auth/sync returns 403 -> must NOT sign out B or show toast
      await act(async () => {
        resolveFetchA(new Response(null, { status: 403 }));
        await fetchAPromise;
      });
      expect(mockSignOut).not.toHaveBeenCalled();
      expect(mockToast).not.toHaveBeenCalled();
      expect(screen.getByTestId('userId').textContent).toBe('user-b');
      expect(screen.getByTestId('isLoggedIn').textContent).toBe('true');

      // 4. B's /auth/sync succeeds cleanly
      await act(async () => {
        resolveFetchB(
          new Response(JSON.stringify({ user: { user: 'user-b' }, preferences: null }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        );
        await fetchBPromise;
      });
      expect(mockSignOut).not.toHaveBeenCalled();

      fetchSpy.mockRestore();
      daltonAuthState.isDaltonAuth = false;
    });

    it('preserves upstream single-promise deduplication when isDaltonAuth is false', async () => {
      daltonAuthState.isDaltonAuth = false;

      const sessionA = {
        user: { id: 'user-a', email: 'a@example.com', user_metadata: {} },
        access_token: 'tok-a',
        expires_at: 4102444800,
      };
      const sessionB = {
        user: { id: 'user-b', email: 'b@example.com', user_metadata: {} },
        access_token: 'tok-b',
        expires_at: 4102444800,
      };

      let resolveFetch!: (value: Response) => void;
      const fetchPromise = new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockReturnValue(fetchPromise);

      renderWithQueryClient(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );
      await waitFor(() => expect(mockOnAuthStateChange).toHaveBeenCalled());
      const handler = mockOnAuthStateChange.mock.calls[0][0] as (
        event: string,
        session: unknown,
      ) => void;

      await act(async () => {
        handler('SIGNED_IN', sessionA);
      });
      expect(fetchSpy).toHaveBeenCalledTimes(1);

      await act(async () => {
        handler('SIGNED_IN', sessionB);
      });
      // When isDaltonAuth is false, upstream single _syncPromise coalesces concurrent syncs
      expect(fetchSpy).toHaveBeenCalledTimes(1);

      await act(async () => {
        resolveFetch(
          new Response(JSON.stringify({ preferences: { theme: 'dark' } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        );
        await fetchPromise;
      });

      fetchSpy.mockRestore();
    });
  });
});
