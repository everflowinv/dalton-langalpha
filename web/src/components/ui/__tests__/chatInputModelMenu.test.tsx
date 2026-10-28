/**
 * The effort/speed rows: what they show, and what a pick writes.
 *
 * The load-bearing rule is that picking the level marked "Default", the one
 * Settings gives, clears the thread's own instead of pinning today's default as
 * a permanent choice: that is the only way back to following Settings.
 *
 * Dropdown primitives are mocked inline (same approach as McpServerRow) so
 * items are queryable without portal or pointer machinery.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

const h = vi.hoisted(() => ({ isMobile: false }));

vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => h.isMobile }));

vi.mock('../dropdown-menu', () => {
  const Box = ({ children }: { children: React.ReactNode }) => <div>{children}</div>;
  return {
    DropdownMenu: Box,
    // The real trigger puts `disabled` on the button it wraps.
    DropdownMenuTrigger: ({ children, disabled }: { children: React.ReactElement; disabled?: boolean }) =>
      React.cloneElement(children as React.ReactElement<{ disabled?: boolean }>, { disabled }),
    DropdownMenuContent: Box,
    // Props pass through, so a caller's own role/aria survives — the real
    // primitive forwards them, and the effort options rely on it. data-menu-item
    // marks what came from the primitive rather than a hand-rolled div.
    // `variant` is the wrapper's own prop, dropped here the way the real one
    // resolves it to classes rather than passing it down to the div.
    DropdownMenuItem: ({ children, onSelect, variant: _variant, ...rest }: {
      children: React.ReactNode;
      onSelect?: (e: { preventDefault: () => void }) => void;
      variant?: string;
    } & Record<string, unknown>) => (
      <div role="menuitem" data-menu-item {...rest} onClick={(e) => onSelect?.(e)}>{children}</div>
    ),
    DropdownMenuSeparator: () => <hr />,
    DropdownMenuSub: Box,
    DropdownMenuSubTrigger: ({ children }: { children: React.ReactNode }) => (
      <div role="menuitem">{children}</div>
    ),
    DropdownMenuSubContent: Box,
  };
});

import { ChatInputModelMenu } from '../chat-input.modelMenu';

const onReasoningEffortChange = vi.fn();
const onFastModeChange = vi.fn();

function renderMenu(overrides: Record<string, unknown> = {}) {
  const props = {
    selectedModel: 'qwen3.6-flash',
    onSelectModel: vi.fn(),
    threadModels: [],
    validModelNames: new Set(['qwen3.6-flash']),
    moreModelsItems: [],
    hasStarredModels: true,
    reasoningEffort: 'high',
    defaultEffort: 'medium',
    onReasoningEffortChange,
    fastMode: false,
    onFastModeChange,
    isCodexModel: false,
    needsSignIn: () => false,
    reasoningEfforts: ['none', 'low', 'medium', 'high'],
    dropdownDirection: 'up' as const,
    container: null,
    ...overrides,
  };
  return render(
    <MemoryRouter>
      <ChatInputModelMenu {...(props as React.ComponentProps<typeof ChatInputModelMenu>)} />
    </MemoryRouter>,
  );
}

/** The option list is a sibling set of radio rows; find one by its word. The
 *  row above shows the same word as its value, so scope the search to the
 *  options rather than the whole menu. */
function option(label: string) {
  const found = screen.getAllByRole('menuitemradio').find((el) => within(el).queryByText(label));
  if (!found) throw new Error(`no option labelled ${label}`);
  return found;
}

beforeEach(() => {
  h.isMobile = false;
  onReasoningEffortChange.mockClear();
  onFastModeChange.mockClear();
});

