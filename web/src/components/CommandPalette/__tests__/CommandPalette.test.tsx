import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { queryOptions } from '@tanstack/react-query';
import { useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { CreateWorkspaceHost } from '@/components/CreateWorkspaceHost';
import { clientOs } from '@/lib/clientOs';
import { searchConversations } from '@/api/search';
// DALTON-HOOK(global-search): test Dalton document search integration inside CommandPalette
import { fetchGlobalSearch, type GlobalSearchOptions, type GlobalSearchResponse } from '@/lib/dalton/searchApi';
import { getRecentThreads } from '@/pages/ChatAgent/utils/api';
import { MEMO_USER_DIR } from '@/pages/ChatAgent/utils/agentPaths.generated';
import { CommandPaletteProvider, useCommandPalette } from '../CommandPaletteProvider';

// The palette's sources answer with nothing, and at once: these tests are
// about its keys and focus, not what it finds.
// One workspace, with one thread, is there to scope to.
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getRecentThreads: vi.fn(async () => ({ threads: [] })),
  getWorkspaceThreads: vi.fn(async () => ({
    threads: [{ thread_id: 't1', title: 'NVDA margins', workspace_id: 'ws1', updated_at: '2026-11-08T00:00:00Z' }],
  })),
}));
vi.mock('@/api/search', () => ({
  searchConversations: vi.fn(async () => ({ threads: [], turns: [], files: [], next_cursor: { threads: null, turns: null, files: null }, failed: [] })),
  getSearchPreview: vi.fn(async () => {
    throw new Error('no preview here');
  }),
}));
// DALTON-HOOK(global-search): stub Dalton search endpoint and home workspace helpers
const resolveChatWorkspaceMock = vi.fn(async () => 'home');
let mockHomeWorkspaceId: string | undefined = 'home';
vi.mock('@/lib/dalton/searchApi', () => ({
  fetchGlobalSearch: vi.fn<
    (q: string, limit?: number, signal?: AbortSignal, options?: GlobalSearchOptions) => Promise<GlobalSearchResponse>
  >(async () => ({ conversations: [], documents: [] })),
}));
vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [{ workspace_id: 'ws1', name: 'Semis', status: 'running', is_home: false }] } }),
}));
vi.mock('@/hooks/useSkills', () => ({ useSkills: () => ({ data: [] }) }));
vi.mock('@/hooks/useHomeWorkspace', () => ({
  homeWorkspaceQuery: () =>
    queryOptions({ queryKey: ['workspaces', 'home'], queryFn: async () => ({ workspace_id: 'home', is_home: true, name: 'Home' }) }),
  useHomeWorkspaceId: () => mockHomeWorkspaceId,
  resolveChatWorkspace: (...args: unknown[]) => resolveChatWorkspaceMock(...(args as [])),
}));
vi.mock('@/hooks/useSymbolSearch', () => ({ useSymbolSearch: () => ({ hits: [], loading: false }) }));
vi.mock('@/lib/quotes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useQuotes: () => ({ quotes: {} }),
}));
vi.mock('@/contexts/ThemeContext', () => ({ useTheme: () => ({ theme: 'light' }) }));
vi.mock('@/hooks/useAppearance', () => ({ useAppearance: () => ({ pickTheme: vi.fn() }) }));
// DALTON-HOOK(team-credentials): useLocalSources calls useDaltonSettingsVisibility -> useTeamCredentials -> useAuth.
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ userId: null, logout: vi.fn() }) }));
vi.mock('@/components/nav/useNavItems', () => ({ useNavItems: () => [] }));
vi.mock('@/pages/Settings/panels/usePreferenceWrite', () => ({ useSaveFailureToast: () => vi.fn() }));
vi.mock('@/pages/ChatAgent/hooks/useCreateWorkspace', () => ({ useCreateWorkspace: () => vi.fn() }));
vi.mock('@/pages/ChatAgent/components/CreateWorkspaceModal', () => ({
  default: () => (
    <div role="dialog" aria-label="Create workspace">
      <input aria-label="Workspace name" autoFocus />
    </div>
  ),
}));

/** Where the app is, so a test reads which row ran. */
function Path() {
  return <output aria-label="Path">{useLocation().pathname}</output>;
}

/** A surface that opens the palette at a kind, as the phone's Dashboard field does. */
function OpensTickers() {
  const palette = useCommandPalette();
  return (
    <button type="button" onClick={() => palette?.open({ kind: 'tickers' })}>
      Search stocks
    </button>
  );
}

function renderShell() {
  return renderWithProviders(
    <CreateWorkspaceHost>
      <CommandPaletteProvider>
        <textarea aria-label="Composer" />
        <OpensTickers />
        <Path />
      </CommandPaletteProvider>
    </CreateWorkspaceHost>,
  );
}
const path = () => screen.getByLabelText('Path').textContent;

const shortcut = () =>
  fireEvent.keyDown(document.activeElement ?? window, { key: 'k', ...(clientOs() === 'windows' ? { ctrlKey: true } : { metaKey: true }) });
