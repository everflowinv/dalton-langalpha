/**
 * The computer surface, rendered against the live `GET /api/v1/computers`
 * payload from the wt3 stack.
 *
 * `@/config/hostMode` is mocked rather than inherited: `web/.env` sets
 * VITE_HOST_MODE and Vite loads it for the Vitest run too, so a test that let
 * the mode default would be asserting whichever mode the developer's env
 * happened to carry.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '@/test/utils';
import { runAuthResets } from '@/lib/authResets';
import { queryKeys } from '@/lib/queryKeys';

vi.mock('@/config/hostMode', () => ({
  HOST_MODE: 'platform',
  isPlatformMode: true,
  APP_ENTRY_PATH: '/app',
}));

vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));

vi.mock('../../utils/api', async () => {
  // The real readers, so a refusal is worded by the same code as in the app.
  const errors = await vi.importActual<typeof import('../../utils/api/errors')>('../../utils/api/errors');
  return {
    getComputers: vi.fn(),
    getComputerProviders: vi.fn(),
    createComputer: vi.fn(),
    startComputer: vi.fn(),
    stopComputer: vi.fn(),
    deleteComputer: vi.fn(),
    setComputerAlwaysOn: vi.fn(),
    setComputerFilesBackup: vi.fn(),
    apiErrorObject: errors.apiErrorObject,
    apiErrorReason: errors.apiErrorReason,
    getWorkspaceQuota: vi.fn(async () => ({ performance: null, max: null, always_on: null })),
    streamComputerEvents: vi.fn(async () => {}),
    streamWorkspaceEvents: vi.fn(async () => {}),
    apiErrorStatus: errors.apiErrorStatus,
    apiErrorDetailMessage: errors.apiErrorDetailMessage,
    formatApiErrorDetail: (err: unknown) =>
      (err as { message?: string })?.message ?? 'Request failed',
  };
});

import { toast } from '@/components/ui/use-toast';
import {
  createComputer,
  deleteComputer,
  getComputerProviders,
  getComputers,
  getWorkspaceQuota,
  setComputerAlwaysOn,
  setComputerFilesBackup,
  startComputer,
  stopComputer,
} from '../../utils/api';
import type { ComputerProviderOption } from '@/types/api';
import { recordPrepareStage } from '../../hooks/computerPreparation';
import ComputersDialog from '../ComputersDialog';

const mockGetComputers = getComputers as Mock;
const mockProviders = getComputerProviders as Mock;
const mockQuota = getWorkspaceQuota as Mock;
const mockCreate = createComputer as Mock;
const mockStart = startComputer as Mock;
const mockStop = stopComputer as Mock;
const mockDelete = deleteComputer as Mock;
const mockSetAlwaysOn = setComputerAlwaysOn as Mock;
const mockSetFilesBackup = setComputerFilesBackup as Mock;
const mockToast = toast as unknown as Mock;

const PRIMARY_ID = '5319ad0e-7835-4ca6-9541-b7c2961a7bf6';
const SECOND_ID = '3b788b47-352e-4cfb-94f0-920b599ea227';

// Verbatim from curl against :8060. `workspace_count` is the server's own
// count of the folders on each machine, which is the whole point: the dialog is
// handed no workspace list to count for itself.
const LIVE_LIST = {
  computers: [
    {
      computer_id: PRIMARY_ID,
      user_id: 'wp13-final-1789525377',
      kind: 'daytona',
      name: 'Alpha Research',
      status: 'running',
      resource_tier: 'standard',
      is_always_on: false,
      is_primary: true,
      workspace_count: 3,
      root_dir: '/home/workspace',
      provider_ref: '91885905-79ed-48dc-81ac-0a3d2d608458',
      config: {},
    },
    {
      computer_id: SECOND_ID,
      user_id: 'wp13-final-1789525377',
      kind: 'daytona',
      name: 'Second machine',
      status: 'stopped',
      resource_tier: 'performance',
      is_always_on: false,
      is_primary: false,
      workspace_count: 0,
      root_dir: '/home/workspace',
      provider_ref: null,
      config: {},
    },
  ],
  total: 2,
};

const NEW_ID = 'b7d1f0c2-9a4e-4f3b-8c61-2e5d7a9f0b13';

// The row `POST /api/v1/computers` answers with: its sandbox is being built.
const CREATED = {
  computer_id: NEW_ID,
  user_id: 'wp13-final-1789525377',
  kind: 'e2b',
  name: 'Research box',
  status: 'starting',
  resource_tier: 'performance',
  is_always_on: false,
  is_primary: false,
  workspace_count: 0,
  root_dir: '/home/workspace',
  provider_ref: null,
  config: {},
};

const DAYTONA_ONLY: ComputerProviderOption[] = [{ kind: 'daytona', is_default: true, allowed: null }];
const DAYTONA_AND_E2B: ComputerProviderOption[] = [
  { kind: 'daytona', is_default: true, allowed: null },
  { kind: 'e2b', is_default: false, allowed: true },
];
const E2B_NOT_ON_PLAN: ComputerProviderOption[] = [
  { kind: 'daytona', is_default: true, allowed: null },
  { kind: 'e2b', is_default: false, allowed: false },
];

function open() {
  return renderWithProviders(<ComputersDialog open onOpenChange={() => {}} />);
}

async function openCreateForm(user: ReturnType<typeof userEvent.setup>) {
  open();
  await user.click(await screen.findByRole('button', { name: 'New computer' }));
  return screen.findByRole('dialog', { name: 'New computer' });
}

function radio(name: string) {
  return screen.getByRole('radio', { name: new RegExp(`^${name}`) });
}

/** The list with the second machine changed, as the server would report it. */
function withSecond(patch: Record<string, unknown>) {
  return {
    ...LIVE_LIST,
    computers: LIVE_LIST.computers.map((computer) =>
      computer.computer_id === SECOND_ID ? { ...computer, ...patch } : computer),
  };
}

