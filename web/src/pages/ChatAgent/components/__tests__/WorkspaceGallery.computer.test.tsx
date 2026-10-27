/**
 * What a card says about the machine underneath it.
 *
 * The rows are the live wt3 ones: two workspaces on computer 5319ad0e, each
 * with its own `dir_name`. The card's state has to come from the computer,
 * because that is what starts and stops.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';

import { renderWithProviders } from '@/test/utils';
import type { Workspace } from '@/types/api';

const COMPUTER_ID = '5319ad0e-7835-4ca6-9541-b7c2961a7bf6';

const WORKSPACES: Workspace[] = [
  {
    workspace_id: 'aecc944a-61b4-4e68-8a1d-28e1b1823001',
    name: 'Live Alpha',
    status: 'running',
    is_home: false,
    computer_id: COMPUTER_ID,
    dir_name: 'live-alpha-16ad',
    resource_tier: 'standard',
    updated_at: '2026-09-16T02:23:43.210835Z',
  },
  {
    workspace_id: '01dae5b9-edb4-4afb-966e-920039c6d0ac',
    name: 'Live Beta',
    status: 'running',
    is_home: false,
    computer_id: COMPUTER_ID,
    dir_name: 'live-beta-1d6d',
    resource_tier: 'standard',
    updated_at: '2026-09-16T02:23:43.210835Z',
  },
];

vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: vi.fn(() => ({
    data: { workspaces: WORKSPACES, total: 2 },
    isLoading: false,
    error: null,
  })),
}));

vi.mock('../../utils/api', () => ({
  createWorkspace: vi.fn(),
  getHomeWorkspace: vi.fn(async () => null),
  reorderWorkspaces: vi.fn(),
  renameWorkspace: vi.fn(),
  getComputers: vi.fn(),
  getComputerProviders: vi.fn(async () => ({ providers: [] })),
  streamComputerEvents: vi.fn(async () => {}),
  streamWorkspaceEvents: vi.fn(async () => {}),
  createComputer: vi.fn(),
  startComputer: vi.fn(),
  stopComputer: vi.fn(),
  getWorkspaceQuota: vi.fn(async () => ({ performance: null, max: null, always_on: null })),
  formatApiErrorDetail: (err: unknown) => (err as { message?: string })?.message ?? 'Request failed',
  uploadWorkspaceFile: vi.fn(),
}));

vi.mock('../workspaceActions', () => ({
  WorkspaceMenuItems: () => null,
  useWorkspaceActions: () => ({
    openUpgrade: vi.fn(),
    toggleAlwaysOn: vi.fn(),
    openDuplicate: vi.fn(),
    openDelete: vi.fn(),
    dialogs: null,
  }),
}));

vi.mock('../../hooks/workspaceRowActions', () => ({ pinWorkspaceRow: vi.fn() }));
vi.mock('../../hooks/useNavigationData', () => ({ isEffectivelyPinned: () => false }));
vi.mock('../../hooks/utils/chatSessionRestore', () => ({ clearChatSession: vi.fn() }));

import { getComputerProviders, getComputers } from '../../utils/api';
import { useWorkspaces } from '@/hooks/useWorkspaces';
import WorkspaceGallery from '../WorkspaceGallery';

const mockGetComputers = getComputers as Mock;
const mockUseWorkspaces = useWorkspaces as Mock;

function computerList(status: string) {
  return {
    computers: [
      {
        computer_id: COMPUTER_ID,
        user_id: 'wp17-live-user',
        kind: 'daytona',
        name: 'Alpha Research',
        status,
        resource_tier: 'standard',
        is_always_on: false,
        is_primary: true,
        root_dir: '/home/workspace',
      },
    ],
    total: 1,
  };
}

// jsdom has no scrollTo on Element; the gallery snaps its scroll on mount.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = vi.fn() as unknown as Element['scrollTo'];
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetComputers.mockResolvedValue(computerList('running'));
});

describe('WorkspaceCard, machine line', () => {
  it('asks the paginated server list to include Home in its custom ordering', async () => {
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);

    await screen.findAllByTestId('workspace-card');
    expect(mockUseWorkspaces).toHaveBeenCalledWith(expect.objectContaining({
      includeHome: true,
      offset: 0,
    }));
  });

  it('names the computer and not the folder, which the title already spells', async () => {
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);

    const cards = await screen.findAllByTestId('workspace-card');
    expect(cards).toHaveLength(2);

    // Both cards name the shared machine; neither repeats its own folder.
    expect(await within(cards[0]).findByText('Alpha Research')).toBeInTheDocument();
    expect(within(cards[0]).queryByText('live-alpha-16ad')).toBeNull();
    expect(within(cards[1]).getByText('Alpha Research')).toBeInTheDocument();
    expect(within(cards[1]).queryByText('live-beta-1d6d')).toBeNull();
  });

  it('reads an unreadable update time as recent instead of failing the card', async () => {
    const listed = mockUseWorkspaces.getMockImplementation();
    mockUseWorkspaces.mockImplementation(() => ({
      data: { workspaces: [{ ...WORKSPACES[0], updated_at: 'not a date' }, WORKSPACES[1]], total: 2 },
      isLoading: false,
      error: null,
    }));
    try {
      renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);
      const cards = await screen.findAllByTestId('workspace-card');
      expect(within(cards[0]).getByText('Updated recently')).toBeInTheDocument();
    } finally {
      mockUseWorkspaces.mockImplementation(listed!);
    }
  });

  it('shows the machine\'s state as words, on every card that shares it', async () => {
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);
    const cards = await screen.findAllByTestId('workspace-card');
    // The state arrives with the computer list, so the first read awaits it.
    expect(await within(cards[0]).findByText('Running')).toBeInTheDocument();
    expect(within(cards[1]).getByText('Running')).toBeInTheDocument();
  });

  it('reads the state from the computer, not from the workspace row', async () => {
    // The rows still say 'running'; the machine has since been stopped. The
    // machine is the one that decides.
    mockGetComputers.mockResolvedValue(computerList('stopped'));
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);

    const cards = await screen.findAllByTestId('workspace-card');
    expect(await within(cards[0]).findByText('Stopped')).toBeInTheDocument();
    expect(within(cards[1]).getByText('Stopped')).toBeInTheDocument();
    expect(screen.queryByText('Running')).not.toBeInTheDocument();
  });

  it('offers the computers surface from the gallery header', async () => {
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);
    const buttons = await screen.findAllByRole('button', { name: /computers/i });
    expect(buttons.length).toBeGreaterThan(0);
  });

  it('hands the banner to the next full machine when one is dismissed', async () => {
    // Two machines equally low: dismissing the one shown must not silence the other.
    const disk = {
      used_bytes: 9_000_000_000,
      total_bytes: 10_000_000_000,
      free_bytes: 1_000_000_000,
      measured_at: '2026-09-16T02:23:43.210835Z',
      level: 'warning',
    };
    const [machine] = computerList('running').computers;
    mockGetComputers.mockResolvedValue({
      computers: [
        { ...machine, computer_id: 'disk-a', name: 'Disk A', disk },
        { ...machine, computer_id: 'disk-b', name: 'Disk B', is_primary: false, disk },
      ],
      total: 2,
    });
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);

    expect(await screen.findByText('Disk A is running low on disk space')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(await screen.findByText('Disk B is running low on disk space')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(screen.queryByTestId('disk-warning')).not.toBeInTheDocument();
  });

  it.each([
    ['daytona', false],
    ['e2b', true],
  ])('offers a bigger size to a full Large machine on %s only where one runs', async (kind, offered) => {
    const disk: Record<string, number> = { standard: 5, performance: 10, max: 10, xlarge: 20 };
    const spec = (tier: string) => ({ tier, cpu: 2, memory_gib: 8, disk_gib: disk[tier] });
    (getComputerProviders as Mock).mockResolvedValue({
      providers: [
        { kind: 'daytona', is_default: true, allowed: null, tiers: ['standard', 'performance', 'max'].map(spec) },
        { kind: 'e2b', is_default: false, allowed: true, tiers: ['standard', 'performance', 'max', 'xlarge'].map(spec) },
      ],
    });
    const [machine] = computerList('running').computers;
    mockGetComputers.mockResolvedValue({
      computers: [{
        ...machine,
        kind,
        resource_tier: 'max',
        disk: {
          used_bytes: 9_000_000_000,
          total_bytes: 10_000_000_000,
          free_bytes: 1_000_000_000,
          measured_at: '2026-09-16T02:23:43.210835Z',
          level: 'warning',
        },
      }],
      total: 1,
    });
    renderWithProviders(<WorkspaceGallery onWorkspaceSelect={vi.fn()} />);

    const warning = await screen.findByTestId('disk-warning');
    await waitFor(() => expect(getComputerProviders).toHaveBeenCalled());
    if (offered) expect(await within(warning).findByRole('button', { name: 'Change spec' })).toBeInTheDocument();
    else {
      // Let the list land before reading its absence.
      await act(async () => {});
      expect(within(warning).queryByRole('button', { name: 'Change spec' })).not.toBeInTheDocument();
    }
  });
});