describe('effort row', () => {
  it('shows the level it is handed as the current value', () => {
    renderMenu();
    const row = screen.getByText('Effort').closest('[role="menuitem"]')!;
    expect(within(row as HTMLElement).getByText('High')).toBeInTheDocument();
    expect(option('High')).toHaveAttribute('aria-checked', 'true');
  });

  it('checks nothing while the level is not known', () => {
    renderMenu({ reasoningEffort: null });
    expect(screen.getAllByRole('menuitemradio').every((el) => el.getAttribute('aria-checked') === 'false')).toBe(true);
  });

  it('badges the Settings level and nothing else', () => {
    renderMenu();
    expect(within(option('Medium') as HTMLElement).getByText('Default')).toBeInTheDocument();
    expect(screen.getAllByText('Default')).toHaveLength(1);
  });

  it('clears the override when the default level is picked', () => {
    renderMenu();
    fireEvent.click(option('Medium'));
    expect(onReasoningEffortChange).toHaveBeenCalledWith(null);
  });

  it('writes the level when any other one is picked', () => {
    renderMenu();
    fireEvent.click(option('Low'));
    expect(onReasoningEffortChange).toHaveBeenCalledWith('low');
  });

  it('renders words, never a bare level name', () => {
    renderMenu({ reasoningEfforts: ['none', 'xhigh', 'max'], reasoningEffort: 'xhigh' });
    expect(screen.getAllByRole('menuitemradio').map((el) => el.textContent))
      .toEqual(['Off', 'Extra High', 'Max']);
    expect(screen.queryByText('xhigh')).not.toBeInTheDocument();
  });

  it('is absent for a model with no ladder', () => {
    renderMenu({ reasoningEfforts: [] });
    expect(screen.queryByText('Effort')).not.toBeInTheDocument();
  });

  /* These were plain divs once, which reads fine with a mouse and strands
     anyone without one: a div outside the menu's item collection is skipped by
     roving focus, so arrowing into the list lands nowhere and Enter does
     nothing. Built on the menu-item primitive, they are reachable by the same
     path every sibling row uses. */
  it('builds each option on the menu-item primitive, not a bare div', () => {
    renderMenu();
    const options = screen.getAllByRole('menuitemradio');
    expect(options).toHaveLength(4);
    for (const el of options) expect(el).toHaveAttribute('data-menu-item');
  });

  it('keeps the setting row itself a menu item', () => {
    h.isMobile = true;
    renderMenu();
    expect(screen.getByText('Effort').closest('[role="menuitem"]')).toHaveAttribute('data-menu-item');
  });
});

describe('model names', () => {
  it('prints the authored display name, else one derived from the key', () => {
    renderMenu({
      selectedModel: 'deepseek-flash',
      threadModels: ['claude-opus-4-8'],
      validModelNames: new Set(['deepseek-flash', 'claude-opus-4-8']),
      metadata: { 'deepseek-flash': { display_name: 'DeepSeek-V4.1-Flash' } },
    });
    expect(screen.getAllByText('DeepSeek-V4.1-Flash').length).toBeGreaterThan(0);
    expect(screen.queryByText('Deepseek Flash')).toBeNull();
    expect(screen.getByText('Opus 4.8')).toBeInTheDocument();
  });
});

describe('trigger', () => {
  it('is disabled while the model list loads', () => {
    renderMenu({ disabled: true });
    expect(screen.getByTitle('Select model')).toBeDisabled();
  });

  it('is enabled once it has loaded', () => {
    renderMenu();
    expect(screen.getByTitle('Select model')).toBeEnabled();
  });
});

describe('speed row', () => {
  it('is absent on a non-Codex model', () => {
    renderMenu();
    expect(screen.queryByText('Speed')).not.toBeInTheDocument();
  });

  it('picks fast and standard', () => {
    renderMenu({ isCodexModel: true });
    fireEvent.click(option('Fast'));
    expect(onFastModeChange).toHaveBeenCalledWith(true);

    fireEvent.click(option('Standard'));
    expect(onFastModeChange).toHaveBeenCalledWith(false);
  });
});

describe('a model whose sign-in needs renewing', () => {
  // Still pickable, since the user may reconnect at any moment, but named as
  // needing sign-in before a send on it is refused.
  it('says so beside its name and can still be picked', () => {
    const onSelectModel = vi.fn();
    renderMenu({
      threadModels: ['gpt-x'],
      validModelNames: new Set(['qwen3.6-flash', 'gpt-x']),
      metadata: { 'gpt-x': { display_name: 'GPT X' }, 'qwen3.6-flash': { display_name: 'Qwen Flash' } },
      needsSignIn: (m: string) => m === 'gpt-x',
      onSelectModel,
    });
    const rowOf = (name: string) => screen.getAllByRole('menuitem').find((el) => within(el).queryByText(name))!;
    const row = rowOf('GPT X');
    expect(within(row).getByText('Needs sign-in')).toBeInTheDocument();
    const other = rowOf('Qwen Flash');
    expect(within(other).queryByText('Needs sign-in')).toBeNull();

    fireEvent.click(row);
    expect(onSelectModel).toHaveBeenCalledWith('gpt-x');
  });
});

describe('mobile', () => {
  // Radix submenus lose a tap on mobile, so the row expands in place instead.
  it('hides the options until the row is tapped', () => {
    h.isMobile = true;
    renderMenu();
    expect(screen.queryByText('Low')).toBeNull();

    fireEvent.click(screen.getByText('Effort').closest('[role="menuitem"]')!);
    expect(option('Low')).toBeTruthy();

    fireEvent.click(option('Low'));
    expect(onReasoningEffortChange).toHaveBeenCalledWith('low');
  });
});
