/**
 * Creating a workspace is instant now: the backend inserts a row on a machine
 * the user already has and returns 201, so there is no provisioning to watch.
 * The 201 body below is the live one from `POST /api/v1/workspaces` on :8060,
 * which came back in 4.5ms already `running` because its computer was up.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';

import { renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';

vi.mock('../../hooks/computerPanelStore', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/computerPanelStore')>()),
  openComputersPanel: vi.fn(),
}));

vi.mock('../../utils/api', () => ({
  getComputers: vi.fn(),
  getComputerProviders: vi.fn(),
  uploadWorkspaceFile: vi.fn(),
  startWorkspace: vi.fn(),
  formatApiErrorDetail: (err: unknown) =>
    (err as { message?: string })?.message ?? 'Request failed',
}));

// DALTON-HOOK(workspace-dedup): mock toast and useDaltonCoverageUnits gate for coverage registration UX
vi.mock('@/components/ui/use-toast', () => ({
  toast: vi.fn(),
}));
vi.mock('@/lib/dalton/coverage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/dalton/coverage')>()),
  useDaltonCoverageUnits: vi.fn(),
}));

import { toast } from '@/components/ui/use-toast';
import { useDaltonCoverageUnits } from '@/lib/dalton/coverage';
import { getComputerProviders, getComputers, startWorkspace, uploadWorkspaceFile } from '../../utils/api';
import { openComputersPanel } from '../../hooks/computerPanelStore';
import CreateWorkspaceModal from '../CreateWorkspaceModal';

const mockUpload = uploadWorkspaceFile as Mock;
const mockGetComputers = getComputers as Mock;
const mockProviders = getComputerProviders as Mock;

// POST /api/v1/workspaces, captured verbatim.
const LIVE_CREATED = {
  workspace_id: 'da7f8dc7-509a-4903-be94-13dc509944a1',
  user_id: 'wp13-final-1789525377',
  name: 'WP16 UI probe',
  description: '',
  sandbox_id: '91885905-79ed-48dc-81ac-0a3d2d608458',
  computer_id: '5319ad0e-7835-4ca6-9541-b7c2961a7bf6',
  dir_name: 'wp16-ui-probe-8707',
  status: 'running',
  config: {},
  is_pinned: false,
  sort_order: 0,
  resource_tier: 'standard',
  is_always_on: false,
};

const PRIMARY_ID = LIVE_CREATED.computer_id;
const SECOND_ID = '3b788b47-352e-4cfb-94f0-920b599ea227';

function machine(computer_id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    computer_id,
    user_id: LIVE_CREATED.user_id,
    kind: 'daytona',
    name,
    status: 'running',
    resource_tier: 'standard',
    is_always_on: false,
    is_primary: false,
    workspace_count: 1,
    root_dir: '/home/workspace',
    ...extra,
  };
}

const PRIMARY = machine(PRIMARY_ID, 'Alpha Research', { is_primary: true });
const SECOND = machine(SECOND_ID, 'Second machine', { status: 'stopped' });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(startWorkspace).mockResolvedValue({ workspace_id: LIVE_CREATED.workspace_id, status: 'running' });
  mockGetComputers.mockResolvedValue({ computers: [PRIMARY], total: 1 });
  mockProviders.mockResolvedValue({ providers: [{ kind: 'daytona', is_default: true, allowed: null }] });
  // DALTON-HOOK(workspace-dedup): default to coverage units enabled for internal team member
  vi.mocked(useDaltonCoverageUnits).mockReturnValue({
    data: { enabled: true, units: [], unclassified: [] },
    isError: false,
  } as unknown as ReturnType<typeof useDaltonCoverageUnits>);
});

/** The route the page is on, for a way out that leaves it. */
function Path() {
  return <span data-testid="path">{useLocation().pathname}</span>;
}

function setup(onCreate: Mock, onComplete = vi.fn(), onClose = vi.fn()) {
  const { queryClient } = renderWithProviders(
    <>
      <Path />
      <CreateWorkspaceModal
        isOpen
        onClose={onClose}
        onCreate={onCreate}
        onComplete={onComplete}
      />
    </>,
  );
  return { onComplete, onClose, queryClient };
}

// A computer whose first sandbox is still being built.
const PREPARING_PRIMARY = { ...PRIMARY, status: 'starting', provider_ref: null };
const NOTICE = "Alpha Research is being prepared. You can create a workspace on it once it's ready.";