/** A key pressed wherever focus is, as the browser would send it. */
const press = (key: string) => fireEvent.keyDown(document.activeElement ?? window, { key });
const field = () => screen.findByRole('combobox');

// The palette's code, loaded once up front so each open waits only on the
// lazy component's own promise, as it does in the app once the code is warm.
beforeAll(async () => {
  await import('../CommandPalette');
}, 30_000);

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('the key buffer', () => {
  it('gives the field what was typed before it had focus, and keeps it off the page', async () => {
    renderShell();
    const composer = screen.getByLabelText('Composer');
    composer.focus();
    shortcut();
    // The page's own key handlers, which would send a draft on Enter.
    const heard: string[] = [];
    const listen = (e: KeyboardEvent) => heard.push(e.key);
    document.addEventListener('keydown', listen);
    // The palette's code is still loading: these land nowhere but the buffer.
    expect(press('n')).toBe(false);
    expect(press('v')).toBe(false);
    press('x');
    press('Backspace');
    press('Enter');

    document.removeEventListener('keydown', listen);
    expect(heard).not.toContain('Enter');
    expect(composer).not.toHaveFocus();
    expect(composer).toHaveValue('');
    const input = await field();
    await waitFor(() => expect(input).toHaveFocus());
    expect(input).toHaveValue('nv');
  });

  it('takes an IME composition finished before the field had focus', async () => {
    renderShell();
    screen.getByLabelText('Composer').focus();
    shortcut();
    // Focus moved off the composer at once, to where the composition then runs.
    const standIn = document.activeElement as HTMLInputElement;
    expect(standIn).not.toBe(screen.getByLabelText('Composer'));
    press('a');
    fireEvent.compositionStart(standIn);
    standIn.value = '毛利';
    fireEvent.input(standIn);
    fireEvent.compositionEnd(standIn, { data: '毛利' });

    const input = await field();
    await waitFor(() => expect(input).toHaveValue('a毛利'));
    expect(screen.getByLabelText('Composer')).toHaveValue('');
  });

  it('closes on Escape before the palette drew, handing focus back', async () => {
    renderShell();
    const composer = screen.getByLabelText('Composer');
    composer.focus();
    shortcut();
    press('Escape');
    expect(composer).toHaveFocus();
  });
});

describe('focus', () => {
  it('returns to where it was when the palette closes', async () => {
    renderShell();
    const composer = screen.getByLabelText('Composer');
    composer.focus();
    shortcut();
    const input = await field();
    await waitFor(() => expect(input).toHaveFocus());

    fireEvent.keyDown(input, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument());
    expect(composer).toHaveFocus();
  });
});

describe('New workspace', () => {
  it('opens its dialog as the palette closes, and the dialog keeps focus', async () => {
    renderShell();
    screen.getByLabelText('Composer').focus();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'new workspace' } });
    const row = await screen.findByRole('option', { name: /New workspace/ });
    fireEvent.click(row);

    const dialog = await screen.findByRole('dialog', { name: 'Create workspace' });
    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument());
    await act(async () => {});
    expect(dialog).toBeInTheDocument();
    expect(screen.getByLabelText('Workspace name')).toHaveFocus();
  });
});

