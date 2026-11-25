import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import {
  CommandPaletteProvider,
  paletteKeyShortcuts,
  paletteShortcut,
} from '../CommandPaletteProvider';
import AppSidebar from '@/components/Sidebar/AppSidebar';
import BottomTabBar from '@/components/BottomTabBar/BottomTabBar';

// DALTON-HOOK(global-search): test ⌘K / ⌘⇧K shortcuts, Monaco yield, sidebar buttons, and mobile BottomTabBar entry.
vi.mock('../CommandPalette', () => ({
  default: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div role="dialog" data-testid="mock-command-palette">
        <button type="button" onClick={onClose}>
          Close palette
        </button>
      </div>
    ) : null,
}));

vi.mock('@/pages/Onboarding/GettingStartedGate', () => ({
  default: () => null,
  GettingStartedGate: () => null,
}));

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', setTheme: vi.fn(), toggleTheme: vi.fn() }),
}));

vi.mock('@/hooks/useHomeWorkspace', () => ({
  useHomeWorkspaceId: () => '99999999-9999-4999-8999-999999999999',
  resolveChatWorkspace: vi.fn(),
  HomeRowIcon: () => <span />,
}));

vi.mock('@/pages/ChatAgent/components/NavigationPanel', () => ({
  default: () => <div />,
}));

vi.mock('@/pages/ChatAgent/components/NavDisplayOptions', () => ({
  default: () => <div />,
}));

vi.mock('@/pages/ChatAgent/hooks/useCreateWorkspace', () => ({
  useCreateWorkspace: () => vi.fn(),
}));

const STABLE_NAV_TREE_PROPS = {};
vi.mock('@/pages/ChatAgent/hooks/useNavTreeProps', () => ({
  useNavTreeProps: () => STABLE_NAV_TREE_PROPS,
}));

vi.mock('@/components/Sidebar/AccountMenu', () => ({
  default: () => <div />,
}));

vi.mock('@/pages/ChatAgent/components/dalton/TeamSharing', () => ({
  TeamWorkspacesSection: () => null,
  useTeamWorkspaces: () => ({ data: undefined }),
}));

vi.mock('@/components/nav/useNavItems', () => ({
  useNavItems: () => [],
}));

vi.mock('@/components/nav/useNavActive', () => ({
  useNavActive: () => () => false,
}));

function setNavigatorPlatform(platform: string) {
  Object.defineProperty(navigator, 'platform', {
    configurable: true,
    value: platform,
  });
  Object.defineProperty(navigator, 'userAgentData', {
    configurable: true,
    value: undefined,
  });
  Object.defineProperty(navigator, 'maxTouchPoints', {
    configurable: true,
    value: 0,
  });
}

function renderShell(collapsed = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <CommandPaletteProvider>
          <AppSidebar
            collapsed={collapsed}
            onToggleCollapse={vi.fn()}
            width={260}
            onWidthChange={vi.fn()}
          />
          <BottomTabBar />
          <div className="monaco-editor">
            <textarea data-testid="monaco-input" />
          </div>
        </CommandPaletteProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('CommandPaletteProvider shortcuts and entry points', () => {
  const originalPlatform = Object.getOwnPropertyDescriptor(navigator, 'platform');
  const originalUaData = Object.getOwnPropertyDescriptor(navigator, 'userAgentData');
  const originalTouch = Object.getOwnPropertyDescriptor(navigator, 'maxTouchPoints');

  beforeEach(() => {
    setNavigatorPlatform('MacIntel');
  });

  afterEach(() => {
    if (originalPlatform) Object.defineProperty(navigator, 'platform', originalPlatform);
    if (originalUaData) Object.defineProperty(navigator, 'userAgentData', originalUaData);
    if (originalTouch) Object.defineProperty(navigator, 'maxTouchPoints', originalTouch);
  });

  it('opens CommandPalette on both ⌘K and ⌘⇧K on Mac and keeps ⌘K shortcut hints', async () => {
    expect(paletteShortcut()).toBe('⌘K');
    expect(paletteKeyShortcuts()).toBe('Meta+K');
    const view = renderShell();
    const sidebar = view.container.querySelector('aside.sidebar') as HTMLElement;

    expect(within(sidebar).getByRole('button', { name: /Search research \(⌘K\)|搜索研究 \(⌘K\)/ })).toBeInTheDocument();
    const paletteBtn = within(sidebar).getByRole('button', { name: /Commands & jump|命令与跳转/ });
    expect(paletteBtn).toHaveAttribute('aria-keyshortcuts', 'Meta+K');
    expect(paletteBtn).toHaveTextContent('⌘K');

    // ⌘K opens Command Palette
    fireEvent.keyDown(window, { key: 'k', metaKey: true, shiftKey: false });
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();

    // ⌘K toggles closed
    fireEvent.keyDown(window, { key: 'k', metaKey: true, shiftKey: false });
    expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();

    // ⌘⇧K also opens Command Palette
    fireEvent.keyDown(window, { key: 'K', metaKey: true, shiftKey: true });
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();
  });

  it('does not steal ⌘K or ⌘⇧K inside .monaco-editor and formats Windows shortcuts as Ctrl K', async () => {
    setNavigatorPlatform('Win32');
    expect(paletteShortcut()).toBe('Ctrl K');
    expect(paletteKeyShortcuts()).toBe('Control+K');

    renderShell();

    const monacoInput = screen.getByTestId('monaco-input');
    fireEvent.keyDown(monacoInput, { key: 'k', ctrlKey: true, shiftKey: false });
    expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();

    fireEvent.keyDown(monacoInput, { key: 'K', ctrlKey: true, shiftKey: true });
    expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true, shiftKey: false });
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true, shiftKey: false });
    expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'K', ctrlKey: true, shiftKey: true });
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();
  });

  it('opens CommandPalette from expanded sidebar button, collapsed rail button, and BottomTabBar', async () => {
    const expanded = renderShell(false);

    fireEvent.click(screen.getByRole('button', { name: /Search research \(⌘K\)|搜索研究 \(⌘K\)/ }));
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close palette' }));
    expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();

    // Mobile BottomTabBar opens CommandPalette via the "More" Radix DropdownMenu
    const user = userEvent.setup();
    const bottomBar = expanded.container.querySelector('.bottom-tab-bar') as HTMLElement;
    await user.click(within(bottomBar).getByTestId('bottom-tab-more'));
    await user.click(await screen.findByRole('menuitem', { name: /Commands & jump|命令与跳转/ }));
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();

    expanded.unmount();

    // Collapsed sidebar rail search button opens CommandPalette
    renderShell(true);
    fireEvent.click(screen.getByRole('button', { name: /Search research \(⌘K\)|搜索研究 \(⌘K\)/ }));
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();
  });
});
