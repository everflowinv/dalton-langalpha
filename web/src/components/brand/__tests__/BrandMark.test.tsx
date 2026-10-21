import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

vi.mock('@/pages/Login/MarketScanlines', () => ({ default: () => null }));
vi.mock('@/pages/Login/EdgeGrain', () => ({ default: () => null }));
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    loginWithEmail: vi.fn(),
    signupWithEmail: vi.fn(),
    loginWithProvider: vi.fn(),
    sendMagicLink: vi.fn(),
    sendPasswordReset: vi.fn(),
    resendConfirmation: vi.fn(),
  }),
}));

afterEach(() => {
  cleanup();
  document.head.querySelectorAll('link[rel="icon"]').forEach((el) => el.remove());
  localStorage.clear();
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('BrandMark, EverflowMark & favicon switching', () => {
  it('renders LangAlphaMark paths when VITE_BRAND_MARK is unset', async () => {
    vi.stubEnv('VITE_BRAND_MARK', '');
    vi.resetModules();

    const { BrandMark } = await import('@/components/brand/BrandMark');
    const { KindTile } = await import('@/pages/ChatAgent/components/mcp/KindTile');

    const { container } = render(
      <div>
        <BrandMark className="test-mark" />
        <KindTile kind="langalpha" />
      </div>,
    );

    const paths = Array.from(container.querySelectorAll('svg path')).map((p) =>
      p.getAttribute('d'),
    );
    expect(paths.some((d) => d?.startsWith('M38.0312'))).toBe(true);
    expect(paths).not.toContain('M14 24h19.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z');
  });

  it('renders EverflowMark paths when VITE_BRAND_MARK=everflow', async () => {
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { BrandMark } = await import('@/components/brand/BrandMark');
    const { KindTile } = await import('@/pages/ChatAgent/components/mcp/KindTile');

    const { container } = render(
      <div>
        <BrandMark className="test-mark" />
        <KindTile kind="langalpha" />
      </div>,
    );

    const svgs = container.querySelectorAll('svg');
    expect(svgs).toHaveLength(2);
    expect(svgs[0].classList.contains('test-mark')).toBe(true);
    expect(svgs[0].getAttribute('viewBox')).toBe('0 0 56 56');

    const paths = Array.from(svgs[0].querySelectorAll('path')).map((p) =>
      p.getAttribute('d'),
    );
    expect(paths).toEqual([
      'M14 10.5h26.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z',
      'M14 24h19.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z',
      'M14 37.5h26.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z',
    ]);
  });

  it('switches LoginPage mark and title when VITE_BRAND_NAME=Everflow and VITE_BRAND_MARK=everflow', async () => {
    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { default: LoginPage } = await import('@/pages/Login/LoginPage');

    const { container } = render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    expect(container.querySelector('.login-page__title')?.textContent).toBe('Everflow');
    const logoSvg = container.querySelector('svg.login-page__logo-icon');
    expect(logoSvg).toBeTruthy();
    const paths = Array.from(logoSvg!.querySelectorAll('path')).map((p) =>
      p.getAttribute('d'),
    );
    expect(paths).toEqual([
      'M14 10.5h26.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z',
      'M14 24h19.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z',
      'M14 37.5h26.6c.8 0 1.3.9.9 1.6l-3 5c-.4.6-1 .9-1.7.9H14c-.6 0-1-.4-1-1v-5.5c0-.6.4-1 1-1Z',
    ]);
  });

  it('updates link[rel="icon"] via ThemeProvider using Everflow favicons when VITE_BRAND_MARK=everflow', async () => {
    const link = document.createElement('link');
    link.setAttribute('rel', 'icon');
    document.head.appendChild(link);
    localStorage.setItem('theme', 'dark');

    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { ThemeProvider } = await import('@/contexts/ThemeContext');
    render(
      <ThemeProvider>
        <div />
      </ThemeProvider>,
    );

    expect(link.getAttribute('href')).toBe('/everflow/favicon-dark.svg');
  });
});
