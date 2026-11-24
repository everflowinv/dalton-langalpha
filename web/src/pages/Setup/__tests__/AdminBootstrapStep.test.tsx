import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  isDaltonLocalAuth: true,
  authState: {
    isInitialized: true,
    isLoggedIn: false,
    needsBootstrap: true,
    bootstrapAdmin: vi.fn(),
    markBootstrapCompleted: vi.fn(),
  },
  fetchBootstrapStatus: vi.fn(),
}));

vi.mock('@/config/daltonAuth', () => ({
  get isDaltonLocalAuth() {
    return h.isDaltonLocalAuth;
  },
}));

vi.mock('@/config/hostMode', () => ({
  APP_ENTRY_PATH: '/',
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => h.authState,
}));

vi.mock('@/lib/dalton/localAuth', async () => {
  const actual = await vi.importActual<typeof import('@/lib/dalton/localAuth')>(
    '@/lib/dalton/localAuth',
  );
  return {
    ...actual,
    fetchBootstrapStatus: (...args: unknown[]) => h.fetchBootstrapStatus(...args),
  };
});

vi.mock('@/pages/Login/EdgeGrain', () => ({
  default: () => null,
}));

import AdminBootstrapStep from '../AdminBootstrapStep';
import { LocalAuthError } from '@/lib/dalton/localAuth';

function renderStep() {
  return render(
    <MemoryRouter initialEntries={['/setup/admin']}>
      <Routes>
        <Route path="/setup/admin" element={<AdminBootstrapStep />} />
        <Route path="/dashboard" element={<div data-testid="dashboard-page">dashboard</div>} />
        <Route path="/" element={<div data-testid="login-page">login</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AdminBootstrapStep', () => {
  beforeEach(() => {
    h.isDaltonLocalAuth = true;
    h.authState = {
      isInitialized: true,
      isLoggedIn: false,
      needsBootstrap: true,
      bootstrapAdmin: vi.fn().mockResolvedValue(undefined),
      markBootstrapCompleted: vi.fn(),
    };
    h.fetchBootstrapStatus.mockReset();
    h.fetchBootstrapStatus.mockResolvedValue({ needs_bootstrap: true });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('redirects away when bootstrap is already completed', () => {
    h.authState.needsBootstrap = false;
    renderStep();
    expect(screen.getByTestId('login-page')).toBeTruthy();
  });

  it('submits setup token and credentials to bootstrapAdmin and navigates to /dashboard', async () => {
    renderStep();
    fireEvent.change(screen.getByLabelText(/token|口令/i), {
      target: { value: 'setup-token-xyz' },
    });
    fireEvent.change(screen.getByLabelText(/email|邮箱/i), {
      target: { value: 'admin@example.com' },
    });
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    fireEvent.change(passwordInputs[1], { target: { value: 'StrongPassword123' } });
    fireEvent.change(passwordInputs[2], { target: { value: 'StrongPassword123' } });

    fireEvent.submit(screen.getByTestId('admin-bootstrap-form'));

    await waitFor(() => {
      expect(h.authState.bootstrapAdmin).toHaveBeenCalledWith({
        bootstrap_token: 'setup-token-xyz',
        email: 'admin@example.com',
        password: 'StrongPassword123',
        username: undefined,
        name: undefined,
      });
    });
    await waitFor(() => {
      expect(screen.getByTestId('dashboard-page')).toBeTruthy();
    });
  });

  it('closes the page permanently on HTTP 409 conflict', async () => {
    h.authState.bootstrapAdmin.mockRejectedValueOnce(new LocalAuthError('already done', 409));
    renderStep();
    fireEvent.change(screen.getByLabelText(/token|口令/i), {
      target: { value: 'setup-token-xyz' },
    });
    fireEvent.change(screen.getByLabelText(/email|邮箱/i), {
      target: { value: 'admin@example.com' },
    });
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    fireEvent.change(passwordInputs[1], { target: { value: 'StrongPassword123' } });
    fireEvent.change(passwordInputs[2], { target: { value: 'StrongPassword123' } });

    fireEvent.submit(screen.getByTestId('admin-bootstrap-form'));

    await waitFor(() => {
      expect(h.authState.markBootstrapCompleted).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('login-page')).toBeTruthy();
    });
  });
});