describe('CreateWorkspaceModal', () => {
  it('opens the workspace straight away, with no provisioning screen', async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
    const { onComplete, onClose } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'WP16 UI probe');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(LIVE_CREATED));
    expect(onClose).toHaveBeenCalled();
    expect(startWorkspace).not.toHaveBeenCalled();
    // No progress phase at all: nothing was provisioned and nothing uploaded.
    expect(screen.queryByText(/uploading files/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^ready$/i)).not.toBeInTheDocument();
  });

  it.each([
    ['one computer', [PRIMARY]],
    ['one live computer beside a deleted one', [PRIMARY, { ...SECOND, status: 'deleted' }]],
  ])('offers no computer choice with %s, and names none', async (_case, computers) => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue({ computers, total: computers.length });
    const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
    setup(onCreate);

    await waitFor(() => expect(mockGetComputers).toHaveBeenCalled());
    await user.type(screen.getByPlaceholderText(/name/i), 'Only machine');
    expect(screen.queryByRole('combobox', { name: 'Computer' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith({ name: 'Only machine', description: '' }));
  });

  it('lets the user put the workspace on another computer, starting from the primary', async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue({ computers: [SECOND, PRIMARY], total: 2 });
    const onCreate = vi.fn().mockResolvedValue({ ...LIVE_CREATED, computer_id: SECOND_ID });
    setup(onCreate);

    const choice = await screen.findByRole('combobox', { name: 'Computer' });
    expect(choice).toHaveValue(PRIMARY_ID);
    expect(screen.getByRole('option', { name: 'Alpha Research (Primary)' })).toBeInTheDocument();

    await user.selectOptions(choice, SECOND_ID);
    await user.type(screen.getByPlaceholderText(/name/i), 'On the second');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith({ name: 'On the second', description: '', computerId: SECOND_ID }),
    );
  });

  it('names what each computer runs on once the providers differ', async () => {
    mockGetComputers.mockResolvedValue({
      computers: [PRIMARY, machine(SECOND_ID, 'Second machine', { kind: 'e2b' })],
      total: 2,
    });
    mockProviders.mockResolvedValue({
      providers: [
        { kind: 'daytona', is_default: true, allowed: null },
        { kind: 'e2b', is_default: false, allowed: true },
      ],
    });
    setup(vi.fn());

    expect(await screen.findByRole('option', { name: 'Alpha Research · Daytona (Primary)' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Second machine · E2B' })).toBeInTheDocument();
  });

  it('has no resource-tier picker: the tier belongs to the machine', async () => {
    const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
    setup(onCreate);
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /performance/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /max/i })).not.toBeInTheDocument();
  });

  it('keeps the user on the form and relays the quota service\'s sentence on a 429', async () => {
    const user = userEvent.setup();
    const platformMessage =
      'You have reached your workspace limit. Upgrade your plan to add more.';
    const onCreate = vi.fn().mockRejectedValue(
      Object.assign(new Error('Request failed with status code 429'), {
        status: 429,
        rateLimitInfo: { message: platformMessage, type: 'workspace_limit' },
      }),
    );
    const { onComplete } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'Denied');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    expect(await screen.findByText(platformMessage)).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
    // Still on the form, with the typed name intact.
    expect(screen.getByPlaceholderText(/name/i)).toHaveValue('Denied');
  });

  it('keeps the user on the form and names the workspace already holding the name', async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockRejectedValue(
      Object.assign(new Error('Request failed with status code 409'), {
        response: {
          status: 409,
          data: {
            detail: {
              code: 'workspace_name_taken',
              message: 'A workspace named "Research" already exists.',
              name: 'Research',
              workspace_id: LIVE_CREATED.workspace_id,
            },
          },
        },
      }),
    );
    const { onComplete } = setup(onCreate);

    const field = screen.getByPlaceholderText(/name/i);
    await user.type(field, 'research');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    expect(
      await screen.findByText('A workspace named "Research" already exists. Choose another name.'),
    ).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
    expect(field).toHaveValue('research');
  });

  it('caps the name at 80 characters as the server counts them, so an emoji is one', async () => {
    const user = userEvent.setup();
    setup(vi.fn());

    const field = screen.getByPlaceholderText(/name/i) as HTMLInputElement;
    await user.click(field);
    await user.paste('\u{1F4C8}'.repeat(90));

    expect(field.value).toBe('\u{1F4C8}'.repeat(80));
  });

  it('shows the upload run only when files are queued, and never an "initializing" step', async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
    mockUpload.mockResolvedValue({});
    const { onComplete } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'With files');
    const file = new File(['hello'], 'notes.txt', { type: 'text/plain' });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, file);

    await user.click(screen.getByRole('button', { name: /^create$/i }));

    // The upload track appears; the old "Initializing workspace" step does not.
    expect(await screen.findByText(/uploading files/i)).toBeInTheDocument();
    expect(screen.queryByText(/initializing workspace/i)).not.toBeInTheDocument();
    await waitFor(() =>
      expect(mockUpload).toHaveBeenCalledWith(
        LIVE_CREATED.workspace_id,
        file,
        null,
        expect.any(Function),
      ),
    );
    // The workspace is only handed over when the user asks for it.
    expect(onComplete).not.toHaveBeenCalled();
  });
  it('waits for start before uploading to a stopped computer', async () => {
    const user = userEvent.setup();
    let ready!: (value: { workspace_id: string; status: string }) => void;
    vi.mocked(startWorkspace).mockReturnValueOnce(new Promise((resolve) => { ready = resolve; }));
    mockUpload.mockResolvedValue({});
    setup(vi.fn().mockResolvedValue({ ...LIVE_CREATED, status: 'stopped' }));
    await user.type(screen.getByPlaceholderText(/name/i), 'Stopped upload');
    await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement,
      new File(['hello'], 'notes.txt', { type: 'text/plain' }));
    await user.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() => expect(startWorkspace).toHaveBeenCalledWith(LIVE_CREATED.workspace_id, { reason: 'create' }));
    expect(mockUpload).not.toHaveBeenCalled();
    ready({ workspace_id: LIVE_CREATED.workspace_id, status: 'running' });
    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));
  });

  it('retries a failed start without recreating the workspace', async () => {
    const user = userEvent.setup();
    vi.mocked(startWorkspace).mockRejectedValueOnce(new Error('Start failed'));
    mockUpload.mockResolvedValue({});
    const create = vi.fn().mockResolvedValue({ ...LIVE_CREATED, status: 'stopped' });
    setup(create);
    await user.type(screen.getByPlaceholderText(/name/i), 'Retry upload');
    await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement,
      new File(['hello'], 'notes.txt', { type: 'text/plain' }));
    await user.click(screen.getByRole('button', { name: /^create$/i }));
    expect(await screen.findByText('Start failed')).toBeInTheDocument();
    expect(mockUpload).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));
    expect(startWorkspace).toHaveBeenCalledTimes(2);
    expect(create).toHaveBeenCalledTimes(1);
  });

});