describe('scopes', () => {
  const selected = (name: RegExp) => waitFor(() => expect(screen.getByRole('option', { name })).toHaveAttribute('aria-selected', 'true'));

  it('narrows to a workspace on Tab and to its thread on another, and Backspace steps back out of each', async () => {
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'semis' } });
    await selected(/Semis/);

    fireEvent.keyDown(input, { key: 'Tab' });
    expect(input).toHaveFocus();
    expect(input).toHaveValue('');
    expect(input).toHaveAccessibleName('Search in Semis');
    // Its newest threads, before a word is typed.
    await selected(/NVDA margins/);
    expect(screen.getByText('Tab: search in it.', { exact: false })).toBeInTheDocument();

    fireEvent.keyDown(input, { key: 'Tab' });
    expect(input).toHaveAccessibleName('Search in NVDA margins');
    expect(screen.getByText('Search in this thread')).toBeInTheDocument();

    fireEvent.keyDown(input, { key: 'Backspace' });
    expect(input).toHaveAccessibleName('Search in Semis');
    fireEvent.keyDown(input, { key: 'Backspace' });
    expect(input).toHaveAccessibleName(/Search threads/);
    expect(input).toHaveValue('semis');
    await selected(/Semis/);
  });

  it('takes a leading $ as the tickers chip, and Escape steps out of it before closing', async () => {
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: '$NVDA' } });
    expect(input).toHaveValue('NVDA');
    expect(input).toHaveAccessibleName('Search in Tickers');

    fireEvent.keyDown(input, { key: 'Escape' });
    expect(input).toHaveAccessibleName(/Search threads/);
    expect(screen.getByRole('combobox')).toBeInTheDocument();
  });

  it('opens in the tickers scope when asked to start there, and Backspace widens it to everything', async () => {
    renderShell();
    fireEvent.click(screen.getByRole('button', { name: 'Search stocks' }));
    const input = await field();
    await waitFor(() => expect(input).toHaveFocus());
    expect(input).toHaveValue('');
    expect(input).toHaveAccessibleName('Search in Tickers');

    fireEvent.change(input, { target: { value: 'nv' } });
    expect(input).toHaveAccessibleName('Search in Tickers');
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.keyDown(input, { key: 'Backspace' });
    expect(input).toHaveAccessibleName(/Search threads/);

    // The next open by the shortcut starts from everything again.
    fireEvent.keyDown(input, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument());
    shortcut();
    expect(await field()).toHaveAccessibleName(/Search threads/);
  });

  it('starts in the tickers scope on a cold open, keeping keys typed before the field had focus', async () => {
    renderShell();
    fireEvent.click(screen.getByRole('button', { name: 'Search stocks' }));
    // Focus is on the button, outside the palette, while its code loads.
    press('n');
    press('v');
    const input = await field();
    await waitFor(() => expect(input).toHaveFocus());
    expect(input).toHaveAccessibleName('Search in Tickers');
    expect(input).toHaveValue('nv');
  });

  it('takes a leading / as the files chip, says what to type, and Backspace in the empty field takes it off', async () => {
    renderShell();
    shortcut();
    const input = await field();
    // Said under the empty field as text: no row Enter could land on.
    const prefixes = document.querySelector('.command-palette-prefixes');
    expect(prefixes).toHaveTextContent('/files');
    expect(prefixes?.closest('[role="listbox"]')).toBeNull();

    fireEvent.change(input, { target: { value: '/' } });
    expect(input).toHaveValue('');
    expect(input).toHaveAccessibleName('Search in Files');
    expect(screen.getByText("Type part of a file's name or path.")).toBeInTheDocument();
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    expect(document.querySelector('.command-palette-prefixes')).toBeNull();

    fireEvent.keyDown(input, { key: 'Backspace' });
    expect(input).toHaveAccessibleName(/Search threads/);
    expect(input).toHaveValue('');
  });

  it('narrows a workspace to its files with /, and Backspace returns to the workspace', async () => {
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'semis' } });
    await selected(/Semis/);
    fireEvent.keyDown(input, { key: 'Tab' });
    expect(input).toHaveAccessibleName('Search in Semis');

    fireEvent.change(input, { target: { value: '/' } });
    expect(input).toHaveAccessibleName('Search Files in Semis');
    fireEvent.keyDown(input, { key: 'Backspace' });
    expect(input).toHaveAccessibleName('Search in Semis');
    await selected(/NVDA margins/);
  });
});

describe('drill-ins', () => {
  const thread = (id: string, title: string) => ({ thread_id: id, title, workspace_id: 'ws1', updated_at: '2026-11-08T00:00:00Z' });
  beforeEach(() => {
    vi.mocked(getRecentThreads).mockResolvedValue({ threads: [thread('r1', 'Rates outlook')] } as never);
    vi.mocked(searchConversations).mockImplementation(async ({ q }) => ({
      threads: q ? [{ ...thread('s1', 'NVDA margins'), workspace_name: 'Semis', automation_name: null, is_pinned: false, has_title: true, match: [] }] : [],
      turns: [],
      files: [],
      next_cursor: { threads: 'next', turns: null, files: null },
      failed: [],
    }));
  });
  afterEach(() => {
    vi.mocked(getRecentThreads).mockResolvedValue({ threads: [] } as never);
    vi.mocked(searchConversations).mockResolvedValue({ threads: [], turns: [], files: [], next_cursor: { threads: null, turns: null, files: null }, failed: [] });
  });

  it('opens Show all as the threads view, and Escape brings the results back as they were', async () => {
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'nvda' } });
    fireEvent.click(await screen.findByRole('option', { name: /Show all threads/ }));

    expect(screen.getByRole('listbox')).toHaveAccessibleName('Threads');
    expect(input).toHaveValue('nvda');
    await screen.findByRole('option', { name: /NVDA margins/ });

    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    expect(input).toHaveValue('nvda');
    await waitFor(() => expect(screen.getByRole('option', { name: /Show all threads/ })).toHaveAttribute('aria-selected', 'true'));
  });

  it('lists the newest threads once its field is cleared, as # does', async () => {
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'nvda' } });
    fireEvent.click(await screen.findByRole('option', { name: /Show all threads/ }));
    fireEvent.change(input, { target: { value: '' } });
    await screen.findByRole('option', { name: /Rates outlook/ });
    expect(screen.queryByText('No matches')).not.toBeInTheDocument();

    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.change(input, { target: { value: '#' } });
    expect(input).toHaveAccessibleName('Search in Threads');
    await screen.findByRole('option', { name: /Rates outlook/ });
  });
});

