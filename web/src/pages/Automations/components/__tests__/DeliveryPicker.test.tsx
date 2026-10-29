import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { ApiError } from '@/api/client';
import type { ProviderModelsData } from '@/components/model/types';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';
import i18n from '@/i18n';
import { queryKeys } from '@/lib/queryKeys';
import { renderWithProviders } from '@/test/utils';
import type { Automation, DeliveryOptions } from '@/types/automation';
import * as api from '../../utils/api';
import { ALL_WORKSPACES, automationToFormState, type FormPatch, type FormState, INITIAL_FORM } from '../../utils/form';
import AutomationInlineForm, { type FormSubmission } from '../AutomationInlineForm';
import MoreOptions from '../MoreOptions';

// DALTON-HOOK(automation-model): mock useAllModels for per-automation model selector tests
const allModelsMock = vi.hoisted(() => ({
  current: {
    models: {} as Record<string, ProviderModelsData>,
    lockedModels: {} as Record<string, ProviderModelsData>,
    keylessModels: new Set<string>(),
    metadata: {} as Record<string, ModelMetadataEntry>,
    payers: {} as Record<string, 'byok' | 'oauth' | 'credits'>,
    isLoading: false,
    rawApiResponse: {} as unknown,
    isLoaded: true,
  },
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => allModelsMock.current,
}));

// DALTON-HOOK(automation-model): the keys decide which custom models are keyless
const apiKeysMock = vi.hoisted(() => ({ current: { apiKeys: {} as unknown } }));
vi.mock('@/hooks/useApiKeys', () => ({
  useApiKeys: () => apiKeysMock.current,
}));
const providersMock = vi.hoisted(() => ({ current: { statusError: false } }));
vi.mock('@/hooks/useConfiguredProviders', () => ({
  useConfiguredProviders: () => providersMock.current,
}));

vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [{ workspace_id: 'ws-1', name: 'Research' }] } }),
}));

vi.mock('@/hooks/useHomeWorkspace', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useHomeWorkspace')>()),
  useHomeWorkspaceId: () => 'home-1',
}));

vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  getDeliveryOptions: vi.fn(),
  setDeliveryDefault: vi.fn(),
}));

afterEach(() => {
  vi.clearAllMocks();
  // DALTON-HOOK(automation-model): reset model catalog mock between tests
  apiKeysMock.current = { apiKeys: {} };
  providersMock.current = { statusError: false };
  allModelsMock.current = {
    models: {},
    lockedModels: {},
    keylessModels: new Set<string>(),
    metadata: {},
    payers: {},
    isLoading: false,
    rawApiResponse: {} as unknown,
    isLoaded: true,
  };
});

const label = (key: string, opts?: Record<string, unknown>) => i18n.t(key, opts);

const OPTIONS: DeliveryOptions = {
  enabled: true,
  workspace_id: 'ws-1',
  apps: {
    discord: {
      chats: [
        { address: 'discord:G1/C7', name: '#alerts', kind: 'channel' },
        { address: 'discord:@me', name: 'Your Discord DM', kind: 'dm' },
      ],
      default: { address: 'discord:@me', name: 'Your Discord DM', via: 'dm' },
      error: null,
    },
    slack: {
      chats: [
        { address: 'slack:T1', name: 'Your Slack DM (Acme)', kind: 'dm' },
        { address: 'slack:T1/C1', name: '#demo', kind: 'channel' },
        { address: 'slack:T1/C2', name: '#research', kind: 'channel' },
      ],
      default: { address: 'slack:T1/C1', name: '#demo', via: 'workspace' },
      error: null,
    },
  },
};

function refusal(status: number, body: unknown): ApiError {
  const code = status >= 500 ? 'ERR_BAD_RESPONSE' : 'ERR_BAD_REQUEST';
  return new ApiError(`Request failed with status code ${status}`, code, { status, statusText: '', headers: {}, data: body });
}

function serve(options: DeliveryOptions | ApiError = OPTIONS) {
  if (options instanceof ApiError) vi.mocked(api.getDeliveryOptions).mockRejectedValue(options);
  else vi.mocked(api.getDeliveryOptions).mockResolvedValue({ data: options } as never);
}

const IN_WORKSPACE: FormState = { ...INITIAL_FORM, place: 'ws-1' };

/** MoreOptions unfolded over a form it edits, with the picked entries on show. */
function Harness({ initial, open = true }: { initial: FormState; open?: boolean }) {
  const [form, setForm] = useState(initial);
  const [isOpen, setOpen] = useState(open);
  const patch: FormPatch = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  return (
    <>
      <MoreOptions form={form} patch={patch} open={isOpen} onOpenChange={setOpen} />
      <output data-testid="methods">{JSON.stringify(form.delivery_methods)}</output>
    </>
  );
}

