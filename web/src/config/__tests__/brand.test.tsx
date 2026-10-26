import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import enUS from '@/locales/en-US.json';
import zhCN from '@/locales/zh-CN.json';

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', preference: 'dark', setTheme: vi.fn(), toggleTheme: vi.fn() }),
}));
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    isLoggedIn: false,
    isInitialized: true,
    loginWithEmail: vi.fn(),
    signupWithEmail: vi.fn(),
    loginWithProvider: vi.fn(),
    sendMagicLink: vi.fn(),
    sendPasswordReset: vi.fn(),
    resendConfirmation: vi.fn(),
  }),
}));
vi.mock('@/hooks/useConfiguredProviders', () => ({
  useConfiguredProviders: () => ({ hasAny: false }),
}));
vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: null }),
}));
vi.mock('@/hooks/useSetupGate', () => ({
  skipSetup: vi.fn(),
}));
vi.mock('@/pages/Login/MarketScanlines', () => ({ default: () => null }));
vi.mock('@/pages/Login/EdgeGrain', () => ({ default: () => null }));
vi.mock('@/components/nav/useNavActive', () => ({
  useNavActive: () => () => false,
}));
vi.mock('@/components/nav/useNavItems', () => ({
  useNavItems: () => [],
}));
vi.mock('@/pages/ChatAgent/components/NavigationPanel', () => ({
  default: () => <div data-testid="nav-panel" />,
}));
vi.mock('@/pages/ChatAgent/components/NavDisplayOptions', () => ({
  default: () => <div data-testid="nav-display-options" />,
}));
vi.mock('@/pages/ChatAgent/hooks/useCreateWorkspace', () => ({
  useCreateWorkspace: () => vi.fn(),
}));
vi.mock('@/pages/ChatAgent/hooks/useNavTreeProps', () => ({
  useNavTreeProps: () => ({}),
}));
vi.mock('@/components/Sidebar/AccountMenu', () => ({
  default: () => <div data-testid="account-menu" />,
}));
vi.mock('@/components/Sidebar/useChatRoute', () => ({
  useChatRoute: () => ({ currentWorkspaceId: null, currentThreadId: null }),
}));
vi.mock('@/pages/ChatAgent/components/sidebarAgentsBridge', () => ({
  useSidebarAgents: () => null,
}));
vi.mock('@/hooks/useWorkspaceLabel', () => ({
  useWorkspaceLabel: () => () => 'Workspace',
}));

const catalogs = {
  'en-US': { translation: enUS },
  'zh-CN': { translation: zhCN },
};

afterEach(async () => {
  cleanup();
  vi.unstubAllEnvs();
  vi.resetModules();
  const { default: i18n, initI18n } = await import('@/i18n');
  await initI18n(catalogs);
  await i18n.changeLanguage('en-US');
});

