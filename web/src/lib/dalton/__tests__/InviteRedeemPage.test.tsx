import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import InviteRedeemPage from '../InviteRedeemPage';
import LoginPage from '@/pages/Login/LoginPage';

const h = vi.hoisted(() => ({
  isDaltonAuth: true,
  checkDaltonInviteToken: vi.fn(),
  redeemDaltonInviteToken: vi.fn(),
  loginWithEmail: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock('@/config/daltonAuth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/config/daltonAuth')>();
  return {
    ...actual,
    get isDaltonAuth() {
      return h.isDaltonAuth;
    },
  };
});

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  checkDaltonInviteToken: (...args: unknown[]) => h.checkDaltonInviteToken(...args),
  redeemDaltonInviteToken: (...args: unknown[]) => h.redeemDaltonInviteToken(...args),
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    loginWithEmail: h.loginWithEmail,
    signupWithEmail: vi.fn(),
    loginWithProvider: vi.fn(),
    sendMagicLink: vi.fn(),
    sendPasswordReset: vi.fn(),
    resendConfirmation: vi.fn(),
  }),
}));

vi.mock('@/pages/Login/MarketScanlines', () => ({
  default: () => <div data-testid="market-scanlines" />,
}));

vi.mock('@/pages/Login/EdgeGrain', () => ({
  default: () => <div data-testid="edge-grain" />,
}));