function renderPicker(methods: string[] = [], initial: FormState = IN_WORKSPACE, open = true) {
  return renderWithProviders(<Harness initial={{ ...initial, delivery_methods: methods }} open={open} />);
}

const picked = () => JSON.parse(screen.getByTestId('methods').textContent ?? '[]') as string[];
const addButton = () => screen.findByRole('button', { name: new RegExp(label('automation.deliveryAdd')) });
// By class: an open list hides the rest of the page from the role queries.
const chips = () => [...document.querySelectorAll<HTMLElement>('.automation-delivery-chip')];

async function openList() {
  fireEvent.click(await addButton());
  return screen.findByRole('listbox');
}

describe('the delivery picker', () => {
  it('lists each app’s Default, DM and channels under the app, Slack first', async () => {
    serve();
    renderPicker();
    const list = await openList();

    const groups = within(list).getAllByRole('group');
    expect(groups.map((g) => within(g).getAllByRole('option').map((o) => o.textContent))).toEqual([
      [`${label('automation.deliveryDefaultOptionLands', { name: '#demo' })}${label('automation.deliveryViaWorkspace')}`, 'Your Slack DM (Acme)', '#demo', '#research'],
      // The DM goes first, whatever order the app listed it in.
      [`${label('automation.deliveryDefaultOptionLands', { name: 'Your Discord DM' })}${label('automation.deliveryViaDm')}`, 'Your Discord DM', '#alerts'],
    ]);
    expect(within(list).getByText('Slack')).toBeInTheDocument();
    expect(within(list).getByText('Discord')).toBeInTheDocument();
    expect(api.getDeliveryOptions).toHaveBeenCalledWith('ws-1');
  });

  it('says where an app’s Default lands, and how it got there', async () => {
    serve({
      ...OPTIONS,
      apps: { slack: { ...OPTIONS.apps.slack, default: { address: 'slack:T1/C2', name: '#research', via: 'preferred' } } },
    });
    renderPicker();
    const list = await openList();

    const option = within(list).getByRole('option', { name: new RegExp(label('automation.deliveryDefaultOptionLands', { name: '#research' })) });
    expect(within(option).getByText(label('automation.deliveryViaPreferred'))).toBeInTheDocument();
  });

  it('picks several chats, each a chip by its name', async () => {
    serve();
    renderPicker();
    const list = await openList();

    fireEvent.click(within(list).getByRole('option', { name: '#research' }));
    fireEvent.click(within(list).getByRole('option', { name: /Default · Your Discord DM/ }));

    expect(picked()).toEqual(['slack:T1/C2', 'discord']);
    expect(chips().map((c) => c.textContent)).toEqual(['#research', 'Discord (Your Discord DM)']);
  });

  it('removes a chip', async () => {
    serve();
    renderPicker(['slack', 'slack:T1/C2']);

    fireEvent.click(await screen.findByRole('button', { name: label('automation.deliveryRemove', { name: '#research' }) }));

    expect(picked()).toEqual(['slack']);
  });

  it('marks the picked entries in the list, and unpicks one there', async () => {
    serve();
    renderPicker(['slack:T1/C1', 'discord']);
    const list = await openList();

    expect(within(list).getByRole('option', { name: '#demo' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(within(list).getByRole('option', { name: '#demo' }));

    expect(picked()).toEqual(['discord']);
  });

  it('keeps an entry it cannot list through an edit, named as best it can', async () => {
    serve();
    renderPicker(['slack:T1/C_GONE', 'telegram:@me', 'slack']);
    const list = await openList();

    fireEvent.click(within(list).getByRole('option', { name: '#research' }));

    expect(picked()).toEqual(['slack:T1/C_GONE', 'telegram:@me', 'slack', 'slack:T1/C2']);
    expect(chips().map((c) => c.textContent)).toEqual(['Slack:T1/C_GONE', 'Telegram DM', 'Slack (#demo)', '#research']);
  });

  it('names a chat from the last run when the apps no longer list it', async () => {
    serve();
    renderWithProviders(
      <MoreOptions
        form={{ ...IN_WORKSPACE, delivery_methods: ['slack:T1/C_GONE'] }}
        patch={vi.fn()}
        open
        onOpenChange={vi.fn()}
        deliveryAttempts={[{ method: 'slack:T1/C_GONE', success: true, address: 'slack:T1/C_GONE', name: '#old', via: 'agent' }]}
      />,
    );

    await addButton();
    expect(chips().map((c) => c.textContent)).toEqual(['#old']);
  });

  it('shows an app’s error quietly', async () => {
    serve({ ...OPTIONS, apps: { ...OPTIONS.apps, telegram: { chats: [], default: null, error: 'list_failed' } } });
    renderPicker();

    expect(await screen.findByText("Telegram: Couldn't load chats")).toBeInTheDocument();
    expect(screen.queryByText(/list_failed/)).not.toBeInTheDocument();
  });

  it('offers the apps alone where no messaging service is connected', async () => {
    serve({ enabled: false, apps: {}, workspace_id: 'ws-1' });
    const { queryClient } = renderPicker(['slack']);

    // Nothing on screen changes when the answer lands, so wait on the answer.
    await waitFor(() =>
      expect(queryClient.getQueryState(queryKeys.automationDelivery.options('ws-1'))?.status).toBe('success'),
    );
    const group = screen.getByRole('group', { name: label('automation.delivery') });
    expect(within(group).getByRole('button', { name: 'Slack' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('button', { name: new RegExp(label('automation.deliveryAdd')) })).not.toBeInTheDocument();
    expect(screen.queryByText(label('automation.deliveryOptionsUnavailable'))).not.toBeInTheDocument();
  });

  it('offers the apps alone, and says so, when the messaging service is unavailable', async () => {
    serve(refusal(503, { detail: 'The messaging service is unavailable.' }));
    renderPicker();

    expect(await screen.findByText(label('automation.deliveryOptionsUnavailable'))).toBeInTheDocument();
    const group = screen.getByRole('group', { name: label('automation.delivery') });
    fireEvent.click(within(group).getByRole('button', { name: 'Discord' }));
    expect(picked()).toEqual(['discord']);
  });

  it("reads the chats of an automation in the Chief of Staff with Home's id, and offers no default", async () => {
    // The server answers for Home as for a run in no workspace.
    serve({ ...OPTIONS, workspace_id: null });
    renderPicker(['slack:T1/C2'], { ...INITIAL_FORM, place: ALL_WORKSPACES });

    await addButton();
    expect(api.getDeliveryOptions).toHaveBeenCalledWith('home-1');
    expect(within(chips()[0]).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      label('automation.deliveryRemove', { name: '#research' }),
    ]);
    expect(screen.queryByText(label('automation.deliveryUseAsDefault'))).not.toBeInTheDocument();
    expect(screen.queryByText(label('automation.deliveryClearDefault'))).not.toBeInTheDocument();
  });

  it('names the chats in the folded summary', async () => {
    serve();
    renderPicker(['slack', 'discord:@me'], IN_WORKSPACE, false);

    expect(
      await screen.findByText(new RegExp(label('automation.deliversTo', { method: 'Slack (#demo), Your Discord DM' }).replace(/[()]/g, '\\$&'))),
    ).toBeInTheDocument();
  });
});

describe('the workspace default', () => {
  async function chipMenu(name: RegExp | string) {
    fireEvent.click(await screen.findByRole('button', { name }));
    return screen.findByRole('menu');
  }

  it('makes a picked chat this workspace’s default, then reads the chats again', async () => {
    serve();
    vi.mocked(api.setDeliveryDefault).mockResolvedValue({ data: { address: 'slack:T1/C2', name: '#research' } } as never);
    renderPicker(['slack:T1/C2']);
    await waitFor(() => expect(api.getDeliveryOptions).toHaveBeenCalledTimes(1));

    const menu = await chipMenu('#research');
    fireEvent.click(within(menu).getByRole('menuitem', { name: label('automation.deliveryUseAsDefault') }));

    await waitFor(() =>
      expect(api.setDeliveryDefault).toHaveBeenCalledWith({ workspace_id: 'ws-1', platform: 'slack', address: 'slack:T1/C2' }),
    );
    await waitFor(() => expect(api.getDeliveryOptions).toHaveBeenCalledTimes(2));
  });

  it('clears it from the chat that is the default', async () => {
    serve();
    vi.mocked(api.setDeliveryDefault).mockResolvedValue({ data: { address: null, name: null } } as never);
    renderPicker(['slack:T1/C1']);

    const menu = await chipMenu(/^#demo/);
    expect(within(menu).queryByRole('menuitem', { name: label('automation.deliveryUseAsDefault') })).not.toBeInTheDocument();
    fireEvent.click(within(menu).getByRole('menuitem', { name: label('automation.deliveryClearDefault') }));

    await waitFor(() =>
      expect(api.setDeliveryDefault).toHaveBeenCalledWith({ workspace_id: 'ws-1', platform: 'slack', address: null }),
    );
  });

  it('clears it from the app’s Default', async () => {
    serve();
    vi.mocked(api.setDeliveryDefault).mockResolvedValue({ data: { address: null, name: null } } as never);
    renderPicker(['slack']);

    const menu = await chipMenu('Slack (#demo)');
    fireEvent.click(within(menu).getByRole('menuitem', { name: label('automation.deliveryClearDefault') }));

    await waitFor(() =>
      expect(api.setDeliveryDefault).toHaveBeenCalledWith({ workspace_id: 'ws-1', platform: 'slack', address: null }),
    );
  });

  it('offers nothing to change on a Default the workspace did not set', async () => {
    serve();
    renderPicker(['discord']);

    await addButton();
    const chip = chips()[0];
    expect(within(chip).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      label('automation.deliveryRemove', { name: 'Discord (Your Discord DM)' }),
    ]);
  });

  it('offers no default on a chat the apps no longer list', async () => {
    serve();
    renderPicker(['slack:T1/C_GONE']);

    await addButton();
    const chip = chips()[0];
    expect(within(chip).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      label('automation.deliveryRemove', { name: 'Slack:T1/C_GONE' }),
    ]);
  });

  it('offers no default where the server says the run has no workspace, as in Home', async () => {
    serve({ ...OPTIONS, workspace_id: null });
    renderPicker(['slack:T1/C2', 'slack']);

    await addButton();
    for (const chip of chips()) {
      expect(within(chip).getAllByRole('button')).toHaveLength(1);
    }
    expect(api.getDeliveryOptions).toHaveBeenCalledWith('ws-1');
  });

  it('shows why a default was refused, beside the chips', async () => {
    serve();
    vi.mocked(api.setDeliveryDefault).mockRejectedValue(
      refusal(400, { detail: 'Not saved.', problems: [{ field: 'automation_output', message: 'The bot is not in #research.' }] }),
    );
    renderPicker(['slack:T1/C2']);

    const menu = await chipMenu('#research');
    fireEvent.click(within(menu).getByRole('menuitem', { name: label('automation.deliveryUseAsDefault') }));

    expect(await screen.findByRole('alert')).toHaveTextContent('The bot is not in #research.');
    expect(api.getDeliveryOptions).toHaveBeenCalledTimes(1);
  });

  it.each([
    [409, 'automation.deliveryDefaultBusy'],
    [503, 'automation.deliveryDefaultUnavailable'],
  ])('says a %s refusal in its own words', async (status, key) => {
    serve();
    vi.mocked(api.setDeliveryDefault).mockRejectedValue(refusal(status, { detail: 'nope' }));
    renderPicker(['slack:T1/C2']);

    const menu = await chipMenu('#research');
    fireEvent.click(within(menu).getByRole('menuitem', { name: label('automation.deliveryUseAsDefault') }));

    expect(await screen.findByRole('alert')).toHaveTextContent(label(key));
  });
});

describe('the workspace default across a workspace switch', () => {
  const OTHER: DeliveryOptions = {
    enabled: true,
    workspace_id: 'ws-2',
    apps: { slack: { ...OPTIONS.apps.slack, default: { address: 'slack:T1/C2', name: '#research', via: 'workspace' } } },
  };

  function Switcher({ methods }: { methods: string[] }) {
    const [form, setForm] = useState<FormState>({ ...IN_WORKSPACE, delivery_methods: methods });
    const patch: FormPatch = (key, value) => setForm((f) => ({ ...f, [key]: value }));
    return (
      <>
        <MoreOptions form={form} patch={patch} open onOpenChange={vi.fn()} />
        <button type="button" onClick={() => patch('place', 'ws-2')}>
          switch
        </button>
      </>
    );
  }

  it('offers no default, pin or default name while another workspace’s chats show', async () => {
    let release: (v: never) => void = () => {};
    vi.mocked(api.getDeliveryOptions).mockImplementation(async (id: string | null) =>
      id === 'ws-1' ? ({ data: { ...OPTIONS, workspace_id: id } } as never) : new Promise<never>((r) => (release = r)),
    );
    renderWithProviders(<Switcher methods={['slack', 'slack:T1/C1']} />);
    await waitFor(() => expect(chips().map((c) => c.textContent)).toEqual(['Slack (#demo)', '#demo']));
    expect(within(chips()[1]).getAllByRole('button')).toHaveLength(2);

    fireEvent.click(screen.getByText('switch'));
    await waitFor(() => expect(api.getDeliveryOptions).toHaveBeenCalledWith('ws-2'));

    // Still ws-1's chats, but none of its defaults.
    expect(chips().map((c) => c.textContent)).toEqual(['Slack', '#demo']);
    expect(document.querySelector('.automation-delivery-chip svg.lucide-pin')).toBeNull();
    for (const c of chips()) expect(within(c).getAllByRole('button')).toHaveLength(1);

    release({ data: OTHER } as never);
    await waitFor(() => expect(chips().map((c) => c.textContent)).toEqual(['Slack (#research)', '#demo']));
  });

  it('invalidates the workspace the change was made for when it lands after a switch', async () => {
    let finish: (v: never) => void = () => {};
    serve();
    vi.mocked(api.setDeliveryDefault).mockImplementation(() => new Promise<never>((r) => (finish = r)));
    const { queryClient } = renderWithProviders(<Switcher methods={['slack:T1/C2']} />);
    fireEvent.click(await screen.findByRole('button', { name: '#research' }));
    fireEvent.click(within(await screen.findByRole('menu')).getByRole('menuitem', { name: label('automation.deliveryUseAsDefault') }));
    await waitFor(() => expect(api.setDeliveryDefault).toHaveBeenCalled());
    expect(api.setDeliveryDefault).toHaveBeenCalledWith({ workspace_id: 'ws-1', platform: 'slack', address: 'slack:T1/C2' });

    fireEvent.click(screen.getByText('switch'));
    await waitFor(() => expect(api.getDeliveryOptions).toHaveBeenCalledWith('ws-2'));
    const spy = vi.spyOn(queryClient, 'invalidateQueries');
    finish({ data: { address: 'slack:T1/C2', name: '#research' } } as never);

    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.automationDelivery.options('ws-1') });
    expect(spy).not.toHaveBeenCalledWith({ queryKey: queryKeys.automationDelivery.options('ws-2') });
  });

  it('shows the new default when the read after the change fails', async () => {
    vi.mocked(api.getDeliveryOptions).mockResolvedValueOnce({ data: OPTIONS } as never);
    vi.mocked(api.getDeliveryOptions).mockRejectedValue(refusal(503, { detail: 'down' }));
    vi.mocked(api.setDeliveryDefault).mockResolvedValue({ data: { address: 'slack:T1/C2', name: '#research' } } as never);
    renderPicker(['slack', 'slack:T1/C2']);

    fireEvent.click(await screen.findByRole('button', { name: '#research' }));
    fireEvent.click(within(await screen.findByRole('menu')).getByRole('menuitem', { name: label('automation.deliveryUseAsDefault') }));

    await waitFor(() => expect(api.getDeliveryOptions).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(chips().map((c) => c.textContent)).toEqual(['Slack (#research)', '#research']));
  });
});

describe('entries the old control cannot show', () => {
  it('keeps them, read-only, while the chats load', async () => {
    vi.mocked(api.getDeliveryOptions).mockReturnValue(new Promise(() => {}));
    renderPicker(['slack:T1/C', 'telegram:@me']);

    await waitFor(() => expect(chips()).toHaveLength(2));
    expect(screen.queryByRole('group', { name: label('automation.delivery') })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Slack' })).not.toBeInTheDocument();
    expect(picked()).toEqual(['slack:T1/C', 'telegram:@me']);
  });

  it('keeps them, read-only, when the chats fail to load', async () => {
    serve(refusal(503, { detail: 'down' }));
    renderPicker(['slack:T1/C', 'telegram:@me']);

    expect(await screen.findByText(label('automation.deliveryOptionsUnavailable'))).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Slack' })).not.toBeInTheDocument();
    expect(chips()).toHaveLength(2);
    expect(picked()).toEqual(['slack:T1/C', 'telegram:@me']);
  });

  it('still offers the control for an entry it can show', async () => {
    vi.mocked(api.getDeliveryOptions).mockReturnValue(new Promise(() => {}));
    renderPicker(['slack']);

    const group = await screen.findByRole('group', { name: label('automation.delivery') });
    expect(within(group).getByRole('button', { name: 'Slack' })).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('a save refused over delivery', () => {
  function renderForm(onSubmit: () => Promise<void>) {
    serve();
    return renderWithProviders(
      <AutomationInlineForm
        initialValues={{ ...IN_WORKSPACE, name: 'Brief', instruction: 'Summarize.', delivery_methods: ['slack', 'slack:T1/C2'] }}
        original={null}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        loading={false}
      />,
    );
  }

  it('shows each refused entry by name, unfolding the options', async () => {
    // The server's shape: the joined sentence in `detail`, each entry beside it.
    const onSubmit = vi.fn().mockRejectedValue(
      refusal(409, {
        detail: "'slack:T1/C2': the bot isn't in the channel",
        problems: [{ entry: 'slack:T1/C2', message: "the bot isn't in the channel" }],
      }),
    );
    renderForm(onSubmit);
    await waitFor(() => expect(api.getDeliveryOptions).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: label('common.create') }));

    expect(await screen.findByRole('alert')).toHaveTextContent("#research: the bot isn't in the channel");
    expect(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) })).toHaveAttribute('aria-expanded', 'true');
    const chip = chips().find((c) => c.textContent === '#research');
    expect(chip).toHaveAttribute('data-invalid');
  });

  it('leaves a refusal naming no entry to the message alone', async () => {
    const onSubmit = vi.fn().mockRejectedValue(refusal(409, { detail: "'slack:T1/C2': the bot isn't in the channel" }));
    renderForm(onSubmit);

    fireEvent.click(screen.getByRole('button', { name: label('common.create') }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) })).toHaveAttribute('aria-expanded', 'false');
  });

  it('forgets the refusal once the delivery changes', async () => {
    const onSubmit = vi.fn().mockRejectedValue(
      refusal(409, { detail: { message: 'x', problems: [{ entry: 'slack:T1/C2', message: 'gone' }] } }),
    );
    renderForm(onSubmit);
    fireEvent.click(screen.getByRole('button', { name: label('common.create') }));
    await screen.findByRole('alert');

    fireEvent.click(screen.getByRole('button', { name: label('automation.deliveryRemove', { name: '#research' }) }));

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

// DALTON-HOOK(automation-model): component tests for model selector in MoreOptions & AutomationInlineForm
describe('model selection in MoreOptions and AutomationInlineForm', () => {
  const SAVED_AUTOMATION: Automation = {
    automation_id: 'auto-1',
    user_id: 'user-1',
    name: 'Brief',
    description: null,
    trigger_type: 'cron',
    cron_expression: '0 9 * * 1-5',
    timezone: 'America/New_York',
    trigger_config: null,
    next_run_at: '2026-09-26T13:00:00Z',
    last_run_at: null,
    instruction: 'Summarize.',
    workspace_id: 'ws-1',
    llm_model: 'claude-sonnet-4-5',
    thread_strategy: 'continue',
    conversation_thread_id: null,
    status: 'active',
    max_failures: 3,
    failure_count: 0,
    delivery_config: { methods: [] },
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    last_execution: null,
  };

  function setupAvailableModels() {
    allModelsMock.current = {
      models: {
        anthropic: { display_name: 'Anthropic', models: ['claude-sonnet-4-5'] },
        openai: { display_name: 'OpenAI', models: ['gpt-4o'] },
      },
      lockedModels: {
        openai: { display_name: 'OpenAI', models: ['o3-pro-locked'] },
      },
      keylessModels: new Set<string>(),
      metadata: {
        'claude-sonnet-4-5': { display_name: 'Claude Sonnet 4.5', provider: 'anthropic' },
        'gpt-4o': { display_name: 'GPT-4o', provider: 'openai' },
        'o3-pro-locked': { display_name: 'o3 Pro Locked', provider: 'openai' },
      },
      payers: {},
      isLoading: false,
      rawApiResponse: {} as unknown,
      isLoaded: true,
    };
  }

  it('lists only available models with Default model (account default) as the first option', () => {
    setupAvailableModels();
    serve();
    renderPicker();

    const modelSelect = screen.getByLabelText(label('automation.detailModel')) as HTMLSelectElement;
    const options = [...modelSelect.options].map((o) => ({ value: o.value, text: o.textContent }));
    expect(options).toEqual([
      { value: '', text: label('automation.defaultModelOption') },
      { value: 'claude-sonnet-4-5', text: 'Claude Sonnet 4.5' },
      { value: 'gpt-4o', text: 'GPT-4o' },
    ]);
  });

  it('creates an automation with a chosen model or with the default model', async () => {
    setupAvailableModels();
    serve();
    const submissions: FormSubmission[] = [];
    const onSubmit = vi.fn((sub: FormSubmission) => {
      submissions.push(sub);
    });

    renderWithProviders(
      <AutomationInlineForm
        initialValues={{ ...IN_WORKSPACE, name: 'Brief', instruction: 'Summarize.' }}
        original={null}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        loading={false}
      />,
    );

    // Submit with default model (null)
    fireEvent.click(screen.getByRole('button', { name: label('common.create') }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(submissions[0]).toMatchObject({ kind: 'create' });
    expect(submissions[0].payload).not.toHaveProperty('llm_model');

    // Open More options, select a specific model, and submit
    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) }));
    const modelSelect = screen.getByLabelText(label('automation.detailModel'));
    fireEvent.change(modelSelect, { target: { value: 'gpt-4o' } });
    fireEvent.click(screen.getByRole('button', { name: label('common.create') }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(submissions[1]).toMatchObject({
      kind: 'create',
      payload: { llm_model: 'gpt-4o' },
    });
  });

  it('preserves original llm_model when untouched on edit and sends explicit null when switched back to default', async () => {
    setupAvailableModels();
    serve();
    const submissions: FormSubmission[] = [];
    const onSubmit = vi.fn((sub: FormSubmission) => {
      submissions.push(sub);
    });

    const initial = automationToFormState(SAVED_AUTOMATION, 'America/New_York', 'home-1');
    renderWithProviders(
      <AutomationInlineForm
        initialValues={initial}
        original={SAVED_AUTOMATION}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        loading={false}
      />,
    );

    // Save without touching model -> preserves original value (omitted in patch)
    fireEvent.change(screen.getByLabelText(label('common.name')), { target: { value: 'Brief v2' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(submissions[0].kind).toBe('edit');
    expect(submissions[0].payload).not.toHaveProperty('llm_model');

    // Open More options, switch model back to default ("") -> explicitly sends llm_model: null
    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) }));
    const modelSelect = screen.getByLabelText(label('automation.detailModel')) as HTMLSelectElement;
    expect(modelSelect.value).toBe('claude-sonnet-4-5');
    fireEvent.change(modelSelect, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(submissions[1]).toMatchObject({
      kind: 'edit',
      payload: { llm_model: null },
    });
  });

  // DALTON-HOOK(automation-model): an empty list before the catalog has arrived (or
  // after it failed) is not "no models"; the selector waits and the saved model stays
  it.each([
    ['still loading', { isLoading: true, rawApiResponse: null }, {}, 'automation.modelCatalogLoading'],
    ['failed to load', { isLoading: false, rawApiResponse: null }, {}, 'automation.modelCatalogUnavailable'],
    ['loaded but its keys failed', { isLoading: false, rawApiResponse: {} }, null, 'automation.modelCatalogUnavailable'],
    ['loaded but a subscription status failed', { isLoading: false, rawApiResponse: {} }, {}, 'automation.modelCatalogUnavailable', true],
  ])('keeps the saved model while the catalog is %s', async (_state, catalog, keys, statusKey, statusError = false) => {
    allModelsMock.current = { ...allModelsMock.current, models: {}, keylessModels: new Set<string>(), ...catalog };
    apiKeysMock.current = { apiKeys: keys };
    providersMock.current = { statusError };
    serve();
    const onSubmit = vi.fn();
    const initial = automationToFormState(SAVED_AUTOMATION, 'America/New_York', 'home-1');
    renderWithProviders(
      <AutomationInlineForm
        initialValues={initial}
        original={SAVED_AUTOMATION}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        loading={false}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) }));
    expect(screen.queryByLabelText(label('automation.detailModel'))).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(label(statusKey));

    fireEvent.change(screen.getByLabelText(label('common.name')), { target: { value: 'Brief v2' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].payload).not.toHaveProperty('llm_model');
  });

  // DALTON-HOOK(automation-model): only a model the keys mark keyless is called
  // unavailable; any other absence (a request that failed) reads as the current setting
  it('shows a saved model missing from the list as the current setting unless it is keyless', async () => {
    allModelsMock.current = { ...allModelsMock.current, models: {}, keylessModels: new Set<string>() };
    serve();
    const initial = automationToFormState(SAVED_AUTOMATION, 'America/New_York', 'home-1');
    renderWithProviders(
      <AutomationInlineForm
        initialValues={initial}
        original={SAVED_AUTOMATION}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        loading={false}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) }));
    const select = screen.getByLabelText(label('automation.detailModel')) as HTMLSelectElement;
    const shown = select.options[select.selectedIndex].textContent ?? '';
    expect(shown).not.toMatch(/unavailable/i);
    expect(shown).toMatch(/current setting/i);
  });

  it('displays server error on the form when saving an unavailable model fails', async () => {
    setupAvailableModels();
    serve();
    const errorMsg = "unknown model 'claude-sonnet-4-5'; use null for the user's default, or one of: gpt-4o";
    const onSubmit = vi.fn().mockRejectedValue(refusal(409, { detail: errorMsg }));

    const initial = automationToFormState(SAVED_AUTOMATION, 'America/New_York', 'home-1');
    renderWithProviders(
      <AutomationInlineForm
        initialValues={initial}
        original={SAVED_AUTOMATION}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        loading={false}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    expect(await screen.findByRole('alert')).toHaveTextContent(errorMsg);
    expect(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  it('excludes keyless custom models from the list, while showing a saved keyless model as unavailable so it can be switched back to default', async () => {
    allModelsMock.current = {
      models: {
        openai: { display_name: 'OpenAI', models: ['gpt-4o', 'custom-keyless'] },
        custom_prov: { display_name: 'Custom Provider', models: ['only-keyless'] },
      },
      lockedModels: {},
      keylessModels: new Set(['custom-keyless', 'only-keyless']),
      metadata: {
        'gpt-4o': { display_name: 'GPT-4o', provider: 'openai' },
        'custom-keyless': { display_name: 'Custom Keyless', provider: 'openai' },
        'only-keyless': { display_name: 'Only Keyless', provider: 'custom_prov' },
      },
      payers: {},
      isLoading: false,
      rawApiResponse: {} as unknown,
      isLoaded: true,
    };
    serve();

    // New form: keyless models are excluded from the options list
    const { unmount } = renderPicker();
    const createSelect = screen.getByLabelText(label('automation.detailModel')) as HTMLSelectElement;
    expect([...createSelect.options].map((o) => ({ value: o.value, text: o.textContent }))).toEqual([
      { value: '', text: label('automation.defaultModelOption') },
      { value: 'gpt-4o', text: 'GPT-4o' },
    ]);
    unmount();

    // Edit form with a saved keyless model: shown as unavailable, and can be switched back to default
    const savedKeyless: Automation = { ...SAVED_AUTOMATION, llm_model: 'custom-keyless' };
    const initial = automationToFormState(savedKeyless, 'America/New_York', 'home-1');
    const submissions: FormSubmission[] = [];
    const onSubmit = vi.fn((sub: FormSubmission) => {
      submissions.push(sub);
    });

    renderWithProviders(
      <AutomationInlineForm
        initialValues={initial}
        original={savedKeyless}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        loading={false}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) }));
    const editSelect = screen.getByLabelText(label('automation.detailModel')) as HTMLSelectElement;
    expect(editSelect.value).toBe('custom-keyless');
    expect([...editSelect.options].map((o) => ({ value: o.value, text: o.textContent }))).toEqual([
      { value: '', text: label('automation.defaultModelOption') },
      { value: 'custom-keyless', text: label('automation.modelUnavailableOption', { model: 'Custom Keyless' }) },
      { value: 'gpt-4o', text: 'GPT-4o' },
    ]);

    fireEvent.change(editSelect, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(submissions[0]).toMatchObject({
      kind: 'edit',
      payload: { llm_model: null },
    });
  });

  it('renders Default model (account default) even when available models are empty and allows clearing a saved model to null', async () => {
    allModelsMock.current = {
      models: {},
      lockedModels: {},
      keylessModels: new Set(),
      metadata: {
        'claude-sonnet-4-5': { display_name: 'Claude Sonnet 4.5', provider: 'anthropic' },
      },
      payers: {},
      isLoading: false,
      rawApiResponse: {} as unknown,
      isLoaded: true,
    };
    serve();

    const initial = automationToFormState(SAVED_AUTOMATION, 'America/New_York', 'home-1');
    const submissions: FormSubmission[] = [];
    const onSubmit = vi.fn((sub: FormSubmission) => {
      submissions.push(sub);
    });

    renderWithProviders(
      <AutomationInlineForm
        initialValues={initial}
        original={SAVED_AUTOMATION}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        loading={false}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: new RegExp(label('automation.moreOptions')) }));
    const modelSelect = screen.getByLabelText(label('automation.detailModel')) as HTMLSelectElement;
    expect(modelSelect.value).toBe('claude-sonnet-4-5');
    expect([...modelSelect.options].map((o) => ({ value: o.value, text: o.textContent }))).toEqual([
      { value: '', text: label('automation.defaultModelOption') },
      // Not keyless, so missing from an empty list reads as the current setting.
      { value: 'claude-sonnet-4-5', text: label('automation.modelCurrentOption', { model: 'Claude Sonnet 4.5' }) },
    ]);

    fireEvent.change(modelSelect, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(submissions[0]).toMatchObject({
      kind: 'edit',
      payload: { llm_model: null },
    });
  });
});
