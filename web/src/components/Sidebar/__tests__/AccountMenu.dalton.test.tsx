import type React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import type { useAuth } from '@/contexts/AuthContext';
import type { useUser } from '@/hooks/useUser';
import type { useReportsEnabled } from '@/hooks/useReportsEnabled';

const logoutMock = vi.fn<ReturnType<typeof useAuth>['logout']>();

vi.mock('@/hooks/useReportsEnabled', () => ({
  useReportsEnabled: vi.fn<typeof useReportsEnabled>(() => false),
}));
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ userId: 'u-1', logout: logoutMock }),
}));
vi.mock('@/hooks/useUser', () => ({
  useUser: vi.fn<() => Partial<ReturnType<typeof useUser>>>(() => ({
    user: { user_id: 'u-1', name: 'Analyst', email: 'analyst@everflow.test' },
  })),
}));
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSeparator: () => <hr />,
  DropdownMenuSub: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSubContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe('AccountMenu help links in Dalton build', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('points What’s new, Changelog, and Documentation to /help/* with no langalpha.ai URLs', async () => {
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { default: AccountMenu } = await import('../AccountMenu');
    renderWithProviders(<AccountMenu variant="row" />);

    const hrefs = screen.getAllByRole('link').map((el) => el.getAttribute('href'));
    expect(hrefs).toContain('/help/whats-new/');
    expect(hrefs).toContain('/help/changelog/');
    expect(hrefs).toContain('/help/');
    for (const href of hrefs) {
      expect(href).not.toContain('langalpha.ai');
    }
  });
});