describe('Enter while the server answers', () => {
  const EMPTY_ANSWER = { threads: [], turns: [], files: [], next_cursor: { threads: null, turns: null, files: null }, failed: [] };
  const found = {
    ...EMPTY_ANSWER,
    threads: [{ thread_id: 's1', title: 'Zebra stripes', workspace_id: 'ws1', workspace_name: 'Semis', automation_name: null, updated_at: '2026-11-08T00:00:00Z', is_pinned: false, has_title: true, match: [] }],
  };
  /** The server's answer, held until the test lets it go. */
  function holdAnswer() {
    let answer: (value: typeof found) => void = () => {};
    let refuse: (error: unknown) => void = () => {};
    vi.mocked(searchConversations).mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          answer = resolve;
          refuse = reject;
        }),
    );
    return { answer: (value: typeof found) => act(async () => answer(value)), refuse: (error: unknown) => act(async () => refuse(error)) };
  }
  const asked = () => waitFor(() => expect(searchConversations).toHaveBeenCalled());
  afterEach(() => {
    vi.mocked(searchConversations).mockResolvedValue(EMPTY_ANSWER);
  });

  it('waits for the server when only Ask matched, then opens the first row it found', async () => {
    const server = holdAnswer();
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'zebra' } });
    await screen.findByRole('option', { name: /zebra/i });
    fireEvent.keyDown(input, { key: 'Enter' });
    await asked();
    expect(path()).toBe('/');

    await server.answer(found);
    await waitFor(() => expect(path()).toBe('/chat/t/s1'));
  });

  it('runs nothing once the selection moves while Enter waits', async () => {
    const server = holdAnswer();
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'zebra' } });
    await screen.findByRole('option', { name: /zebra/i });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    await asked();

    await server.answer(found);
    await screen.findByRole('option', { name: /Zebra stripes/ });
    await act(async () => {});
    expect(path()).toBe('/');
    expect(screen.getByRole('combobox')).toBeInTheDocument();
  });

  it('waits on a stalled server for two seconds at most, then asks', async () => {
    holdAnswer();
    renderShell();
    shortcut();
    const input = await field();
    // Ask is offered once where new work goes is known.
    fireEvent.change(input, { target: { value: 'zebra' } });
    await screen.findByRole('option', { name: /zebra/i });
    vi.useFakeTimers();
    fireEvent.change(input, { target: { value: 'zebras' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(searchConversations).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'zebras' }), expect.anything());
    fireEvent.keyDown(input, { key: 'Enter' });

    await act(async () => { await vi.advanceTimersByTimeAsync(1800); });
    expect(path()).toBe('/');
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(path()).toBe('/chat/t/__default__');
  });

  it('says a refused search, and Enter no longer waits on it', async () => {
    const server = holdAnswer();
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'zebra' } });
    await asked();
    await server.refuse(Object.assign(new Error('Request failed with status code 429'), { response: { status: 429, data: {} } }));

    await screen.findByText('Too many searches at once. Try again in a moment.');
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(path()).toBe('/chat/t/__default__'));
  });

  it('says which kind the server ran out of time on, and shows the others', async () => {
    vi.mocked(searchConversations).mockResolvedValue({ ...found, failed: ['files'] });
    renderShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'zebra' } });
    await screen.findByRole('option', { name: /Zebra stripes/ });
    expect(screen.getByText("Couldn't search your files right now.")).toBeInTheDocument();
  });
});