it('blocks backdrop, close and cancel while creation is pending', async () => {
  const user = userEvent.setup();
  let finish!: (value: typeof LIVE_CREATED) => void;
  const pending = new Promise<typeof LIVE_CREATED>((resolve) => { finish = resolve; });
  const { onClose, onComplete } = setup(vi.fn(() => pending));
  await user.type(screen.getByPlaceholderText(/name/i), 'Pending');
  await user.click(screen.getByRole('button', { name: /^create$/i }));
  expect(screen.getByRole('button', { name: /cancel/i })).toBeDisabled();
  const close = document.querySelector<HTMLButtonElement>('.cwm-close-btn')!;
  expect(close).toBeDisabled();
  await user.click(document.querySelector<HTMLElement>('.cwm-overlay')!);
  await user.click(close);
  expect(onClose).not.toHaveBeenCalled();
  finish(LIVE_CREATED);
  await waitFor(() => expect(onComplete).toHaveBeenCalledWith(LIVE_CREATED));
  expect(onClose).toHaveBeenCalledOnce();
});

describe('CreateWorkspaceModal, a computer still being prepared', () => {
  it('holds the form while the only computer is prepared, and lets go once it is ready', async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue({ computers: [PREPARING_PRIMARY], total: 1 });
    const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
    const { queryClient } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'Too early');
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    const create = screen.getByRole('button', { name: /^create$/i });
    expect(create).toBeDisabled();

    // What the status fan-out writes when the stream says running.
    act(() => {
      queryClient.setQueryData(queryKeys.computers.lists(), {
        computers: [{ ...PREPARING_PRIMARY, status: 'running' }],
        total: 1,
      });
    });
    await waitFor(() => expect(screen.queryByText(NOTICE)).not.toBeInTheDocument());
    expect(create).toBeEnabled();
    await user.click(create);
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith({ name: 'Too early', description: '' }));
  });

  it('offers a computer being prepared as a choice it cannot take', async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue({ computers: [PREPARING_PRIMARY, SECOND], total: 2 });
    const onCreate = vi.fn().mockResolvedValue({ ...LIVE_CREATED, computer_id: SECOND_ID });
    setup(onCreate);

    const choice = await screen.findByRole('combobox', { name: 'Computer' });
    expect(screen.getByRole('option', { name: 'Alpha Research (Primary, Preparing)' })).toBeDisabled();
    // The primary is still where it starts, so the hold shows until another is picked.
    expect(screen.getByText(NOTICE)).toBeInTheDocument();

    await user.selectOptions(choice, SECOND_ID);
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/name/i), 'On the second');
    await user.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith({ name: 'On the second', description: '', computerId: SECOND_ID }),
    );
  });

  it('suffixes a non-primary computer being prepared', async () => {
    mockGetComputers.mockResolvedValue({
      computers: [PRIMARY, { ...SECOND, status: 'creating', provider_ref: null }],
      total: 2,
    });
    setup(vi.fn());
    await screen.findByRole('combobox', { name: 'Computer' });
    expect(screen.getByRole('option', { name: 'Second machine (Preparing)' })).toBeDisabled();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('holds nothing for a user with no computer yet, whose first workspace makes one', async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue({ computers: [], total: 0 });
    const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
    setup(onCreate);

    await waitFor(() => expect(mockGetComputers).toHaveBeenCalled());
    await user.type(screen.getByPlaceholderText(/name/i), 'First');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() => expect(onCreate).toHaveBeenCalled());
  });

  it("words the server's refusal for a computer the list had not yet seen being prepared", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockRejectedValue(
      Object.assign(new Error('Request failed with status code 409'), {
        response: {
          status: 409,
          data: { detail: { code: 'computer_preparing', message: 'Computer is still being prepared' } },
        },
      }),
    );
    setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'Raced');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    expect(
      await screen.findByText("This computer is still being prepared. Try again once it's ready."),
    ).toBeInTheDocument();
    expect(screen.queryByText('Computer is still being prepared')).not.toBeInTheDocument();
  });

  it('offers to add a computer when the user has none and none is built for them', async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue({ computers: [], total: 0 });
    const onCreate = vi.fn().mockRejectedValue(
      Object.assign(new Error('Request failed with status code 409'), {
        response: {
          status: 409,
          data: { detail: { code: 'needs_computer', message: 'Add a computer first.' } },
        },
      }),
    );
    const { onClose } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'First');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    const card = await screen.findByRole('alert');
    expect(card).toHaveTextContent("Workspaces live on a computer, and you don't have one yet.");
    expect(screen.queryByText('Add a computer first.')).not.toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: 'Add a computer' }));
    expect(onClose).toHaveBeenCalled();
    expect(openComputersPanel).toHaveBeenCalledWith({ view: 'create' });
    // No page here shows the Computers dialog, so it opens on chat's now
    // rather than unasked on whichever page shows one next.
    expect(screen.getByTestId('path')).toHaveTextContent('/chat');
  });

  it("offers the computer whose backup is off when that is why Home can't live anywhere", async () => {
    const user = userEvent.setup();
    mockGetComputers.mockResolvedValue({ computers: [], total: 0 });
    const onCreate = vi.fn().mockRejectedValue(
      Object.assign(new Error('Request failed with status code 409'), {
        response: {
          status: 409,
          data: {
            detail: { code: 'needs_backup', message: 'Turn on backup.', computer_id: SECOND_ID, computer_name: 'Office Mac' },
          },
        },
      }),
    );
    const { onClose } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'First');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    const card = await screen.findByRole('alert');
    expect(card).toHaveTextContent(
      "Backup is off on Office Mac, so Home can't live there. Turn backup on, and Office Mac can be Home's computer.",
    );
    await user.click(within(card).getByRole('button', { name: 'Open Office Mac' }));
    expect(onClose).toHaveBeenCalled();
    expect(openComputersPanel).toHaveBeenCalledWith({ view: 'details', computerId: SECOND_ID });
  });
});

