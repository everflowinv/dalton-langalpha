import { describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { ReportFailure } from '@/components/ui/error-banner';
import AccountMenu from '../AccountMenu';

const state = vi.hoisted(() => ({ reportsEnabled: false }));

vi.mock('@/hooks/useReportsEnabled', () => ({
  useReportsEnabled: () => state.reportsEnabled,
  useReportsState: () => state.reportsEnabled,
}));
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ userId: 'u-1', logout: vi.fn() }),
}));
vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: { user_id: 'u-1', name: 'Test User', email: 'test@dalton.test' } }),
}));
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuItem: ({
    children,
    onClick,
  }: {
    children: React.ReactNode;
    onClick?: () => void;
  }) => (
    <button type="button" role="menuitem" onClick={onClick}>
      {children}
    </button>
  ),
  DropdownMenuSeparator: () => <hr />,
  DropdownMenuLabel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe('DALTON-HOOK(problem-reports-off): frontend report entry gating', () => {
  it('hides report menu item and error-banner report action when reports are disabled', () => {
    state.reportsEnabled = false;
    renderWithProviders(
      <>
        <AccountMenu variant="row" />
        <ReportFailure
          error={{ kind: 'internal', message: 'Internal failure', runId: 'run-1' }}
          threadId="t-1"
        />
      </>,
    );
    expect(screen.queryByText(/Report a problem|问题反馈/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Report this|上报此问题/i)).not.toBeInTheDocument();
  });

  it('shows report menu item and error-banner report action when reports are enabled', () => {
    state.reportsEnabled = true;
    renderWithProviders(
      <>
        <AccountMenu variant="row" />
        <ReportFailure
          error={{ kind: 'internal', message: 'Internal failure', runId: 'run-1' }}
          threadId="t-1"
        />
      </>,
    );
    expect(screen.getByText(/Report a problem|问题反馈/i)).toBeInTheDocument();
    expect(screen.getByText(/Report this|上报此问题/i)).toBeInTheDocument();
  });
});
