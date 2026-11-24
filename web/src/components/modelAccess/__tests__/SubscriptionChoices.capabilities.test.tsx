import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';

const oauthStatusSpy = vi.fn();

vi.mock('@/hooks/useConfiguredProviders', () => ({
  useOAuthStatus: (provider: string, options?: { enabled?: boolean }) => {
    oauthStatusSpy(provider, options);
    return {
      data: options?.enabled === false
        ? undefined
        : { connected: false, state: 'disconnected', can_sign_in: true },
      isError: false,
    };
  },
}));

afterEach(() => {
  oauthStatusSpy.mockReset();
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('SubscriptionChoices capability gating', () => {
  it('renders subscription sign-ins immediately on first paint in Everflow builds', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'everflow');
    const { SubscriptionChoices } = await import('../SubscriptionChoices');
    renderWithProviders(
      <SubscriptionChoices
        planGroups={[]}
        configured={new Set()}
        onPickSignIn={vi.fn()}
        onPickPlan={vi.fn()}
      />,
    );
    expect(screen.getByText('ChatGPT')).toBeInTheDocument();
    expect(screen.getByText('Claude')).toBeInTheDocument();
    expect(oauthStatusSpy).toHaveBeenCalledWith('claude-oauth', { enabled: true });
  });

  it('hides all subscription OAuth sign-ins when native_oauth_sync is disabled in Core builds', async () => {
    vi.stubEnv('VITE_DALTON_PROFILE', 'core');
    const { SubscriptionChoices } = await import('../SubscriptionChoices');
    renderWithProviders(
      <SubscriptionChoices
        planGroups={[]}
        configured={new Set()}
        onPickSignIn={vi.fn()}
        onPickPlan={vi.fn()}
      />,
    );
    expect(screen.queryByText('ChatGPT')).toBeNull();
    expect(screen.queryByText('Claude')).toBeNull();
    expect(screen.queryByText('Google Antigravity')).toBeNull();
    expect(screen.queryByText('Muse Code')).toBeNull();
    expect(screen.queryByText('Grok (SuperGrok)')).toBeNull();
    expect(oauthStatusSpy).toHaveBeenCalledWith('claude-oauth', { enabled: false });
  });
});
