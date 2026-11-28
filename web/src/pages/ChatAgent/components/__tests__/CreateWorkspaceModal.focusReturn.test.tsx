// DALTON-HOOK(dialog-focus-return): closing the New workspace dialog puts focus back on the control that opened it
/**
 * Where focus lands when the New workspace dialog closes. A dialog's opener
 * is read back from the click: the handler hands the dialog the control it
 * belongs to, and the dialog focuses it on close.
 *
 * Guessing it instead fails twice. Voice Control, a screen reader's virtual
 * cursor and a script's `click()` all activate a button without moving focus
 * to it and without a `pointerdown` first. And Safari leaves focus where a
 * click found it, so with the caret in the composer a click on "New
 * workspace" would read the composer as the opener. `fireEvent.click` below
 * is that activation: it moves focus nowhere and fires no pointer event.
 */
import { useEffect, useRef, useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { queryOptions } from '@tanstack/react-query';

import { renderWithProviders } from '@/test/utils';
import type { Workspace } from '@/types/api';
import { computersApi } from './computersDialogApi';
import { CreateWorkspaceHost, useOpenCreateWorkspace } from '@/components/CreateWorkspaceHost';
import { CommandPaletteProvider, useCommandPalette } from '@/components/CommandPalette/CommandPaletteProvider';
import { clientOs } from '@/lib/clientOs';

vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  isDaltonBuild: false,
}));

vi.mock('@/hooks/useUser', () => ({ useUser: () => ({ user: null, isLoading: false, isError: false }) }));

vi.mock('../../utils/api', async () => ({
  ...(await computersApi()),
  uploadWorkspaceFile: vi.fn(),
  startWorkspace: vi.fn(),
  setComputerSpec: vi.fn(),
  reorderWorkspaces: vi.fn(),
  createWorkspace: vi.fn(),
  getRecentThreads: vi.fn(async () => ({ threads: [] })),
  getWorkspaceThreads: vi.fn(async () => ({ threads: [], total: 0 })),
}));

vi.mock('@/api/search', () => ({
  searchConversations: vi.fn(async () => ({
    threads: [],
    turns: [],
    files: [],
    next_cursor: { threads: null, turns: null, files: null },
    failed: [],
  })),
  getSearchPreview: vi.fn(async () => {
    throw new Error('no preview');
  }),
}));
vi.mock('@/lib/dalton/searchApi', () => ({
  fetchGlobalSearch: vi.fn(async () => ({ documents: [] })),
}));
vi.mock('@/hooks/useSkills', () => ({ useSkills: () => ({ data: [] }) }));
vi.mock('@/hooks/useHomeWorkspace', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useHomeWorkspace')>()),
  homeWorkspaceQuery: () =>
    queryOptions({ queryKey: ['workspaces', 'home'], queryFn: async () => ({ workspace_id: 'home', is_home: true, name: 'Home' }) }),
  useHomeWorkspaceId: () => 'home',
  resolveChatWorkspace: vi.fn(async () => 'home'),
}));
vi.mock('@/hooks/useSymbolSearch', () => ({ useSymbolSearch: () => ({ hits: [], loading: false }) }));
vi.mock('@/lib/quotes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useQuotes: () => ({ quotes: {} }),
}));
vi.mock('@/contexts/ThemeContext', () => ({ useTheme: () => ({ theme: 'light' }) }));
vi.mock('@/hooks/useAppearance', () => ({ useAppearance: () => ({ pickTheme: vi.fn() }) }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ userId: null, logout: vi.fn() }) }));
vi.mock('@/components/nav/useNavItems', () => ({ useNavItems: () => [] }));
vi.mock('@/pages/Settings/panels/usePreferenceWrite', () => ({ useSaveFailureToast: () => vi.fn() }));

vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));
vi.mock('@/lib/dalton/coverage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/dalton/coverage')>()),
  useDaltonCoverageUnits: () => ({ data: { enabled: false }, isError: false }),
}));

// The gallery's own reads, as WorkspaceGallery.computer.test.tsx stubs them.
vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [], total: 0 }, isLoading: false, error: null }),
  allWorkspacesKey: (sortBy: string, includeHome: boolean) => ['workspaces', 'list', { view: 'all', sortBy, includeHome }],
}));
vi.mock('../workspaceActions', () => ({
  WorkspaceMenuItems: () => null,
  useWorkspaceActions: () => ({ openDuplicate: vi.fn(), openMove: vi.fn(), openDelete: vi.fn(), dialogs: null }),
}));
vi.mock('../../hooks/useRemoteControl', () => ({ useThisMac: () => ({ status: null }) }));
vi.mock('../../hooks/workspaceRowActions', () => ({ pinWorkspaceRow: vi.fn() }));
vi.mock('../../hooks/useNavigationData', () => ({ isEffectivelyPinned: () => false }));
vi.mock('../../hooks/utils/chatSessionRestore', () => ({ clearChatSession: vi.fn() }));