// DALTON-HOOK(workspace-dedup): duplicate company workspace refusal UX (open readable, ask owner, i18n, name-change reset, create anyway)
describe('CreateWorkspaceModal, duplicate company workspace', () => {
  it('offers "Open this workspace" for readable matches using i18n copy and navigates there on click', async () => {
    const user = userEvent.setup();
    const existingId = '99999999-9999-4999-8999-999999999999';
    const onCreate = vi.fn().mockRejectedValue(
      Object.assign(new Error('Request failed with status code 409'), {
        response: {
          status: 409,
          data: {
            detail: {
              code: 'company_workspace_exists',
              message: '服务端中文提示不应直接展示',
              matches: [
                {
                  workspace_id: existingId,
                  workspace_name: 'ANT',
                  owner_name: 'Richard',
                  lead_analyst_name: 'Richard',
                  ticker: 'ANT',
                  can_read: true,
                },
              ],
            },
          },
        },
      }),
    );
    const { onClose, onComplete } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/name/i), 'Anthropic');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    const card = await screen.findByRole('alert');
    expect(card).toHaveTextContent('Anthropic already has a workspace "ANT"');
    expect(card).toHaveTextContent('Richard');
    expect(card).not.toHaveTextContent('服务端中文提示不应直接展示');
    expect(onComplete).not.toHaveBeenCalled();

    await user.click(within(card).getByRole('button', { name: 'Open this workspace' }));
    expect(onClose).toHaveBeenCalled();
    expect(screen.getByTestId('path')).toHaveTextContent(`/chat/${existingId}`);
  });

  it('shows share notice for unreadable matches, clears on edit, and resubmits the 409 name on "Create anyway"', async () => {
    const user = userEvent.setup();
    const onCreate = vi
      .fn()
      .mockRejectedValueOnce(
        Object.assign(new Error('Request failed with status code 409'), {
          response: {
            status: 409,
            data: {
              detail: {
                code: 'company_workspace_exists',
                message: '服务端中文提示不应直接展示',
                matches: [
                  {
                    workspace_name: 'Anthropic',
                    owner_name: 'CY',
                    can_read: false,
                  },
                ],
              },
            },
          },
        }),
      )
      .mockResolvedValueOnce(LIVE_CREATED);
    const { onComplete } = setup(onCreate);

    const input = screen.getByPlaceholderText(/name/i);
    await user.type(input, 'Anthropic');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    const card = await screen.findByRole('alert');
    expect(card).toHaveTextContent('Anthropic already has a workspace "Anthropic" (owner: CY)');
    expect(card).toHaveTextContent('Ask the owner to share access');
    expect(card).not.toHaveTextContent('服务端中文提示不应直接展示');
    expect(within(card).queryByRole('button', { name: 'Open this workspace' })).not.toBeInTheDocument();

    await user.click(within(card).getByRole('button', { name: 'Create anyway' }));
    await waitFor(() =>
      expect(onCreate).toHaveBeenLastCalledWith({
        name: 'Anthropic',
        description: '',
        allowDuplicateCompany: true,
      }),
    );
    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(LIVE_CREATED));
  });

  it('clears the duplicate notice when the user edits the name, and ignores stale in-flight 409s', async () => {
    const user = userEvent.setup();
    let rejectFirst!: (err: unknown) => void;
    const firstPending = new Promise<typeof LIVE_CREATED>((_resolve, reject) => {
      rejectFirst = reject;
    });
    const onCreate = vi
      .fn()
      .mockReturnValueOnce(firstPending)
      .mockRejectedValueOnce(
        Object.assign(new Error('Request failed with status code 409'), {
          response: {
            status: 409,
            data: {
              detail: {
                code: 'company_workspace_exists',
                message: 'dup',
                matches: [{ workspace_name: 'NVDA', owner_name: 'Richard', can_read: false }],
              },
            },
          },
        }),
      );
    setup(onCreate);

    const input = screen.getByPlaceholderText(/name/i);
    await user.type(input, 'Anthropic');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    // Edit the name while the "Anthropic" request is still in flight.
    await user.clear(input);
    await user.type(input, 'NVDA');

    await act(async () => {
      rejectFirst(
        Object.assign(new Error('Request failed with status code 409'), {
          response: {
            status: 409,
            data: {
              detail: {
                code: 'company_workspace_exists',
                message: 'stale',
                matches: [{ workspace_name: 'ANT', owner_name: 'Richard', can_read: false }],
              },
            },
          },
        }),
      );
    });

    // Stale 409 for "Anthropic" must not render while input holds "NVDA".
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    // Now submit "NVDA" and verify the 409 notice renders, then clears on edit.
    await user.click(screen.getByRole('button', { name: /^create$/i }));
    const card = await screen.findByRole('alert');
    expect(card).toHaveTextContent('NVDA already has a workspace "NVDA"');

    await user.type(input, ' Corp');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('submits coverageKind/ticker/displayName when Company is selected and defaults to Other', async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
    const { onComplete } = setup(onCreate);

    // Default selection is "Other" -> no ticker/displayName fields rendered.
    const otherBtn = screen.getByRole('button', { name: 'Other' });
    expect(otherBtn).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByLabelText(/ticker/i)).not.toBeInTheDocument();

    // Select Company, fill ticker and company name, and submit.
    await user.click(screen.getByRole('button', { name: 'Company' }));
    await user.type(screen.getByPlaceholderText(/enter workspace name/i), 'Nvidia');
    await user.type(screen.getByLabelText(/ticker/i), 'NVDA');
    await user.type(screen.getByLabelText(/company name/i), 'NVIDIA Corporation');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith({
        name: 'Nvidia',
        description: '',
        coverageKind: 'company',
        ticker: 'NVDA',
        displayName: 'NVIDIA Corporation',
      }),
    );
    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(LIVE_CREATED));
  });

  it('prefills Type, Ticker, and Company Name from 409 suggestion and lets user edit before Create anyway', async () => {
    const user = userEvent.setup();
    const onCreate = vi
      .fn()
      .mockRejectedValueOnce(
        Object.assign(new Error('Request failed with status code 409'), {
          response: {
            status: 409,
            data: {
              detail: {
                code: 'company_workspace_exists',
                message: 'dup',
                matches: [{ workspace_name: 'NVDA', owner_name: 'Richard', can_read: true }],
                suggestion: {
                  coverage_kind: 'company',
                  ticker: 'NVDA',
                  display_name: 'NVIDIA Corporation',
                },
              },
            },
          },
        }),
      )
      .mockResolvedValueOnce({
        ...LIVE_CREATED,
        coverage_unit_warning: '股票代码 NVDA.US 已被其他覆盖单元登记，工作区已创建但未自动登记覆盖单元。',
      });
    const { onComplete } = setup(onCreate);

    await user.type(screen.getByPlaceholderText(/enter workspace name/i), 'Nvidia');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    const card = await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: 'Company' })).toHaveAttribute('aria-pressed', 'true');
    const tickerInput = screen.getByLabelText(/ticker/i) as HTMLInputElement;
    const companyInput = screen.getByLabelText(/company name/i) as HTMLInputElement;
    expect(tickerInput.value).toBe('NVDA');
    expect(companyInput.value).toBe('NVIDIA Corporation');

    // User can edit the prefilled ticker before clicking Create anyway.
    await user.clear(tickerInput);
    await user.type(tickerInput, 'NVDA.US');
    await user.click(within(card).getByRole('button', { name: 'Create anyway' }));

    await waitFor(() =>
      expect(onCreate).toHaveBeenLastCalledWith({
        name: 'Nvidia',
        description: '',
        allowDuplicateCompany: true,
        coverageKind: 'company',
        ticker: 'NVDA.US',
        displayName: 'NVIDIA Corporation',
      }),
    );
    await waitFor(() => expect(onComplete).toHaveBeenCalled());
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Workspace created, coverage unit not registered',
        description: '股票代码 NVDA.US 已被其他覆盖单元登记，工作区已创建但未自动登记覆盖单元。',
      }),
    );
  });

  it('supports Sector type and Chinese i18n labels', async () => {
    const user = userEvent.setup();
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    try {
      const onCreate = vi.fn().mockResolvedValue(LIVE_CREATED);
      setup(onCreate);

      expect(screen.getByRole('button', { name: '其他' })).toHaveAttribute('aria-pressed', 'true');
      await user.click(screen.getByRole('button', { name: '行业' }));
      expect(screen.queryByLabelText(/股票代码/)).not.toBeInTheDocument();
      await user.type(screen.getByPlaceholderText('输入工作区名称'), '半导体');
      await user.type(screen.getByLabelText(/行业名/), '半导体行业');
      await user.click(screen.getByRole('button', { name: '创建' }));

      await waitFor(() =>
        expect(onCreate).toHaveBeenCalledWith({
          name: '半导体',
          description: '',
          coverageKind: 'sector',
          displayName: '半导体行业',
        }),
      );
    } finally {
      await i18n.changeLanguage('en-US');
    }
  });

  // DALTON-HOOK(workspace-dedup): 409 coverage_suggestion confirmation when Type is Other (Register & Create vs Create without registering)
  it('prompts on 409 coverage_suggestion with prefilled fields and supports Register & Create or Create without registering', async () => {
    const user = userEvent.setup();
    const suggestionErr = Object.assign(new Error('Request failed with status code 409'), {
      response: {
        status: 409,
        data: {
          detail: {
            code: 'coverage_suggestion',
            message: '「Nvidia」识别为公司（代码 NVDA）',
            suggestion: {
              coverage_kind: 'company',
              ticker: 'NVDA',
              display_name: 'NVIDIA Corporation',
            },
          },
        },
      },
    });

    // 1. Register & Create path (allows editing prefilled fields before confirming)
    const onCreateRegister = vi
      .fn()
      .mockRejectedValueOnce(suggestionErr)
      .mockResolvedValueOnce(LIVE_CREATED);
    const first = setup(onCreateRegister);

    await user.type(screen.getByPlaceholderText(/enter workspace name/i), 'Nvidia');
    await user.click(screen.getByRole('button', { name: /^create$/i }));

    const card = await screen.findByRole('alert');
    expect(card).toHaveTextContent('"Nvidia" looks like a company (NVDA).');
    expect(screen.getByRole('button', { name: 'Company' })).toHaveAttribute('aria-pressed', 'true');
    const tickerInput = screen.getByLabelText(/ticker/i) as HTMLInputElement;
    expect(tickerInput.value).toBe('NVDA');
    await user.clear(tickerInput);
    await user.type(tickerInput, 'NVDA.US');
    await user.click(within(card).getByRole('button', { name: 'Register & Create' }));

    await waitFor(() =>
      expect(onCreateRegister).toHaveBeenLastCalledWith({
        name: 'Nvidia',
        description: '',
        coverageKind: 'company',
        ticker: 'NVDA.US',
        displayName: 'NVIDIA Corporation',
      }),
    );
    await waitFor(() => expect(first.onComplete).toHaveBeenCalledWith(LIVE_CREATED));

    // 2. Create without registering path (submits skipCoverageSuggestion: true and omits coverage fields)
    const { unmount } = renderWithProviders(<></>);
    unmount();
    vi.clearAllMocks();
    mockGetComputers.mockResolvedValue({ computers: [PRIMARY], total: 1 });
    const onCreateSkip = vi
      .fn()
      .mockRejectedValueOnce(suggestionErr)
      .mockResolvedValueOnce(LIVE_CREATED);
    const second = setup(onCreateSkip);

    const nameInputs = screen.getAllByPlaceholderText(/enter workspace name/i);
    await user.type(nameInputs[nameInputs.length - 1], 'Nvidia');
    const createBtns = screen.getAllByRole('button', { name: /^create$/i });
    await user.click(createBtns[createBtns.length - 1]);

    const alerts = await screen.findAllByRole('alert');
    const latestCard = alerts[alerts.length - 1];
    await user.click(within(latestCard).getByRole('button', { name: 'Create without registering' }));

    await waitFor(() =>
      expect(onCreateSkip).toHaveBeenLastCalledWith({
        name: 'Nvidia',
        description: '',
        skipCoverageSuggestion: true,
      }),
    );
    await waitFor(() => expect(second.onComplete).toHaveBeenCalledWith(LIVE_CREATED));
  });

  // DALTON-HOOK(workspace-dedup): length hints on ticker/display_name without blocking submission, and bilingual structured warning rendering
  it('shows length hints without blocking long ticker submit, and renders structured coverage warnings in English and Chinese', async () => {
    const user = userEvent.setup();
    const longTicker = 'A'.repeat(33);
    const onCreateEn = vi.fn().mockResolvedValue({
      ...LIVE_CREATED,
      coverage_unit_warning: '服务端中文文案不应在英文界面显示',
      coverage_unit_warning_detail: {
        code: 'ticker_too_long',
        params: { max_length: 32 },
      },
    });
    setup(onCreateEn);

    await user.click(screen.getByRole('button', { name: 'Company' }));
    expect(screen.getByText('Max 32 characters')).toBeInTheDocument();
    expect(screen.getByText('Max 120 characters')).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText(/enter workspace name/i), 'LongCo');
    await user.type(screen.getByLabelText(/ticker/i), longTicker);
    expect(screen.getByText(/Max 32 characters \(currently 33\)/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() =>
      expect(onCreateEn).toHaveBeenCalledWith({
        name: 'LongCo',
        description: '',
        coverageKind: 'company',
        ticker: longTicker,
      }),
    );
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Workspace created, coverage unit not registered',
        description:
          'Ticker cannot exceed 32 characters. The workspace was created without auto-registering a coverage unit.',
      }),
    );

    // Chinese locale renders Chinese copy from structured code + params
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    try {
      vi.clearAllMocks();
      mockGetComputers.mockResolvedValue({ computers: [PRIMARY], total: 1 });
      const onCreateZh = vi.fn().mockResolvedValue({
        ...LIVE_CREATED,
        coverage_unit_warning: 'fallback',
        coverage_unit_warning_detail: {
          code: 'ticker_conflict',
          params: { ticker: 'NVDA' },
        },
      });
      setup(onCreateZh);
      const nameInputs = screen.getAllByPlaceholderText('输入工作区名称');
      await user.type(nameInputs[nameInputs.length - 1], 'Nvidia');
      const createBtns = screen.getAllByRole('button', { name: '创建' });
      await user.click(createBtns[createBtns.length - 1]);

      await waitFor(() =>
        expect(toast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: '工作区已创建，覆盖单元未登记',
            description:
              '股票代码 NVDA 已被其他覆盖单元登记，工作区已创建但未自动登记覆盖单元。',
          }),
        ),
      );
    } finally {
      await i18n.changeLanguage('en-US');
    }
  });

  // DALTON-HOOK(workspace-dedup): hides coverage fields when coverage_units disabled, loading, or caller is external / company tier (isError)
  it('hides coverage unit fields when coverage units are disabled, loading, or caller is external / company tier (isError)', async () => {
    const user = userEvent.setup();
    vi.mocked(useDaltonCoverageUnits).mockReturnValue({
      data: { enabled: false },
      isError: false,
    } as unknown as ReturnType<typeof useDaltonCoverageUnits>);
    const onCreateDisabled = vi.fn().mockResolvedValue(LIVE_CREATED);
    const first = renderWithProviders(
      <CreateWorkspaceModal isOpen onClose={vi.fn()} onCreate={onCreateDisabled} />,
    );

    expect(screen.queryByRole('button', { name: 'Company' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sector' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Other' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/ticker/i)).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/enter workspace name/i), 'My Research');
    await user.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() =>
      expect(onCreateDisabled).toHaveBeenCalledWith({
        name: 'My Research',
        description: '',
      }),
    );
    first.unmount();

    // 403 error (external member or company tier) or 404 (Dalton disabled)
    vi.mocked(useDaltonCoverageUnits).mockReturnValue({
      data: undefined,
      isError: true,
    } as unknown as ReturnType<typeof useDaltonCoverageUnits>);
    const onCreateForbidden = vi.fn().mockResolvedValue(LIVE_CREATED);
    const second = renderWithProviders(
      <CreateWorkspaceModal isOpen onClose={vi.fn()} onCreate={onCreateForbidden} />,
    );
    expect(screen.queryByRole('button', { name: 'Company' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sector' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Other' })).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/enter workspace name/i), 'External Ws');
    await user.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() =>
      expect(onCreateForbidden).toHaveBeenCalledWith({
        name: 'External Ws',
        description: '',
      }),
    );
    second.unmount();

    // Loading state before GET /coverage/units resolves also keeps fields hidden
    vi.mocked(useDaltonCoverageUnits).mockReturnValue({
      data: undefined,
      isError: false,
    } as unknown as ReturnType<typeof useDaltonCoverageUnits>);
    renderWithProviders(
      <CreateWorkspaceModal isOpen onClose={vi.fn()} onCreate={vi.fn()} />,
    );
    expect(screen.queryByRole('button', { name: 'Company' })).not.toBeInTheDocument();
  });
});
