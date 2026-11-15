import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import {
  CommandPaletteProvider,
  paletteKeyShortcuts,
  paletteShortcut,
} from '../CommandPaletteProvider';
import {
  closeGlobalSearch,
  GlobalSearchHost,
} from '@/components/dalton/globalSearchHost';
import AppSidebar from '@/components/Sidebar/AppSidebar';

// DALTON-HOOK(global-search): test ⌘K (GlobalSearch) vs ⌘⇧K (CommandPalette) separation and sidebar labels.
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

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <CommandPaletteProvider>
          <AppSidebar
            collapsed={false}
            onToggleCollapse={vi.fn()}
            width={260}
            onWidthChange={vi.fn()}
          />
          <div className="monaco-editor">
            <textarea data-testid="monaco-input" />
          </div>
        </CommandPaletteProvider>
        <GlobalSearchHost />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('CommandPaletteProvider vs GlobalSearchHost shortcuts', () => {
  const originalPlatform = Object.getOwnPropertyDescriptor(navigator, 'platform');
  const originalUaData = Object.getOwnPropertyDescriptor(navigator, 'userAgentData');
  const originalTouch = Object.getOwnPropertyDescriptor(navigator, 'maxTouchPoints');

  beforeEach(() => {
    closeGlobalSearch();
    setNavigatorPlatform('MacIntel');
  });

  afterEach(() => {
    closeGlobalSearch();
    if (originalPlatform) Object.defineProperty(navigator, 'platform', originalPlatform);
    if (originalUaData) Object.defineProperty(navigator, 'userAgentData', originalUaData);
    if (originalTouch) Object.defineProperty(navigator, 'maxTouchPoints', originalTouch);
  });

  it('opens only GlobalSearch on ⌘K and only CommandPalette on ⌘⇧K (Mac)', async () => {
    renderShell();

    // Sidebar renders both distinct entries with their own shortcuts
    expect(screen.getByRole('button', { name: /Search research \(⌘K\)|搜索研究 \(⌘K\)/ })).toBeInTheDocument();
    const paletteBtn = screen.getByRole('button', { name: /Commands & jump|命令与跳转/ });
    expect(paletteBtn).toBeInTheDocument();
    expect(paletteBtn).toHaveAttribute('aria-keyshortcuts', 'Meta+Shift+K');
    expect(paletteBtn).toHaveTextContent('⌘⇧K');

    // ⌘K opens only Dalton Global Search
    fireEvent.keyDown(window, { key: 'k', metaKey: true, shiftKey: false });
    expect(await screen.findByPlaceholderText(/Search conversations and documents|搜索历史对话与文档/)).toBeInTheDocument();
    expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();

    act(() => {
      closeGlobalSearch();
    });
    await waitFor(() => {
      expect(screen.queryByPlaceholderText(/Search conversations and documents|搜索历史对话与文档/)).not.toBeInTheDocument();
    });

    // ⌘⇧K opens only Command Palette
    fireEvent.keyDown(window, { key: 'K', metaKey: true, shiftKey: true });
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Search conversations and documents|搜索历史对话与文档/)).not.toBeInTheDocument();
  });

  it('does not steal ⌘⇧K inside .monaco-editor and formats Windows shortcuts as Ctrl+Shift+K', async () => {
    setNavigatorPlatform('Win32');
    expect(paletteShortcut()).toBe('Ctrl+Shift+K');
    expect(paletteKeyShortcuts()).toBe('Control+Shift+K');

    renderShell();

    // Monaco keeps ⌘⇧K / Ctrl+Shift+K
    const monacoInput = screen.getByTestId('monaco-input');
    fireEvent.keyDown(monacoInput, { key: 'K', ctrlKey: true, shiftKey: true });
    expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();

    // Outside Monaco, Ctrl+Shift+K opens CommandPalette, not GlobalSearch
    fireEvent.keyDown(window, { key: 'K', ctrlKey: true, shiftKey: true });
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Search conversations and documents|搜索历史对话与文档/)).not.toBeInTheDocument();
  });

  it('closes GlobalSearch when CommandPalette opens and closes CommandPalette when GlobalSearch opens', async () => {
    renderShell();

    // Open GlobalSearch via ⌘K, then open CommandPalette via ⌘⇧K -> GlobalSearch closes
    fireEvent.keyDown(window, { key: 'k', metaKey: true, shiftKey: false });
    expect(await screen.findByPlaceholderText(/Search conversations and documents|搜索历史对话与文档/)).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'K', metaKey: true, shiftKey: true });
    expect(await screen.findByTestId('mock-command-palette')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByPlaceholderText(/Search conversations and documents|搜索历史对话与文档/)).not.toBeInTheDocument();
    });

    // Now press ⌘K while CommandPalette is open -> GlobalSearch opens and CommandPalette closes
    fireEvent.keyDown(window, { key: 'k', metaKey: true, shiftKey: false });
    expect(await screen.findByPlaceholderText(/Search conversations and documents|搜索历史对话与文档/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByTestId('mock-command-palette')).not.toBeInTheDocument();
    });
  });
});