// DALTON-HOOK(global-search): Dalton memo and workspace file-content search tests inside CommandPalette
describe('Dalton memo and file-content search in CommandPalette', () => {
  function RouteStateSpy() {
    const loc = useLocation();
    return (
      <>
        <output aria-label="Path">{loc.pathname}</output>
        <output aria-label="RouteState">{JSON.stringify(loc.state ?? null)}</output>
      </>
    );
  }

  function renderDocShell() {
    return renderWithProviders(
      <CreateWorkspaceHost>
        <CommandPaletteProvider>
          <textarea aria-label="Composer" />
          <RouteStateSpy />
        </CommandPaletteProvider>
      </CreateWorkspaceHost>,
    );
  }

  beforeEach(() => {
    mockHomeWorkspaceId = 'home';
    vi.mocked(fetchGlobalSearch).mockResolvedValue({ conversations: [], documents: [] });
  });

  it('uses includeContent=false in root view, shows Memos and Search file content row, and enters File content with includeContent=true + deduplication', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [
        { workspace_id: 'ws1', workspace_name: 'Semis', path: 'reports/dup.md', name: 'dup.md', size: 100, modified_at: '2026-11-08T00:00:00Z', match: [[8, 11]] },
      ],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockResolvedValue({
      conversations: [],
      documents: [
        {
          kind: 'memo',
          memo_key: 'wafer-thesis.md',
          workspace_id: null,
          path: `${MEMO_USER_DIR}/wafer-thesis.md`,
          title: 'Wafer Thesis Memo',
          workspace_name: '',
          timestamp: '2026-11-08T00:00:00Z',
          snippet: 'TSMC wafer margin expanded',
          highlights: [[11, 17]],
        },
        {
          kind: 'workspace_file',
          memo_key: null,
          workspace_id: 'ws1',
          path: 'reports/dup.md',
          title: 'dup.md',
          workspace_name: 'Semis',
          timestamp: '2026-11-08T00:00:00Z',
          snippet: 'Duplicate path margin note',
          highlights: [[15, 21]],
        },
        {
          kind: 'workspace_file',
          memo_key: null,
          workspace_id: 'ws1',
          path: 'notes/body_hit.md',
          title: 'body_hit.md',
          workspace_name: 'Semis',
          timestamp: '2026-11-08T00:00:00Z',
          snippet: 'Matched margin inside body',
          highlights: [[8, 14]],
        },
      ],
    });

    renderDocShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'margin' } });

    await screen.findByText('Wafer Thesis Memo');
    expect(fetchGlobalSearch).toHaveBeenCalledWith('margin', 20, expect.anything(), {
      includeConversations: false,
      workspaceId: undefined,
      includeContent: false,
    });
    expect(fetchGlobalSearch).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      expect.anything(),
      expect.objectContaining({ includeContent: true }),
    );

    const groups = Array.from(document.querySelectorAll('.command-palette-group')).map((el) => el.textContent);
    expect(groups.slice(0, 3)).toEqual(['Files', 'Memos', 'File content']);

    // Root view shows Search file content trigger row instead of body hits
    const triggerOpt = screen.getByRole('option', { name: 'Search file content for “margin”' });
    expect(screen.queryByText('body_hit.md')).not.toBeInTheDocument();

    // Selecting trigger row enters File content view and requests includeContent=true
    fireEvent.click(triggerOpt);
    await screen.findByRole('option', { name: /body_hit\.md/ });
    expect(screen.getByRole('listbox')).toHaveAccessibleName('File content');
    expect(fetchGlobalSearch).toHaveBeenLastCalledWith('margin', 20, expect.anything(), {
      includeConversations: false,
      workspaceId: undefined,
      includeContent: true,
    });

    // `/` view deduplicates upstream dup.md from File content while keeping body_hit.md
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.change(input, { target: { value: '/' } });
    fireEvent.change(input, { target: { value: 'margin' } });
    await screen.findByRole('option', { name: /body_hit\.md/ });
    const filesGroup = await screen.findByRole('group', { name: 'Files' });
    await within(filesGroup).findByRole('option', { name: /dup\.md/ });
    expect(screen.queryByText('Duplicate path margin note')).not.toBeInTheDocument();
    expect(screen.getAllByRole('option', { name: /dup\.md/ })).toHaveLength(1);
    const marks = Array.from(document.querySelectorAll('.command-palette-detail mark')).map((el) => el.textContent);
    expect(marks).toEqual(['margin', 'margin']);
  });

  it('uses 600ms debounce in `/` view (including PreviewPane) and skips non-file prefixes and < 2 char queries', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockResolvedValue({
      conversations: [],
      documents: [
        {
          kind: 'workspace_file',
          memo_key: null,
          workspace_id: 'ws1',
          path: 'notes/wafer.md',
          title: 'wafer.md',
          workspace_name: 'Semis',
          timestamp: '2026-11-08T00:00:00Z',
          snippet: 'Wafer allocation rose 20%',
          highlights: [[0, 5]],
        },
      ],
    });

    renderDocShell();
    shortcut();
    const input = await field();
    vi.useFakeTimers();

    // 1-char query skips /api/v1/dalton/search even after debounce
    fireEvent.change(input, { target: { value: 'w' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(700); });
    expect(fetchGlobalSearch).not.toHaveBeenCalled();

    // `$` ticker prefix does NOT trigger /api/v1/dalton/search
    fireEvent.change(input, { target: { value: '$wafer' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(700); });
    expect(fetchGlobalSearch).not.toHaveBeenCalled();

    // Leave `$` ticker chip back to root before entering `/` files prefix
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.change(input, { target: { value: '/wa' } });
    fireEvent.change(input, { target: { value: 'wafer' } });
    // Before 600ms passes, includeContent=true must NOT have fired yet
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(fetchGlobalSearch).not.toHaveBeenCalled();

    // After 600ms passes, includeContent=true fires once
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    vi.useRealTimers();

    expect(fetchGlobalSearch).toHaveBeenCalledTimes(1);
    expect(fetchGlobalSearch).toHaveBeenCalledWith('wafer', 20, expect.anything(), {
      includeConversations: false,
      workspaceId: undefined,
      includeContent: true,
    });
    await screen.findByRole('option', { name: /wafer\.md/ });
    expect(document.querySelector('.command-palette-pane')?.textContent).toContain('Wafer allocation rose 20%');
  });

  it('never sends the content scan for words typed before switching into the files view', async () => {
    vi.mocked(fetchGlobalSearch).mockResolvedValue({ conversations: [], documents: [] });

    renderDocShell();
    shortcut();
    const input = await field();
    vi.useFakeTimers();

    fireEvent.change(input, { target: { value: 'alpha' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    expect(fetchGlobalSearch).toHaveBeenCalledWith('alpha', 20, expect.anything(), expect.objectContaining({ includeContent: false }));

    fireEvent.change(input, { target: { value: '/be' } });
    fireEvent.change(input, { target: { value: 'beta' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    vi.useRealTimers();

    const contentCalls = vi.mocked(fetchGlobalSearch).mock.calls.filter(([, , , options]) => options?.includeContent);
    expect(contentCalls.map(([q]) => q)).toEqual(['beta']);
  });

  it('keeps memo and triggers Search file content in workspace scope, and hides both in thread scope', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [
        { thread_id: 't1', workspace_id: 'ws1', workspace_name: 'Semis', title: 'NVDA thread', has_title: true, is_pinned: false, updated_at: '2026-11-08T00:00:00Z', automation_name: null, match: [] },
      ],
      turns: [],
      files: [],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockResolvedValue({
      conversations: [],
      documents: [
        {
          kind: 'memo',
          memo_key: 'personal.md',
          workspace_id: null,
          path: `${MEMO_USER_DIR}/personal.md`,
          title: 'Personal Memo',
          workspace_name: '',
          timestamp: null,
          snippet: 'NVDA memo hit',
          highlights: [[0, 4]],
        },
        {
          kind: 'workspace_file',
          memo_key: null,
          workspace_id: 'ws1',
          path: 'ws1.md',
          title: 'ws1.md',
          workspace_name: 'Semis',
          timestamp: null,
          snippet: 'NVDA in ws1',
          highlights: [[0, 4]],
        },
        {
          kind: 'workspace_file',
          memo_key: null,
          workspace_id: 'ws2',
          path: 'ws2.md',
          title: 'ws2.md',
          workspace_name: 'Biotech',
          timestamp: null,
          snippet: 'NVDA in ws2',
          highlights: [[0, 4]],
        },
      ],
    });

    renderDocShell();
    shortcut();
    const input = await field();

    // Tab onto Semis workspace -> calls includeContent=false with workspaceId='ws1' and shows Personal Memo + trigger row
    fireEvent.change(input, { target: { value: 'Semis' } });
    await waitFor(() =>
      expect(document.querySelector('[role="option"][aria-selected="true"]')).toHaveTextContent(/Semis/),
    );
    fireEvent.keyDown(input, { key: 'Tab' });
    fireEvent.change(input, { target: { value: 'NVDA' } });

    await screen.findByText('Personal Memo');
    expect(fetchGlobalSearch).toHaveBeenCalledWith('NVDA', 20, expect.anything(), {
      includeConversations: false,
      workspaceId: 'ws1',
      includeContent: false,
    });
    const wsTrigger = await screen.findByRole('option', { name: 'Search file content for “NVDA”' });
    fireEvent.click(wsTrigger);
    await screen.findByRole('option', { name: /ws1\.md/ });
    expect(screen.queryByRole('option', { name: /ws2\.md/ })).not.toBeInTheDocument();
    expect(fetchGlobalSearch).toHaveBeenLastCalledWith('NVDA', 20, expect.anything(), {
      includeConversations: false,
      workspaceId: 'ws1',
      includeContent: true,
    });

    // Escape back to workspace view, select NVDA margins thread -> thread scope hides both and stops calling fetchGlobalSearch
    fireEvent.keyDown(input, { key: 'Escape' });
    await screen.findByRole('option', { name: /NVDA margins/ });
    while (!document.querySelector('[role="option"][aria-selected="true"]')?.textContent?.includes('NVDA margins')) {
      fireEvent.keyDown(input, { key: 'ArrowUp' });
    }
    vi.mocked(fetchGlobalSearch).mockClear();
    fireEvent.keyDown(input, { key: 'Tab' });
    fireEvent.change(input, { target: { value: 'NVDA' } });
    await waitFor(() => {
      expect(screen.queryByText('Personal Memo')).not.toBeInTheDocument();
      expect(screen.queryByRole('option', { name: /Search file content/ })).not.toBeInTheDocument();
    });
    expect(fetchGlobalSearch).not.toHaveBeenCalled();
  });

  it('navigates with openFilePath state when selecting a memo or workspace_file result', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockResolvedValue({
      conversations: [],
      documents: [
        {
          kind: 'memo',
          memo_key: 'pick.md',
          workspace_id: null,
          path: `${MEMO_USER_DIR}/pick.md`,
          title: 'Pick Memo',
          workspace_name: '',
          timestamp: null,
          snippet: 'Pick this memo',
          highlights: [[0, 4]],
        },
        {
          kind: 'workspace_file',
          memo_key: null,
          workspace_id: 'ws1',
          path: 'models/dcf.xlsx',
          title: 'dcf.xlsx',
          workspace_name: 'Semis',
          timestamp: null,
          snippet: 'Pick this model',
          highlights: [[0, 4]],
        },
      ],
    });

    renderDocShell();
    shortcut();
    let input = await field();
    fireEvent.change(input, { target: { value: 'Pick' } });

    const memoOpt = await screen.findByRole('option', { name: /Pick Memo/ });
    fireEvent.click(memoOpt);
    await waitFor(() => expect(path()).toBe('/chat/home'));
    expect(JSON.parse(screen.getByLabelText('RouteState').textContent || 'null')).toEqual({
      openFilePath: `${MEMO_USER_DIR}/pick.md`,
      workspaceId: 'home',
    });

    shortcut();
    input = await field();
    fireEvent.change(input, { target: { value: 'Pick' } });
    fireEvent.click(await screen.findByRole('option', { name: 'Search file content for “Pick”' }));
    const fileOpt = await screen.findByRole('option', { name: /dcf\.xlsx/ });
    fireEvent.click(fileOpt);
    await waitFor(() => expect(path()).toBe('/chat/ws1'));
    expect(JSON.parse(screen.getByLabelText('RouteState').textContent || 'null')).toEqual({
      openFilePath: 'models/dcf.xlsx',
      workspaceId: 'ws1',
    });
  });

  it('passes workspaceId in File content view so >20 other-workspace hits do not crowd out workspace results', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockImplementation(async (_q, _limit, _signal, options) => {
      if (options?.workspaceId === 'ws1') {
        return {
          conversations: [],
          documents: [
            {
              kind: 'workspace_file',
              memo_key: null,
              workspace_id: 'ws1',
              path: 'notes/semis_only.md',
              title: 'semis_only.md',
              workspace_name: 'Semis',
              timestamp: null,
              snippet: 'Alpha wafer yield',
              highlights: [[0, 5]],
            },
          ],
        };
      }
      return {
        conversations: [],
        documents: Array.from({ length: 20 }, (_, i) => ({
          kind: 'workspace_file' as const,
          memo_key: null,
          workspace_id: 'ws-other',
          path: `other/doc-${i}.md`,
          title: `doc-${i}.md`,
          workspace_name: 'Other',
          timestamp: null,
          snippet: `Alpha other ${i}`,
          highlights: [[0, 5]] as [number, number][],
        })),
      };
    });

    renderDocShell();
    shortcut();
    const input = await field();

    fireEvent.change(input, { target: { value: 'Semis' } });
    await waitFor(() =>
      expect(document.querySelector('[role="option"][aria-selected="true"]')).toHaveTextContent(/Semis/),
    );
    fireEvent.keyDown(input, { key: 'Tab' });
    fireEvent.change(input, { target: { value: 'Alpha' } });

    const trigger = await screen.findByRole('option', { name: 'Search file content for “Alpha”' });
    fireEvent.click(trigger);
    await screen.findByRole('option', { name: /semis_only\.md/ });
    expect(screen.queryByRole('option', { name: /doc-0\.md/ })).not.toBeInTheDocument();
  });

  it('supports keyboard navigation to Search file content row and Show all memos in root and workspace scopes', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockResolvedValue({
      conversations: [],
      documents: [
        ...Array.from({ length: 6 }, (_, i) => ({
          kind: 'memo' as const,
          memo_key: `memo-${i}.md`,
          workspace_id: null,
          path: `${MEMO_USER_DIR}/memo-${i}.md`,
          title: `Memo ${i}`,
          workspace_name: '',
          timestamp: null,
          snippet: `Yield memo ${i}`,
          highlights: [[0, 5]] as [number, number][],
        })),
        ...Array.from({ length: 6 }, (_, i) => ({
          kind: 'workspace_file' as const,
          memo_key: null,
          workspace_id: 'ws1',
          path: `notes/fc-${i}.md`,
          title: `fc-${i}.md`,
          workspace_name: 'Semis',
          timestamp: null,
          snippet: `Yield file ${i}`,
          highlights: [[0, 5]] as [number, number][],
        })),
      ],
    });

    renderDocShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'Yield' } });

    const showAllMemos = await screen.findByRole('option', { name: 'Show all memos' });
    const triggerRow = screen.getByRole('option', { name: 'Search file content for “Yield”' });
    expect(screen.queryByText('Memo 5')).not.toBeInTheDocument();
    expect(screen.queryByText('fc-0.md')).not.toBeInTheDocument();

    // Activate "Show all memos" -> dedicated Memos view shows all 6 memos
    fireEvent.click(showAllMemos);
    await screen.findByRole('option', { name: /Memo 5/ });
    expect(screen.getByRole('listbox')).toHaveAccessibleName('Memos');
    expect(screen.queryByRole('option', { name: /fc-0\.md/ })).not.toBeInTheDocument();

    // Escape back to root, arrow down onto Search file content trigger row, and press Enter
    fireEvent.keyDown(input, { key: 'Escape' });
    await screen.findByRole('option', { name: 'Search file content for “Yield”' });
    while (document.querySelector('[role="option"][aria-selected="true"]')?.textContent !== triggerRow.textContent) {
      fireEvent.keyDown(input, { key: 'ArrowDown' });
    }
    fireEvent.keyDown(input, { key: 'Enter' });
    await screen.findByRole('option', { name: /fc-5\.md/ });
    expect(screen.getByRole('listbox')).toHaveAccessibleName('File content');

    // Escape back, scope into Semis workspace, verify Show all memos & trigger row also work there
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.change(input, { target: { value: 'Semis' } });
    await waitFor(() =>
      expect(document.querySelector('[role="option"][aria-selected="true"]')).toHaveTextContent(/Semis/),
    );
    fireEvent.keyDown(input, { key: 'Tab' });
    fireEvent.change(input, { target: { value: 'Yield' } });

    await screen.findByRole('option', { name: 'Show all memos' });
    fireEvent.click(await screen.findByRole('option', { name: 'Search file content for “Yield”' }));
    await screen.findByRole('option', { name: /fc-5\.md/ });
    expect(screen.getByRole('listbox')).toHaveAccessibleName('File content');
  });

  it('shows timeout notice and preserves fallback name hits when content_timed_out is true, and aborts in-flight content request on query change/exit', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });

    const abortedSignals: AbortSignal[] = [];
    vi.mocked(fetchGlobalSearch).mockImplementation(async (q, _limit, signal, options) => {
      if (options?.includeContent && q === 'slow') {
        if (signal) abortedSignals.push(signal);
        return new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' })));
        });
      }
      if (options?.includeContent && q === 'timeout') {
        return {
          conversations: [],
          content_timed_out: true,
          documents: [
            {
              kind: 'workspace_file',
              memo_key: null,
              workspace_id: 'ws1',
              path: 'notes/timeout_name.md',
              title: 'timeout_name.md',
              workspace_name: 'Semis',
              timestamp: null,
              snippet: 'notes/timeout_name.md',
              highlights: [[6, 13]],
            },
          ],
        };
      }
      return { conversations: [], documents: [] };
    });

    renderDocShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'timeout' } });

    fireEvent.click(await screen.findByRole('option', { name: 'Search file content for “timeout”' }));
    await screen.findByText('Too many files to search content in time. Try a more specific keyword or scope to a workspace.');
    expect(screen.getByRole('option', { name: /timeout_name\.md/ })).toBeInTheDocument();

    // Typing 'slow' inside File content triggers in-flight request; leaving with Escape aborts it cleanly
    vi.useFakeTimers();
    fireEvent.change(input, { target: { value: 'slow' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(650); });
    vi.useRealTimers();
    expect(abortedSignals).toHaveLength(1);
    expect(abortedSignals[0].aborted).toBe(false);

    fireEvent.keyDown(input, { key: 'Escape' });
    await waitFor(() => expect(abortedSignals[0].aborted).toBe(true));
  });

  it('hides PreviewPane when an upstream file row is selected in `/` view and shows it on memo/file-content rows', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [
        {
          workspace_id: 'ws1',
          workspace_name: 'Semis',
          path: 'reports/plain.md',
          name: 'plain.md',
          size: 120,
          modified_at: '2026-11-08T00:00:00Z',
          match: [[8, 13]],
        },
      ],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockResolvedValue({
      conversations: [],
      documents: [
        {
          kind: 'memo',
          memo_key: 'note.md',
          workspace_id: null,
          path: `${MEMO_USER_DIR}/note.md`,
          title: 'Plain Note Memo',
          workspace_name: '',
          timestamp: null,
          snippet: 'Plain snippet preview body',
          highlights: [[0, 5]],
        },
      ],
    });

    renderDocShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: '/' } });
    fireEvent.change(input, { target: { value: 'plain' } });

    await screen.findByRole('option', { name: /plain\.md/ });
    await screen.findByRole('option', { name: /Plain Note Memo/ });

    // Row 0 is upstream ordinary file (`plain.md`): PreviewPane must remain hidden.
    expect(document.querySelector('[role="option"][aria-selected="true"]')).toHaveTextContent(/plain\.md/);
    expect(document.querySelector('.command-palette-pane')).toBeNull();
    expect(document.querySelector('.command-palette-main')).not.toHaveClass('is-paned');

    // ArrowDown onto Memo hit -> PreviewPane mounts with snippet.
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    await waitFor(() => expect(document.querySelector('.command-palette-pane')).not.toBeNull());
    expect(document.querySelector('.command-palette-pane')?.textContent).toContain('Plain snippet preview body');

    // ArrowUp back onto ordinary file -> PreviewPane hides again.
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    await waitFor(() => expect(document.querySelector('.command-palette-pane')).toBeNull());
  });

  it('shows palette.busy copy when Dalton document search returns 429', async () => {
    vi.mocked(searchConversations).mockResolvedValue({
      threads: [],
      turns: [],
      files: [],
      next_cursor: { threads: null, turns: null, files: null },
      failed: [],
    });
    vi.mocked(fetchGlobalSearch).mockRejectedValue(
      Object.assign(new Error('Request failed with status code 429'), { response: { status: 429, data: {} } }),
    );

    renderDocShell();
    shortcut();
    const input = await field();
    fireEvent.change(input, { target: { value: 'margin' } });

    await screen.findByText('Too many searches at once. Try again in a moment.');
    expect(screen.queryByText("Couldn't search your memos and file contents right now.")).not.toBeInTheDocument();
  });
});