function renderWithProviders(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe('InviteRedeemPage & LoginPage username completion', () => {
  beforeEach(() => {
    h.isDaltonAuth = true;
    h.checkDaltonInviteToken.mockReset();
    h.redeemDaltonInviteToken.mockReset();
    h.loginWithEmail.mockReset();
    h.loginWithEmail.mockResolvedValue({ data: { session: {} }, error: null });
  });

  it('reads token from URL fragment, strips it via replaceState, renders checking state, then invalid message when expired', async () => {
    let resolveCheck!: (val: { valid: boolean; reason: string | null }) => void;
    h.checkDaltonInviteToken.mockReturnValue(
      new Promise((res) => {
        resolveCheck = res;
      }),
    );

    const replaceStateSpy = vi.spyOn(window.history, 'replaceState');

    renderWithProviders(
      <MemoryRouter initialEntries={['/invite#tok-expired']}>
        <Routes>
          <Route path="/invite" element={<InviteRedeemPage />} />
        </Routes>
      </MemoryRouter>,
    );

    // Strips #tok-expired immediately
    expect(replaceStateSpy).toHaveBeenCalled();

    // State 1: checking
    expect(h.checkDaltonInviteToken).toHaveBeenCalledWith('tok-expired');
    expect(
      screen.getByText(/正在验证邀请链接|Checking invite link/i),
    ).toBeInTheDocument();

    // State 2: invalid / expired
    resolveCheck({ valid: false, reason: 'expired' });
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/邀请链接无效或已过期，请联系管理员|invalid or has expired/i);
    replaceStateSpy.mockRestore();
  });

  it('renders registration form when invite is valid, validates inputs, redeems via POST body, and auto-logs in', async () => {
    h.checkDaltonInviteToken.mockResolvedValue({ valid: true, reason: null });
    h.redeemDaltonInviteToken.mockResolvedValue({
      ok: true,
      username: 'alice_01',
      email: 'alice_01@team.dalton.local',
    });

    renderWithProviders(
      <MemoryRouter initialEntries={['/invite#tok-valid']}>
        <Routes>
          <Route path="/invite" element={<InviteRedeemPage />} />
          <Route path="/dashboard" element={<div data-testid="dashboard-page">Dashboard</div>} />
        </Routes>
      </MemoryRouter>,
    );

    // State 3: valid form
    const usernameInput = await screen.findByLabelText(/用户名|Username/i);
    const passwordInputs = screen.getAllByPlaceholderText(/至少 10 位字符|At least 10 characters|再次输入密码|Re-enter your password/i);
    const passwordInput = passwordInputs[0];
    const confirmInput = passwordInputs[1];
    const submitBtn = screen.getByRole('button', { name: /注册并登录|Create account & sign in/i });

    // Rejects invalid username (uppercase)
    fireEvent.change(usernameInput, { target: { value: 'AliceUpper' } });
    fireEvent.change(passwordInput, { target: { value: 'validpassword123' } });
    fireEvent.change(confirmInput, { target: { value: 'validpassword123' } });
    fireEvent.click(submitBtn);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(h.redeemDaltonInviteToken).not.toHaveBeenCalled();

    // Rejects mismatched passwords
    fireEvent.change(usernameInput, { target: { value: 'alice_01' } });
    fireEvent.change(passwordInput, { target: { value: 'validpassword123' } });
    fireEvent.change(confirmInput, { target: { value: 'differentpassword123' } });
    fireEvent.click(submitBtn);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(h.redeemDaltonInviteToken).not.toHaveBeenCalled();

    // Valid submission -> redeems token, calls loginWithEmail, navigates to /dashboard
    fireEvent.change(confirmInput, { target: { value: 'validpassword123' } });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(h.redeemDaltonInviteToken).toHaveBeenCalledWith({
        token: 'tok-valid',
        username: 'alice_01',
        password: 'validpassword123',
      });
    });
    await waitFor(() => {
      expect(h.loginWithEmail).toHaveBeenCalledWith(
        'alice_01@team.dalton.local',
        'validpassword123',
      );
    });
    expect(await screen.findByTestId('dashboard-page')).toBeInTheDocument();
  });

  it('auto-appends @team.dalton.local on LoginPage in Dalton mode when input has no @ and preserves full emails', async () => {
    h.isDaltonAuth = true;
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: true,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    renderWithProviders(
      <MemoryRouter initialEntries={['/login']}>
        <LoginPage />
      </MemoryRouter>,
    );

    expect(screen.getByText(/邮箱或用户名|Email or username/i)).toBeInTheDocument();
    const identifierInput = screen.getByPlaceholderText(/用户名或 you@example.com|username or you@example.com/i);
    expect(identifierInput).toHaveAttribute('type', 'text');
    expect(identifierInput).toHaveAttribute('autocomplete', 'username');
    const passwordInput = screen.getByPlaceholderText(/你的密码|Your password/i);
    const signInBtn = screen.getByRole('button', { name: /^(登录|Sign in)$/i });

    // Bare username -> auto-completes domain
    fireEvent.change(identifierInput, { target: { value: 'alice_01' } });
    fireEvent.change(passwordInput, { target: { value: 'validpassword123' } });
    fireEvent.click(signInBtn);

    await waitFor(() => {
      expect(h.loginWithEmail).toHaveBeenCalledWith(
        'alice_01@team.dalton.local',
        'validpassword123',
      );
    });

    // Full email -> passed unchanged
    h.loginWithEmail.mockClear();
    fireEvent.change(identifierInput, { target: { value: 'pm@dalton.test' } });
    fireEvent.click(signInBtn);

    await waitFor(() => {
      expect(h.loginWithEmail).toHaveBeenCalledWith('pm@dalton.test', 'validpassword123');
    });
  });

  it('keeps standard email input and placeholder on LoginPage when isDaltonAuth is false', async () => {
    h.isDaltonAuth = false;
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: true,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    renderWithProviders(
      <MemoryRouter initialEntries={['/login']}>
        <LoginPage />
      </MemoryRouter>,
    );

    const continueEmailBtn = screen.getByRole('button', {
      name: /使用邮箱继续|Continue with email/i,
    });
    fireEvent.click(continueEmailBtn);

    const emailInput = screen.getByPlaceholderText('you@example.com');
    expect(emailInput).toHaveAttribute('type', 'email');
    expect(emailInput).toHaveAttribute('autocomplete', 'email');
  });
});