async function secondRow() {
  return (await screen.findAllByTestId('computer-row'))[1];
}

/** A 409 as the API client rejects it, with the route's coded detail. */
function conflict(code: string, message: string) {
  const detail = { code, message };
  return Object.assign(new Error('Request failed with status code 409'), {
    status: 409,
    response: { status: 409, data: { detail }, headers: {} },
  });
}

// Found by its title: covered, it is hidden from the tree, and a role query
// cannot name a dialog whose label sits inside the hidden part.
const computersDialog = () =>
  screen.getByText('Computers', { selector: 'h3' }).closest<HTMLElement>('[role="dialog"]') as HTMLElement;
// The shell's backdrop is the dialog's parent; it carries the dimming.
const backdropOf = (dialog: HTMLElement) => dialog.parentElement as HTMLElement;

const scrollIntoView = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  // The preparation stages are module state; start each case from none.
  runAuthResets();
  mockGetComputers.mockResolvedValue(LIVE_LIST);
  mockProviders.mockResolvedValue({ providers: DAYTONA_ONLY });
  // jsdom has no layout, so no scrollIntoView either.
  Element.prototype.scrollIntoView = scrollIntoView;
});

afterEach(() => {
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

describe('ComputersDialog', () => {
  it('lists each machine with its status, tier and primary badge', async () => {
    open();
    expect(await screen.findByText('Alpha Research')).toBeInTheDocument();
    expect(screen.getByText('Second machine')).toBeInTheDocument();

    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.getByText('Stopped')).toBeInTheDocument();

    expect(screen.getByText('Standard')).toBeInTheDocument();
    expect(screen.getByText('Performance')).toBeInTheDocument();

    // Only the primary is badged.
    expect(screen.getAllByText('Primary')).toHaveLength(1);
  });

  it("says how many workspaces a machine holds, from the machine's own count", async () => {
    open();
    expect(await screen.findByText('3 workspaces')).toBeInTheDocument();
    expect(screen.getByText('0 workspaces')).toBeInTheDocument();
  });

  it('stops the running machine', async () => {
    const user = userEvent.setup();
    mockStop.mockResolvedValue({ computer_id: PRIMARY_ID, status: 'stopped', message: 'ok' });
    const { queryClient } = open();
    const detail = queryKeys.workspaces.detail('cached-sibling');
    queryClient.setQueryDefaults(detail, { gcTime: Infinity });
    queryClient.setQueryData(detail, { status: 'running', computer_id: PRIMARY_ID });

    await user.click(await screen.findByRole('button', { name: /stop/i }));
    expect(mockStop).toHaveBeenCalledWith(PRIMARY_ID);
    await waitFor(() => expect(queryClient.getQueryState(detail)?.isInvalidated).toBe(true));
    expect(queryClient.getQueryData(detail)).toMatchObject({ status: 'stopped' });
  });

  it('starts a stopped machine lazily, so the stream reports the boot', async () => {
    const user = userEvent.setup();
    mockStart.mockResolvedValue({ computer_id: SECOND_ID, status: 'starting', message: 'ok' });
    open();

    // Two rows, one Start button: the running machine offers Stop instead.
    await user.click(await screen.findByRole('button', { name: /start/i }));
    expect(mockStart).toHaveBeenCalledWith(SECOND_ID, { lazy: true });
  });

  it.each(['creating', 'starting'])('shows a %s machine with no sandbox yet as being prepared, on step one', async (status) => {
    mockGetComputers.mockResolvedValue({
      ...LIVE_LIST,
      computers: LIVE_LIST.computers.map((computer) =>
        computer.computer_id === SECOND_ID ? { ...computer, status } : computer),
    });
    open();

    const row = (await screen.findAllByTestId('computer-row'))[1];
    // The first frame usually goes out before anyone listens, so no stage is step one.
    expect(row).toHaveTextContent('Preparing · Creating the sandbox (1 of 3)');
    expect(row).toHaveTextContent("You can create workspaces on it once it's ready.");
    expect(within(row).getByRole('button', { name: /start/i })).toBeDisabled();
    // An ordinary restart keeps its own word.
    expect(screen.queryByText('Starting')).not.toBeInTheDocument();
  });

  it('moves the step on as the stream names it, and never back', async () => {
    mockGetComputers.mockResolvedValue({ computers: [CREATED], total: 1 });
    open();
    const row = await screen.findByTestId('computer-row');

    act(() => recordPrepareStage(NEW_ID, 'tools'));
    expect(row).toHaveTextContent('Preparing · Installing tools (2 of 3)');
    act(() => recordPrepareStage(NEW_ID, 'sandbox'));
    expect(row).toHaveTextContent('Preparing · Installing tools (2 of 3)');
    act(() => recordPrepareStage(NEW_ID, 'finishing'));
    expect(row).toHaveTextContent('Preparing · Finishing setup (3 of 3)');
  });

  it('falls back to the plain label for a stage this build does not know', async () => {
    mockGetComputers.mockResolvedValue({ computers: [CREATED], total: 1 });
    open();
    const row = await screen.findByTestId('computer-row');

    act(() => recordPrepareStage(NEW_ID, 'warming_cache'));
    expect(row).toHaveTextContent('Preparing');
    expect(row).not.toHaveTextContent(/of 3/);
    expect(row).not.toHaveTextContent('warming_cache');
  });

  it('keeps "Starting" for a machine that already has a sandbox', async () => {
    mockGetComputers.mockResolvedValue({
      ...LIVE_LIST,
      computers: [{ ...LIVE_LIST.computers[0], status: 'starting' }],
      total: 1,
    });
    open();
    expect(await screen.findByText('Starting')).toBeInTheDocument();
    expect(screen.queryByText(/Preparing/)).not.toBeInTheDocument();
  });

  it.each([
    ['starting', true],
    ['stopping', true],
    ['running', false],
  ])('holds the always-on switch while a machine is %s', async (status, disabled) => {
    mockGetComputers.mockResolvedValue({
      ...LIVE_LIST,
      computers: [{ ...LIVE_LIST.computers[0], status, is_always_on: true }],
      total: 1,
    });
    open();

    const toggle = await screen.findByRole('switch', { name: 'Always-on' });
    if (disabled) expect(toggle).toBeDisabled();
    else expect(toggle).toBeEnabled();
  });

  it('creates a computer on the chosen provider and tier, then scrolls to its row', async () => {
    const user = userEvent.setup();
    mockProviders.mockResolvedValue({ providers: DAYTONA_AND_E2B });
    mockQuota.mockResolvedValue({ performance: { used: 0, limit: 2 }, max: null, always_on: null });
    mockCreate.mockImplementation(async () => {
      mockGetComputers.mockResolvedValue({ computers: [...LIVE_LIST.computers, CREATED], total: 3 });
      return CREATED;
    });
    await openCreateForm(user);

    // The deployment default is picked until the user picks another.
    expect(await screen.findByRole('radiogroup', { name: 'Provider' })).toBeInTheDocument();
    expect(radio('Daytona')).toBeChecked();
    await user.click(radio('E2B'));
    await user.click(radio('Performance'));
    await user.type(screen.getByLabelText(/^Name/), 'Research box');
    await user.click(screen.getByRole('button', { name: 'Create computer' }));

    expect(mockCreate).toHaveBeenCalledWith({ name: 'Research box', resource_tier: 'performance', kind: 'e2b' });
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New computer' })).not.toBeInTheDocument());
    const rows = await screen.findAllByTestId('computer-row');
    const row = rows.find((r) => r.textContent?.includes('Research box'));
    expect(row).toBeDefined();
    expect(scrollIntoView.mock.contexts).toContain(row);
    // Nothing waits on the build: the row says it is under way.
    expect(row).toHaveTextContent('Preparing · Creating the sandbox (1 of 3)');
  });

  it('shows a provider the plan lacks as locked, and where to upgrade', async () => {
    const user = userEvent.setup();
    mockProviders.mockResolvedValue({ providers: E2B_NOT_ON_PLAN });
    mockCreate.mockResolvedValue({ ...CREATED, kind: 'daytona', resource_tier: 'standard' });
    await openCreateForm(user);

    await screen.findByRole('radiogroup', { name: 'Provider' });
    const e2b = radio('E2B');
    expect(e2b).toBeDisabled();
    expect(e2b).toHaveAccessibleDescription(/^Not on your plan/);
    await user.click(e2b);
    expect(e2b).not.toBeChecked();
    expect(radio('Daytona')).toBeChecked();
    expect(screen.getByTestId('create-computer-plan-cta')).toHaveTextContent(
      "Need a provider your plan doesn't include? Upgrade plan",
    );

    await user.click(screen.getByRole('button', { name: 'Create computer' }));
    expect(mockCreate).toHaveBeenCalledWith({ resource_tier: 'standard', kind: 'daytona' });
  });

  it('offers no provider choice with one provider, and sends none', async () => {
    const user = userEvent.setup();
    mockCreate.mockResolvedValue({ ...CREATED, kind: 'daytona', resource_tier: 'standard' });
    await openCreateForm(user);

    expect(await screen.findByRole('radiogroup', { name: 'Computer spec' })).toBeInTheDocument();
    await waitFor(() => expect(mockProviders).toHaveBeenCalled());
    expect(screen.queryByRole('radiogroup', { name: 'Provider' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('create-computer-plan-cta')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Create computer' }));
    expect(mockCreate).toHaveBeenCalledWith({ resource_tier: 'standard' });
  });

  it('keeps the form open and relays the quota service\'s sentence when a create is refused', async () => {
    const user = userEvent.setup();
    const platformMessage = 'You are using 3 of 3 computers on the Starter plan. Upgrade or stop one to add another.';
    const detail = { message: platformMessage, type: 'workspace_limit', current: 3, limit: 3, remaining: 0 };
    mockCreate.mockRejectedValue(
      Object.assign(new Error(platformMessage), {
        status: 429,
        response: { status: 429, data: { detail }, headers: {} },
        rateLimitInfo: detail,
      }),
    );
    await openCreateForm(user);

    await user.type(screen.getByLabelText(/^Name/), 'One too many');
    await user.click(screen.getByRole('button', { name: 'Create computer' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(platformMessage);
    expect(screen.getByRole('dialog', { name: 'New computer' })).toBeInTheDocument();
    expect(screen.getByLabelText(/^Name/)).toHaveValue('One too many');
  });

  it.each([
    [1, 'You can have only 1 SSH computer. Remove it to add another.'],
    [3, 'You can have up to 3 SSH computers. Remove one to add another.'],
  ])('words the SSH computer cap (limit %i) in its own copy on a create', async (limit, copy) => {
    const user = userEvent.setup();
    const detail = { code: 'ssh_computer_limit', message: 'server words', limit };
    mockCreate.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 409'), {
        status: 409,
        response: { status: 409, data: { detail }, headers: {} },
      }),
    );
    await openCreateForm(user);

    await user.type(screen.getByLabelText(/^Name/), 'One too many');
    await user.click(screen.getByRole('button', { name: 'Create computer' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy);
  });

  it.each([
    ['one provider, one kind', DAYTONA_ONLY, LIVE_LIST.computers, false],
    ['two providers', DAYTONA_AND_E2B, LIVE_LIST.computers, true],
    ['machines on two kinds', DAYTONA_ONLY, [...LIVE_LIST.computers, CREATED], true],
  ])('names the provider on the rows only when there is more than one (%s)', async (_case, providers, computers, shown) => {
    mockProviders.mockResolvedValue({ providers });
    mockGetComputers.mockResolvedValue({ computers, total: computers.length });
    open();
    expect(await screen.findByText('Alpha Research')).toBeInTheDocument();
    await waitFor(() => expect(mockProviders).toHaveBeenCalled());
    if (shown) expect(await screen.findAllByText('Daytona')).toHaveLength(2);
    else expect(screen.queryByText('Daytona')).not.toBeInTheDocument();
  });

  it('shows the quota service\'s own sentence on a 429, not copy written here', async () => {
    const user = userEvent.setup();
    const platformMessage =
      'You are using 3 of 3 computers on the Starter plan. Upgrade or stop one to add another.';
    mockStart.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 429'), {
        status: 429,
        rateLimitInfo: {
          message: platformMessage,
          type: 'workspace_limit',
          current: 3,
          limit: 3,
          remaining: 0,
        },
      }),
    );
    open();

    await user.click(await screen.findByRole('button', { name: /start/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(platformMessage);
    // Not the client's status line, and nothing this dialog composed.
    expect(alert).not.toHaveTextContent('status code 429');
  });

  it('does not fetch while closed', () => {
    renderWithProviders(<ComputersDialog open={false} onOpenChange={() => {}} />);
    expect(mockGetComputers).not.toHaveBeenCalled();
  });
});

describe('ComputersDialog: removing a computer', () => {
  it('offers Remove on every computer but the primary', async () => {
    open();
    const [primary, second] = await screen.findAllByTestId('computer-row');
    expect(within(primary).queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
    const remove = within(second).getByRole('button', { name: 'Remove' });
    expect(remove).toBeEnabled();
    expect(remove).not.toHaveAccessibleDescription();
  });

  it('offers Make primary on a hosted computer', async () => {
    open();
    expect(within(await secondRow()).getByRole('button', { name: 'Make primary' })).toBeInTheDocument();
  });

  it('never offers Make primary on a machine its owner brought', async () => {
    mockGetComputers.mockResolvedValue(
      withSecond({ kind: 'ssh', ssh: { host: 'box.example.com', port: 22, username: 'dev', fingerprint: 'SHA256:x' } }),
    );
    open();
    const owned = await secondRow();
    expect(within(owned).queryByRole('button', { name: 'Make primary' })).not.toBeInTheDocument();
    expect(within(owned).getByRole('button', { name: 'Remove' })).toBeInTheDocument();
  });

  it('names no tier for a machine its owner brought, even while its address is unknown', async () => {
    // Its owner sizes it, so the tier its row holds is no spec of theirs.
    mockGetComputers.mockResolvedValue(withSecond({ kind: 'ssh', ssh: null }));
    open();
    expect(await secondRow()).not.toHaveTextContent('Performance');
  });

  it.each([
    [2, 'Delete its 2 workspaces to remove it.'],
    [1, 'Delete its workspace to remove it.'],
  ])('holds Remove on a computer with %i workspace(s), and says why', async (count, hint) => {
    mockGetComputers.mockResolvedValue(withSecond({ workspace_count: count }));
    open();
    const remove = within(await secondRow()).getByRole('button', { name: 'Remove' });
    expect(remove).toBeDisabled();
    expect(remove).toHaveAccessibleDescription(hint);
  });

  it.each([
    [
      'a spec change is running',
      {
        spec_change: {
          target_tier: 'standard',
          from_tier: 'performance',
          state: 'in_progress',
          started_at: '2026-10-17T09:00:00Z',
        },
      },
      'You can remove it once the spec change finishes.',
    ],
    ['it is starting', { status: 'starting', provider_ref: 'sbx-1' }, 'You can remove it once it has started.'],
    ['it is stopping', { status: 'stopping', provider_ref: 'sbx-1' }, 'You can remove it once it has stopped.'],
  ])('holds Remove while %s', async (_case, patch, hint) => {
    mockGetComputers.mockResolvedValue(withSecond(patch));
    open();
    const remove = within(await secondRow()).getByRole('button', { name: 'Remove' });
    expect(remove).toBeDisabled();
    expect(remove).toHaveAccessibleDescription(hint);
  });

  it("points a preparing computer's Remove at the hint the row already shows", async () => {
    mockGetComputers.mockResolvedValue(withSecond({ status: 'starting' }));
    open();
    const row = await secondRow();
    const remove = within(row).getByRole('button', { name: 'Remove' });
    expect(remove).toBeDisabled();
    expect(remove).toHaveAccessibleDescription(/You can create workspaces on it once it's ready\./);
    expect(row).not.toHaveTextContent(/You can remove it/);
  });

  it('removes on confirm: the row leaves, a toast says so, and focus stays in the dialog', async () => {
    const user = userEvent.setup();
    mockDelete.mockImplementation(async () => {
      // The server's list excludes a removed computer from here on.
      mockGetComputers.mockResolvedValue({ computers: [LIVE_LIST.computers[0]], total: 1 });
      return { key: null };
    });
    open();
    await user.click(within(await secondRow()).getByRole('button', { name: 'Remove' }));

    const confirm = await screen.findByRole('dialog', { name: 'Remove Second machine?' });
    expect(confirm).toHaveTextContent("Its sandbox and everything on its disk are deleted. This can't be undone.");
    // A cloud computer has no key line to ask about.
    expect(within(confirm).queryByRole('checkbox')).not.toBeInTheDocument();
    await user.click(within(confirm).getByRole('button', { name: 'Remove computer' }));

    expect(mockDelete).toHaveBeenCalledWith(SECOND_ID, { removeKey: true });
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Remove Second machine?' })).not.toBeInTheDocument());
    expect(screen.getAllByTestId('computer-row')).toHaveLength(1);
    expect(screen.queryByText('Second machine')).not.toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith({ title: 'Second machine removed', description: undefined });
    // The button that opened the confirm went with its row.
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Computers' })).toContainElement(document.activeElement as HTMLElement));
  });

  it('tells the owner of a machine they brought that their files stay on it', async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue(withSecond({
      kind: 'ssh',
      root_dir: '/home/dev/langalpha',
      ssh: { host: 'box.example.com', port: 22, username: 'dev', fingerprint: 'SHA256:x' },
    }));
    open();
    await user.click(within(await secondRow()).getByRole('button', { name: 'Remove' }));

    const confirm = await screen.findByRole('dialog', { name: 'Remove Second machine?' });
    expect(confirm).toHaveTextContent('Your files in /home/dev/langalpha stay');
    expect(confirm).not.toHaveTextContent('everything on its disk are deleted');
  });

  describe('the key line on a machine its owner brought', () => {
    const SSH_ROW = {
      kind: 'ssh',
      root_dir: '/home/dev/langalpha',
      ssh: { host: 'box.example.com', port: 22, username: 'dev', fingerprint: 'SHA256:x' },
    };

    async function confirmSsh(user: ReturnType<typeof userEvent.setup>, uncheck = false) {
      mockGetComputers.mockResolvedValue(withSecond(SSH_ROW));
      open();
      await user.click(within(await secondRow()).getByRole('button', { name: 'Remove' }));
      const confirm = await screen.findByRole('dialog', { name: 'Remove Second machine?' });
      const box = within(confirm).getByRole('checkbox', { name: 'Remove our key from ~/.ssh/authorized_keys' });
      expect(box).toBeChecked();
      if (uncheck) await user.click(box);
      await user.click(within(confirm).getByRole('button', { name: 'Remove computer' }));
    }

    it('is checked by default and sends remove_key true', async () => {
      mockDelete.mockResolvedValue({ key: null });
      await confirmSsh(userEvent.setup());
      await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(SECOND_ID, { removeKey: true }));
    });

    it('keeps the key when unchecked', async () => {
      mockDelete.mockResolvedValue({ key: null });
      await confirmSsh(userEvent.setup(), true);
      await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(SECOND_ID, { removeKey: false }));
    });

    const KEY = { used_by: null, fingerprint: 'SHA256:abc/def+', comment: 'langalpha' };
    const BY_HAND =
      ' To remove it yourself, delete the line ending in langalpha from ~/.ssh/authorized_keys. Its fingerprint is SHA256:abc/def+.';
    it.each([
      ['no outcome', { key: null }, undefined, false],
      ['removed', { key: { ...KEY, outcome: 'removed' } }, 'We also removed our key from ~/.ssh/authorized_keys.', false],
      [
        'kept',
        { key: { ...KEY, outcome: 'kept', used_by: 'Other box' } },
        'Our key stays in ~/.ssh/authorized_keys, since Other box still logs in with it.',
        false,
      ],
      [
        'skipped',
        { key: { ...KEY, outcome: 'skipped' } },
        'We could not safely remove our key, so it still lets us log in there.' + BY_HAND,
        true,
      ],
      [
        'unreached',
        { key: { ...KEY, outcome: 'unreached' } },
        'We could not reach the machine, so our key is still there.' + BY_HAND,
        true,
      ],
      [
        'unreached with nothing to name',
        { key: { outcome: 'unreached', used_by: null, fingerprint: null, comment: null } },
        'We could not reach the machine, so our key is still there.',
        true,
      ],
    ])('tells the outcome: %s', async (_case, res, description, stays) => {
      mockDelete.mockResolvedValue(res);
      await confirmSsh(userEvent.setup());
      await waitFor(() => expect(mockToast).toHaveBeenCalled());
      const arg = mockToast.mock.calls[0][0];
      expect(arg.title).toBe('Second machine removed');
      expect(arg.description).toBe(description);
      expect(arg.variant).toBeUndefined();
      // A left-behind key is the user's to remove: the toast waits to be closed.
      expect(arg.duration).toBe(stays ? Infinity : undefined);
    });
  });

  it('holds both buttons and says so while the remove is in flight', async () => {
    const user = userEvent.setup();
    let finish: () => void = () => {};
    mockDelete.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    open();
    await user.click(within(await secondRow()).getByRole('button', { name: 'Remove' }));
    const confirm = await screen.findByRole('dialog', { name: 'Remove Second machine?' });
    await user.click(within(confirm).getByRole('button', { name: 'Remove computer' }));

    expect(await within(confirm).findByRole('button', { name: 'Removing...' })).toBeDisabled();
    expect(within(confirm).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    await act(async () => finish());
  });

  it.each([
    ['busy', 'This computer is busy. Try again in a moment.'],
    ['has_workspaces', 'Delete the workspaces on this computer first.'],
    ['primary', "Your primary computer can't be removed."],
  ])('keeps the confirm open and words a %s refusal itself', async (code, copy) => {
    const user = userEvent.setup();
    mockDelete.mockRejectedValue(conflict(code, 'Computer cannot be removed right now'));
    open();
    await user.click(within(await secondRow()).getByRole('button', { name: 'Remove' }));
    const confirm = await screen.findByRole('dialog', { name: 'Remove Second machine?' });
    await user.click(within(confirm).getByRole('button', { name: 'Remove computer' }));

    const alert = await within(confirm).findByRole('alert');
    expect(alert).toHaveTextContent(copy);
    expect(alert).not.toHaveTextContent('Computer cannot be removed right now');
    expect(within(confirm).getByRole('button', { name: 'Remove computer' })).toBeEnabled();
    expect(screen.getAllByTestId('computer-row')).toHaveLength(2);
    expect(mockToast).not.toHaveBeenCalled();
  });

  it('falls back to its own sentence for a refusal with nothing to read', async () => {
    const user = userEvent.setup();
    mockDelete.mockRejectedValue(Object.assign(new Error('Network Error'), {}));
    open();
    await user.click(within(await secondRow()).getByRole('button', { name: 'Remove' }));
    const confirm = await screen.findByRole('dialog', { name: 'Remove Second machine?' });
    await user.click(within(confirm).getByRole('button', { name: 'Remove computer' }));

    expect(await within(confirm).findByRole('alert')).toHaveTextContent("Couldn't remove this computer. Try again.");
  });
});

describe('ComputersDialog: one panel at a time', () => {
  it('steps back while New computer is open, with one backdrop dimming the page', async () => {
    const user = userEvent.setup();
    const form = await openCreateForm(user);

    expect(computersDialog()).toHaveAttribute('inert');
    // Out of the tree too: covered, it is not a dialog a screen reader can be in.
    expect(screen.queryByRole('dialog', { name: 'Computers' })).not.toBeInTheDocument();
    expect(backdropOf(form).style.backgroundColor).toBe('transparent');
    expect(backdropOf(computersDialog()).style.backgroundColor).toBe('var(--color-bg-overlay-strong)');
  });

  it('closes only the panel on top on Escape, and comes back with focus in it', async () => {
    const user = userEvent.setup();
    await openCreateForm(user);

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New computer' })).not.toBeInTheDocument());
    const dialog = screen.getByRole('dialog', { name: 'Computers' });
    expect(dialog).not.toHaveAttribute('inert');
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));
  });

  it('steps back while the remove confirm is open', async () => {
    const user = userEvent.setup();
    open();
    await user.click(within(await secondRow()).getByRole('button', { name: 'Remove' }));
    const confirm = await screen.findByRole('dialog', { name: 'Remove Second machine?' });

    expect(computersDialog()).toHaveAttribute('inert');
    expect(backdropOf(confirm).style.backgroundColor).toBe('transparent');

    await user.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Remove Second machine?' })).not.toBeInTheDocument());
    expect(computersDialog()).not.toHaveAttribute('inert');
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('shows always-on only on a machine that has it, and only to turn it off', async () => {
    const user = userEvent.setup();
    const [first, second] = LIVE_LIST.computers;
    mockGetComputers.mockResolvedValue({ ...LIVE_LIST, computers: [{ ...first, is_always_on: true }, second] });
    mockSetAlwaysOn.mockResolvedValue({ ...first, is_always_on: false });
    open();

    const [withIt, without] = await screen.findAllByTestId('computer-row');
    expect(within(without).queryByRole('switch', { name: 'Always-on' })).not.toBeInTheDocument();
    const toggle = within(withIt).getByRole('switch', { name: 'Always-on' });
    expect(toggle).toBeChecked();

    await user.click(toggle);
    await waitFor(() => expect(mockSetAlwaysOn).toHaveBeenCalledWith(first.computer_id, false));
    expect(screen.queryByRole('dialog', { name: /always-on/i })).not.toBeInTheDocument();
  });
});

describe('ComputersDialog: file backup', () => {
  const SSH = { host: 'box.example.com', port: 22, username: 'dev', fingerprint: 'SHA256:x', tunnel: false, tunnel_connected: null };

  it('offers the switch only on a machine the user brought, and asks before turning it off', async () => {
    const user = userEvent.setup();
    // What the server holds, so the re-read after each change agrees with it.
    let held = true;
    const brought = { kind: 'ssh', status: 'running', provider_ref: 'dev@box', ssh: SSH };
    mockGetComputers.mockImplementation(async () => withSecond({ ...brought, files_backup: held }));
    mockSetFilesBackup.mockImplementation(async (_computerId: string, enabled: boolean) => {
      held = enabled;
      return withSecond({ ...brought, files_backup: held }).computers[1];
    });
    open();

    const [hosted, row] = await screen.findAllByTestId('computer-row');
    expect(within(hosted).queryByRole('switch', { name: 'Back up files' })).not.toBeInTheDocument();
    const toggle = within(row).getByRole('switch', { name: 'Back up files' });
    expect(toggle).toBeChecked();

    await user.click(toggle);
    const confirm = await screen.findByRole('dialog', { name: 'Stop backing up files?' });
    expect(within(confirm).getByText(/each workspace on Second machine is kept but stops updating/)).toBeInTheDocument();
    expect(mockSetFilesBackup).not.toHaveBeenCalled();

    await user.click(within(confirm).getByRole('button', { name: 'Stop backing up' }));
    await waitFor(() => expect(mockSetFilesBackup).toHaveBeenCalledWith(SECOND_ID, false));
    await waitFor(() => expect(within(row).getByRole('switch', { name: 'Back up files' })).not.toBeChecked());

    // Back on needs no confirmation.
    await user.click(within(row).getByRole('switch', { name: 'Back up files' }));
    await waitFor(() => expect(mockSetFilesBackup).toHaveBeenCalledWith(SECOND_ID, true));
    expect(screen.queryByRole('dialog', { name: 'Stop backing up files?' })).not.toBeInTheDocument();
  });

  it('puts the switch back and says why when the server refuses', async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue(withSecond({ kind: 'ssh', status: 'running', provider_ref: 'dev@box', ssh: SSH, files_backup: false }));
    const refusal = Object.assign(new Error('Request failed with status code 409'), {
      status: 409,
      response: { status: 409, data: { detail: 'This computer is being changed. Try again shortly.' }, headers: {} },
    });
    mockSetFilesBackup.mockRejectedValue(refusal);
    open();

    const brought = await secondRow();
    const toggle = within(brought).getByRole('switch', { name: 'Back up files' });
    expect(toggle).not.toBeChecked();

    await user.click(toggle);
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith({
      variant: 'destructive',
      title: "Couldn't change file backup",
      description: 'This computer is being changed. Try again shortly.',
    }));
    expect(within(brought).getByRole('switch', { name: 'Back up files' })).not.toBeChecked();
  });
});