describe('brand configuration (VITE_BRAND_NAME & VITE_BRAND_MARK)', () => {
  it('defaults BRAND_NAME to LangAlpha and BRAND_MARK to langalpha when env vars are unset', async () => {
    vi.stubEnv('VITE_BRAND_NAME', '');
    vi.stubEnv('VITE_BRAND_MARK', '');
    vi.resetModules();

    const { BRAND_NAME, BRAND_MARK, faviconHref } = await import('@/config/brand');
    expect(BRAND_NAME).toBe('LangAlpha');
    expect(BRAND_MARK).toBe('langalpha');
    expect(faviconHref('light')).toBe('/logo-favicon.svg');
    expect(faviconHref('dark')).toBe('/logo-favicon-dark.svg');
  });

  it('resolves BRAND_NAME to Everflow and BRAND_MARK to everflow when configured', async () => {
    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { BRAND_NAME, BRAND_MARK, faviconHref } = await import('@/config/brand');
    expect(BRAND_NAME).toBe('Everflow');
    expect(BRAND_MARK).toBe('everflow');
    expect(faviconHref('light')).toBe('/everflow/favicon.svg');
    expect(faviconHref('dark')).toBe('/everflow/favicon-dark.svg');
  });

  it('renders original images, alt text, and 60x60 LogoIcon when both env vars are unset', async () => {
    vi.stubEnv('VITE_BRAND_NAME', '');
    vi.stubEnv('VITE_BRAND_MARK', '');
    vi.resetModules();

    const { default: AppSidebar } = await import('@/components/Sidebar/AppSidebar');
    const { ShareTopBar } = await import('@/pages/SharedChat/ShareTopBar');
    const { default: SetupWizard } = await import('@/pages/Setup/SetupWizard');
    const { default: ShareUnavailable } = await import('@/pages/SharedChat/ShareUnavailable');
    const { default: LoginPage } = await import('@/pages/Login/LoginPage');

    const { container } = render(
      <MemoryRouter>
        <AppSidebar
          collapsed={false}
          onToggleCollapse={() => {}}
          width={260}
          onWidthChange={() => {}}
        />
        <ShareTopBar name="report.html" />
        <SetupWizard />
        <ShareUnavailable />
        <LoginPage />
      </MemoryRouter>,
    );

    expect(container.querySelector('.sidebar-wordmark')?.textContent).toBe('LangAlpha');
    expect(container.querySelector('.share-topbar-brand')?.textContent).toBe('LangAlpha');

    const sidebarImg = container.querySelector('.sidebar-brand img') as HTMLImageElement | null;
    expect(sidebarImg).toBeTruthy();
    expect(sidebarImg?.getAttribute('alt')).toBe('');

    const langAlphaImgs = screen.getAllByAltText('LangAlpha') as HTMLImageElement[];
    expect(langAlphaImgs).toHaveLength(2);
    expect(langAlphaImgs[0].classList.contains('h-7')).toBe(true);
    expect(langAlphaImgs[1].classList.contains('h-8')).toBe(true);

    const loginSvg = container.querySelector('svg.login-page__logo-icon');
    expect(loginSvg?.getAttribute('viewBox')).toBe('0 0 60 60');
    expect(container.querySelector('.login-page__title')?.textContent).toBe('LangAlpha');
  });

  it('renders Everflow in AppSidebar, ShareTopBar, SetupWizard, and ShareUnavailable when configured', async () => {
    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { default: AppSidebar } = await import('@/components/Sidebar/AppSidebar');
    const { ShareTopBar } = await import('@/pages/SharedChat/ShareTopBar');
    const { default: SetupWizard } = await import('@/pages/Setup/SetupWizard');
    const { default: ShareUnavailable } = await import('@/pages/SharedChat/ShareUnavailable');

    const { container } = render(
      <MemoryRouter>
        <AppSidebar
          collapsed={false}
          onToggleCollapse={() => {}}
          width={260}
          onWidthChange={() => {}}
        />
        <ShareTopBar name="report.html" />
        <SetupWizard />
        <ShareUnavailable />
      </MemoryRouter>,
    );

    expect(container.querySelector('.sidebar-wordmark')?.textContent).toBe('Everflow');
    expect(container.querySelector('.share-topbar-brand')?.textContent).toBe('Everflow');
    expect(screen.getAllByText('Everflow')).toHaveLength(2);
    expect(container.querySelector('.sidebar-brand svg')?.getAttribute('viewBox')).toBe('0 0 56 56');
    expect(screen.queryByAltText('LangAlpha')).toBeNull();
  });

  it('switches the Legal home navigation link while keeping legal body text unchanged', async () => {
    // The in-app PrivacyPolicy page is gone (upstream moved the policy to an
    // external VITE_PRIVACY_URL link rendered inside Legal), so only Legal's
    // own back link is branded here.
    vi.stubEnv('VITE_BRAND_NAME', '');
    vi.resetModules();

    const { default: LegalDefault } = await import('@/pages/Legal/Legal');

    const { unmount } = render(
      <MemoryRouter>
        <LegalDefault />
      </MemoryRouter>,
    );
    expect(screen.getAllByText('← Back to LangAlpha')).toHaveLength(1);
    unmount();

    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.resetModules();

    const { default: LegalEverflow } = await import('@/pages/Legal/Legal');

    const { container } = render(
      <MemoryRouter>
        <LegalEverflow />
      </MemoryRouter>,
    );
    expect(screen.getAllByText('← Back to Everflow')).toHaveLength(1);
    expect(container.textContent).toContain('hosted LangAlpha service');
    expect(container.textContent).toContain('langalpha repository');
    expect(container.innerHTML).toContain('github.com/ginlix/langalpha');
  });

  it('interpolates {{brand}} in en-US and zh-CN locale strings when unset and when VITE_BRAND_NAME=Everflow', async () => {
    vi.stubEnv('VITE_BRAND_NAME', '');
    vi.resetModules();

    const { default: i18nDefault, initI18n: initDefault } = await import('@/i18n');
    await initDefault(catalogs);
    await i18nDefault.changeLanguage('en-US');
    expect(i18nDefault.t('workspace.welcomeTitle')).toBe('Welcome to LangAlpha');
    expect(i18nDefault.t('shareLink.goToLangAlpha')).toBe('Go to LangAlpha');

    await i18nDefault.changeLanguage('zh-CN');
    expect(i18nDefault.t('workspace.welcomeTitle')).toBe('欢迎使用 LangAlpha');
    expect(i18nDefault.t('shareLink.goToLangAlpha')).toBe('前往 LangAlpha');

    vi.stubEnv('VITE_BRAND_NAME', 'Everflow');
    vi.resetModules();

    const { default: i18n, initI18n } = await import('@/i18n');
    await initI18n(catalogs);
    await i18n.changeLanguage('en-US');
    expect(i18n.t('workspace.welcomeTitle')).toBe('Welcome to Everflow');
    expect(i18n.t('shareLink.goToLangAlpha')).toBe('Go to Everflow');

    await i18n.changeLanguage('zh-CN');
    expect(i18n.t('workspace.welcomeTitle')).toBe('欢迎使用 Everflow');
    expect(i18n.t('shareLink.goToLangAlpha')).toBe('前往 Everflow');
  });
});