import CreateWorkspaceModal from '../CreateWorkspaceModal';
import WorkspaceGallery from '../WorkspaceGallery';
import { getComputers } from '../../utils/api';

/** The gallery's button: "Create Workspace" where it is empty, "New workspace" above the cards. */
const NEW_WORKSPACE = /^(New workspace|Create Workspace|新建工作区|创建工作区)$/;

beforeEach(() => {
  vi.mocked(getComputers).mockResolvedValue({ computers: [], total: 0 });
});

/** Opens the dialog, closes it from the keyboard, and returns once it is gone. */
async function closeWithEscape() {
  await screen.findByRole('dialog');
  fireEvent.keyDown(document, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
}

describe('the New workspace dialog returns focus to its opener', () => {
  it('focuses the control the opener named, over whatever held focus when the dialog opened', async () => {
    function ComposerAndTrigger() {
      const [open, setOpen] = useState(false);
      const composer = useRef<HTMLTextAreaElement>(null);
      const trigger = useRef<HTMLButtonElement>(null);
      useEffect(() => {
        // Safari after a click on the button: the caret is still in the composer.
        composer.current?.focus();
        setOpen(true);
      }, []);
      return (
        <>
          <textarea aria-label="Message" ref={composer} />
          <button ref={trigger}>New workspace</button>
          {open && (
            <CreateWorkspaceModal
              isOpen
              returnFocusTo={trigger.current}
              onClose={() => setOpen(false)}
              onCreate={vi.fn(async () => ({ workspace_id: 'ws-1', name: 'a', status: 'running' }) as Workspace)}
            />
          )}
        </>
      );
    }
    const { getByRole } = renderWithProviders(<ComposerAndTrigger />);
    const trigger = getByRole('button', { name: 'New workspace' });

    await closeWithEscape();

    expect(document.activeElement).toBe(trigger);
  });

  it('focuses the button the app shell opened it from, for an activation that moves focus nowhere', async () => {
    function SidebarButton() {
      const open = useOpenCreateWorkspace();
      return <button onClick={(e) => open?.(e.currentTarget)}>New workspace</button>;
    }
    renderWithProviders(
      <CreateWorkspaceHost>
        <SidebarButton />
      </CreateWorkspaceHost>,
    );
    const trigger = screen.getByRole('button', { name: 'New workspace' });

    fireEvent.click(trigger);
    await closeWithEscape();

    expect(document.activeElement).toBe(trigger);
  });

  it("focuses the gallery's own button, which mounts the dialog without the app shell's guess", async () => {
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);
    // An empty gallery offers the dialog three times: the wide header's action,
    // the narrow header's and the empty state's, each wired on its own.
    const triggers = await screen.findAllByRole('button', { name: NEW_WORKSPACE });
    expect(triggers).toHaveLength(3);

    for (const trigger of triggers) {
      fireEvent.click(trigger);
      await closeWithEscape();

      expect(document.activeElement).toBe(trigger);
    }
  });

  it('returns focus to the persistent control that opened the command palette when New workspace is clicked and cancelled', async () => {
    function PaletteTriggerButton() {
      const palette = useCommandPalette();
      return (
        <button
          type="button"
          onClick={(e) => palette?.open(undefined, { returnFocusTo: e.currentTarget })}
        >
          Open palette
        </button>
      );
    }

    renderWithProviders(
      <CreateWorkspaceHost>
        <CommandPaletteProvider>
          <PaletteTriggerButton />
          <textarea aria-label="Composer" />
        </CommandPaletteProvider>
      </CreateWorkspaceHost>,
    );

    // 1. Opened from a persistent button: click "New workspace" in the palette, then click Cancel.
    const paletteButton = screen.getByRole('button', { name: 'Open palette' });
    fireEvent.click(paletteButton);
    const input = await screen.findByRole('combobox');
    fireEvent.change(input, { target: { value: 'new workspace' } });
    const row = await screen.findByRole('option', { name: /New workspace|新建工作区/ });
    fireEvent.pointerDown(row);
    fireEvent.click(row);

    await screen.findByRole('dialog');
    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^(Cancel|取消)$/ }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    expect(document.activeElement).toBe(paletteButton);

    // 2. Opened by shortcut from the composer: click "New workspace" in the palette, then press Escape.
    const composer = screen.getByLabelText('Composer');
    composer.focus();
    fireEvent.keyDown(composer, {
      key: 'k',
      ...(clientOs() === 'windows' ? { ctrlKey: true } : { metaKey: true }),
    });
    const input2 = await screen.findByRole('combobox');
    fireEvent.change(input2, { target: { value: 'new workspace' } });
    const row2 = await screen.findByRole('option', { name: /New workspace|新建工作区/ });
    fireEvent.pointerDown(row2);
    fireEvent.click(row2);

    await closeWithEscape();

    expect(document.activeElement).toBe(composer);
  });
});
