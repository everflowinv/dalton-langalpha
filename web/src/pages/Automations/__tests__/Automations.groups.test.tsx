// DALTON-HOOK(company-automations): manage groups, company writes, and deep links.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import i18n from '@/i18n';
import { queryKeys } from '@/lib/queryKeys';
import { renderWithProviders } from '@/test/utils';
import type { AgentAutomationRead, AutomationRun, ScriptAutomationRead, ScriptExecutionRead } from '@/types/automation';
import * as api from '../utils/api';
import Automations from '../Automations';

const mobile = vi.hoisted(() => ({ on: false }));
const auth = vi.hoisted(() => ({ userId: 'user-1' }));
const home = vi.hoisted(() => ({ getHomeWorkspace: vi.fn() }));

vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => mobile.on }));
vi.mock('@/hooks/useHomeTimezone', () => ({ useHomeTimezone: () => 'America/New_York' }));
vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [] }, isPending: false, isLoading: false }),
}));
vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));
vi.mock('@/pages/ChatAgent/utils/api/workspaces', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getHomeWorkspace: home.getHomeWorkspace,
}));
vi.mock('@/contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({ userId: auth.userId, isLoggedIn: true, isInitialized: true }),
  };
});
vi.mock('../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/api')>();
  return {
    ...actual,
    listAutomations: vi.fn(),
    listExecutions: vi.fn(),
    listRecentRuns: vi.fn(),
    listAutomationReads: vi.fn(),
    listAutomationExecutionReads: vi.fn(),
    getAutomationRead: vi.fn(),
    getDeliveryOptions: vi.fn(),
    updateAutomation: vi.fn(),
    updateCompanyScript: vi.fn(),
    pauseAutomation: vi.fn(),
    createAutomation: vi.fn(),
  };
});

const HOME_ROW = { workspace_id: 'home-1', is_home: true };
const label = (key: string, opts?: Record<string, unknown>) => i18n.t(key, opts);

function httpError(status: number, detail: string): Error {
  const error = new Error(detail);
  return Object.assign(error, { response: { status, data: { detail } } });
}

function emptyPage() {
  return { data: { automations: [], total: 0 } } as never;
}

function agent(over: Partial<AgentAutomationRead> & Pick<AgentAutomationRead, 'automation_id' | 'name' | 'scope'>): AgentAutomationRead {
  return {
    user_id: 'user-1',
    description: null,
    trigger_type: 'cron',
    cron_expression: '0 9 * * 1-5',
    timezone: 'America/New_York',
    trigger_config: null,
    next_run_at: null,
    last_run_at: null,
    instruction: 'Summarize the desk.',
    workspace_id: 'home-1',
    llm_model: null,
    additional_context: null,
    thread_strategy: 'new',
    conversation_thread_id: null,
    status: 'active',
    max_failures: 3,
    failure_count: 0,
    delivery_config: { methods: ['slack'] },
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-02T00:00:00Z',
    last_execution: null,
    engine_kind: 'agent',
    notify_storage_mode: 'default',
    config_version: 2,
    active_version: 2,
    created_by: 'user-1',
    updated_by: null,
    ...over,
  };
}

function scriptRun(automationId: string): ScriptExecutionRead {
  return {
    automation_execution_id: 'run-1',
    automation_id: automationId,
    status: 'succeeded',
    scheduled_at: '2026-11-03T06:00:00Z',
    created_at: '2026-11-03T06:00:01Z',
    engine_kind: 'script',
    conversation_thread_id: null,
    started_at: '2026-11-03T06:00:02Z',
    completed_at: '2026-11-03T06:00:10Z',
    exit_code: 0,
    failure_reason: null,
    notify_status: null,
    triggered_by_user_id: null,
    log_tail: null,
    trigger_source: null,
  };
}

function script(over: Partial<ScriptAutomationRead> & Pick<ScriptAutomationRead, 'automation_id' | 'name'>): ScriptAutomationRead {
  return {
    status: 'active',
    enabled: true,
    paused_by_user: false,
    cron_expression: '0 9 * * 1-5',
    timezone: 'America/New_York',
    config_version: 3,
    active_version: 3,
    updated_at: '2026-11-03T00:00:00Z',
    notify_storage_mode: 'task_row',
    engine_kind: 'script',
    scope: 'company',
    description: null,
    next_run_at: null,
    last_run_at: '2026-11-03T06:00:00Z',
    updated_by: null,
    dalton_notify_category: null,
    dalton_notify_effective: null,
    dalton_notify_source: null,
    dalton_notify_route_targets: null,
    last_execution: scriptRun(over.automation_id),
    ...over,
  };
}

function renderAt(route: string) {
  return renderWithProviders(<Automations />, { route });
}

function inspector(name: string) {
  const heading = screen.getByRole('heading', { level: 2, name });
  const article = heading.closest('article');
  if (!article) throw new Error(`missing inspector for ${name}`);
  return within(article);
}

beforeEach(() => {
  mobile.on = false;
  auth.userId = 'user-1';
  localStorage.clear();
  home.getHomeWorkspace.mockResolvedValue(HOME_ROW);
  vi.mocked(api.listAutomations).mockResolvedValue({ data: { automations: [], total: 0 } } as never);
  vi.mocked(api.listExecutions).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listRecentRuns).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listAutomationExecutionReads).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listAutomationReads).mockResolvedValue(emptyPage());
  vi.mocked(api.getAutomationRead).mockRejectedValue(httpError(404, 'missing'));
  vi.mocked(api.getDeliveryOptions).mockResolvedValue({ data: { enabled: false, apps: {}, workspace_id: null } } as never);
  vi.mocked(api.updateAutomation).mockResolvedValue({ data: {} } as never);
  vi.mocked(api.updateCompanyScript).mockResolvedValue({ data: {} } as never);
  vi.mocked(api.pauseAutomation).mockResolvedValue({ data: {} } as never);
  vi.mocked(api.createAutomation).mockResolvedValue({ data: {} } as never);
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('three manage groups', () => {
  it('pages each group on its own past 100 items', async () => {
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      const scope = params?.scope;
      const offset = params?.offset ?? 0;
      if (scope !== 'company' && scope !== 'personal') return emptyPage();
      return {
        data: {
          automations: [agent({ automation_id: `${scope}-${offset}`, name: `${scope} ${offset}`, scope })],
          total: 150,
        },
      } as never;
    });
    renderAt('/automations?view=manage');

    expect((await screen.findAllByText('company 0')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('personal 0').length).toBeGreaterThan(0);

    const companyNext = () =>
      screen.getByRole('button', { name: `${label('automation.nextPage')} ${label('automation.groupCompany')}` });
    fireEvent.click(companyNext());
    expect((await screen.findAllByText('company 50')).length).toBeGreaterThan(0);
    fireEvent.click(companyNext());
    expect((await screen.findAllByText('company 100')).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.queryAllByText('company 0')).toHaveLength(0));
    expect(screen.getAllByText('personal 0').length).toBeGreaterThan(0);
    expect(companyNext()).toBeDisabled();
    expect(api.listAutomationReads).toHaveBeenCalledWith({ scope: 'company', limit: 50, offset: 100 });
    expect(api.listAutomationReads).toHaveBeenCalledWith({ scope: 'personal', limit: 50, offset: 0 });

    const personalNext = () =>
      screen.getByRole('button', { name: `${label('automation.nextPage')} ${label('automation.groupMine')}` });
    fireEvent.click(personalNext());
    expect((await screen.findAllByText('personal 50')).length).toBeGreaterThan(0);
    fireEvent.click(personalNext());
    expect((await screen.findAllByText('personal 100')).length).toBeGreaterThan(0);
    expect(screen.queryAllByText('personal 0')).toHaveLength(0);
    expect(screen.getAllByText('company 100').length).toBeGreaterThan(0);
    expect(personalNext()).toBeDisabled();
    expect(api.listAutomationReads).toHaveBeenCalledWith({ scope: 'personal', limit: 50, offset: 100 });
  });

  it('keeps a company refusal inside the company group', async () => {
    const secret = 'Quarterly ledger alpha';
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope === 'company') throw httpError(403, secret);
      if (params?.scope === 'personal') {
        return { data: { automations: [agent({ automation_id: 'me-1', name: 'My morning note', scope: 'personal' })], total: 1 } } as never;
      }
      if (params?.scope === 'research_tracking') {
        return { data: { automations: [agent({ automation_id: 'rs-1', name: 'Filing watch', scope: 'research_tracking' })], total: 1 } } as never;
      }
      return emptyPage();
    });
    renderAt('/automations?view=manage');

    expect(await screen.findByText(label('automation.companyUnavailable'))).toBeInTheDocument();
    const personal = screen.getByRole('region', { name: label('automation.groupMine') });
    const research = screen.getByRole('region', { name: label('automation.groupResearch') });
    expect(within(personal).getByRole('button', { name: /My morning note/ })).toBeInTheDocument();
    expect(within(research).getByRole('button', { name: /Filing watch/ })).toBeInTheDocument();
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2, name: secret })).not.toBeInTheDocument();
  });

  it('shows a company load error when the other groups are empty', async () => {
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope === 'company') throw httpError(503, 'Unavailable');
      return emptyPage();
    });
    renderAt('/automations?view=manage');

    const company = await screen.findByRole('region', { name: label('automation.groupCompany') });
    expect(within(company).getByText(label('automation.scopeLoadFailed'))).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: label('automation.groupMine') })).getByText(label('automation.scopeEmpty')),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: label('automation.groupResearch') })).getByText(label('automation.scopeEmpty')),
    ).toBeInTheDocument();
  });

  it('replaces the previous user when the signed-in user changes', async () => {
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'personal') return emptyPage();
      const mine = auth.userId === 'user-1';
      return {
        data: {
          automations: [agent({ automation_id: mine ? 'first-1' : 'second-1', name: mine ? 'First desk' : 'Second desk', scope: 'personal' })],
          total: 1,
        },
      } as never;
    });
    const view = renderAt('/automations?view=manage');
    expect((await screen.findAllByText('First desk')).length).toBeGreaterThan(0);

    auth.userId = 'user-2';
    view.rerender(<Automations />);

    expect((await screen.findAllByText('Second desk')).length).toBeGreaterThan(0);
    expect(screen.queryAllByText('First desk')).toHaveLength(0);
  });
});

describe('company script and agent details', () => {
  const prices = script({ automation_id: 'data:prices', name: 'Close prices', config_version: 5 });
  const desk = agent({
    automation_id: 'co-agent',
    name: 'Desk brief',
    scope: 'company',
    config_version: 6,
    delivery_config: { methods: ['slack'] },
  });

  function serveCompany(tasks: Array<ScriptAutomationRead | AgentAutomationRead>) {
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: tasks, total: tasks.length } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      const found = tasks.find((task) => task.automation_id === id);
      if (!found) throw httpError(404, 'missing');
      return { data: found } as never;
    });
  }

  it('offers schedule controls for a script and edit plus pause for a company agent', async () => {
    serveCompany([prices, desk]);
    renderAt('/automations?view=manage');

    fireEvent.click(await screen.findByRole('button', { name: /Close prices/ }));
    const scriptPane = inspector('Close prices');
    await waitFor(() => expect(scriptPane.getByRole('button', { name: label('automation.pause') })).toBeEnabled());
    expect(scriptPane.getByRole('button', { name: label('automation.edit') })).toBeEnabled();
    expect(scriptPane.queryByRole('button', { name: label('automation.runNow') })).not.toBeInTheDocument();
    expect(scriptPane.queryByRole('button', { name: label('common.delete') })).not.toBeInTheDocument();
    expect(scriptPane.queryByRole('button', { name: label('automation.openThread') })).not.toBeInTheDocument();
    const scriptRun = await scriptPane.findByRole('listitem');
    expect(scriptRun.textContent).toContain(label('automation.runSucceeded'));
    expect(scriptRun.textContent).toContain('·');

    fireEvent.click(screen.getByRole('button', { name: /Desk brief/ }));
    const agentPane = inspector('Desk brief');
    // The pane crossfade keeps the outgoing inspector mounted until its exit finishes.
    await waitFor(() => {
      expect(screen.queryByRole('heading', { level: 2, name: 'Close prices' })).not.toBeInTheDocument();
    });
    await waitFor(() => expect(agentPane.getByRole('button', { name: label('automation.pause') })).toBeEnabled());
    expect(agentPane.getByRole('button', { name: label('automation.edit') })).toBeEnabled();
    expect(agentPane.queryByRole('button', { name: label('automation.runNow') })).not.toBeInTheDocument();
    expect(agentPane.queryByRole('button', { name: label('common.delete') })).not.toBeInTheDocument();
  });

  it('opens a script that is not on the current page', async () => {
    serveCompany([desk]);
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      if (id !== 'data:prices') throw httpError(404, 'missing');
      return { data: prices } as never;
    });
    renderAt(`/automations?id=${encodeURIComponent('data:prices')}`);

    const heading = await screen.findByRole('heading', { level: 2, name: 'Close prices' });
    expect(api.getAutomationRead).toHaveBeenCalledWith('data:prices');
    const linkedRun = await within(heading.closest('article')!).findByRole('listitem');
    expect(linkedRun.textContent).toContain(label('automation.runSucceeded'));
    expect(linkedRun.textContent).toContain('·');
    expect(screen.queryByText(label('automation.notFound'))).not.toBeInTheDocument();
  });

  it('sends the detail version for a company agent and leaves delivery off the request', async () => {
    const listed = agent({ automation_id: 'co-1', name: 'Desk', scope: 'company', config_version: 2 });
    const opened = agent({ ...listed, config_version: 7 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [listed], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      if (id !== 'co-1') throw httpError(404, 'missing');
      return { data: opened } as never;
    });
    renderAt('/automations?view=manage');

    const edit = () => inspector('Desk').getByRole('button', { name: label('automation.edit') });
    await waitFor(() => expect(edit()).toBeEnabled());
    fireEvent.click(edit());
    fireEvent.change(await screen.findByLabelText(label('common.name')), { target: { value: 'Desk hours' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    await waitFor(() => expect(api.updateAutomation).toHaveBeenCalled());
    const [id, payload] = vi.mocked(api.updateAutomation).mock.calls[0];
    expect(id).toBe('co-1');
    expect(payload.name).toBe('Desk hours');
    expect(payload.expected_config_version).toBe(7);
    expect(payload).not.toHaveProperty('delivery_config');
    expect(payload).not.toHaveProperty('config_version');
    expect(payload).not.toHaveProperty('scope');
    expect(payload).not.toHaveProperty('user_id');
  });

  it('uses the version from the write that just finished for the next company pause', async () => {
    let version = 7;
    const listed = agent({ automation_id: 'co-1', name: 'Desk', scope: 'company', config_version: 2 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [listed], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      if (id !== 'co-1') throw httpError(404, 'missing');
      return { data: agent({ ...listed, config_version: version }) } as never;
    });
    vi.mocked(api.pauseAutomation).mockImplementation(async () => {
      version += 1;
      return { data: {} } as never;
    });
    renderAt('/automations?view=manage');

    const pause = () => inspector('Desk').getByRole('button', { name: label('automation.pause') });
    await waitFor(() => expect(pause()).toBeEnabled());
    fireEvent.click(pause());
    await waitFor(() => expect(api.pauseAutomation).toHaveBeenCalledWith('co-1', 7));
    await waitFor(() => expect(pause()).toBeEnabled());
    fireEvent.click(pause());
    await waitFor(() => expect(api.pauseAutomation).toHaveBeenLastCalledWith('co-1', 8));
  });
});

describe('a company script save', () => {
  const id = 'data:prices';

  it('keeps the draft after 409 and sends it again only when asked', async () => {
    let current = script({ automation_id: id, name: 'Close prices', cron_expression: '0 9 * * 1-5', timezone: 'America/New_York', config_version: 3 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [current], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async () => ({ data: current }) as never);
    vi.mocked(api.updateCompanyScript).mockImplementation(async () => {
      current = { ...current, config_version: 4, cron_expression: '15 8 * * 1-5', timezone: 'UTC' };
      throw httpError(409, 'Someone else saved this task.');
    });
    renderAt('/automations?view=manage');

    const edit = () => inspector('Close prices').getByRole('button', { name: label('automation.edit') });
    await waitFor(() => expect(edit()).toBeEnabled());
    fireEvent.click(edit());
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: '0 10 * * 1-5' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    expect(await screen.findByText(label('automation.currentVersion', { version: 4 }))).toBeInTheDocument();
    expect(screen.getByText(label('automation.serverSchedule', { cron: '15 8 * * 1-5', zone: 'UTC' }))).toBeInTheDocument();
    expect(screen.getByLabelText(label('automation.scriptCron'))).toHaveValue('0 10 * * 1-5');
    expect(api.updateCompanyScript).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.updateCompanyScript).mock.calls[0]).toEqual([
      id,
      { expected_config_version: 3, cron_expression: '0 10 * * 1-5' },
    ]);

    const retry = await screen.findByRole('button', { name: label('automation.retryWithCurrent') });
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);
    await waitFor(() => expect(api.updateCompanyScript).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.updateCompanyScript).mock.calls[1]).toEqual([
      id,
      { expected_config_version: 4, cron_expression: '0 10 * * 1-5' },
    ]);
  });

  it('clears the open draft when the task becomes forbidden', async () => {
    const draft = '7 7 * * 7';
    let forbidden = false;
    const current = script({ automation_id: id, name: 'Close prices', cron_expression: '0 9 * * 1-5' });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [current], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async () => {
      if (forbidden) throw httpError(403, 'restricted');
      return { data: current } as never;
    });
    vi.mocked(api.updateCompanyScript).mockImplementation(async () => {
      forbidden = true;
      throw httpError(409, 'updated');
    });
    renderAt('/automations?view=manage');

    const edit = () => inspector('Close prices').getByRole('button', { name: label('automation.edit') });
    await waitFor(() => expect(edit()).toBeEnabled());
    fireEvent.click(edit());
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: draft } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    expect(await screen.findByText(label('automation.detailForbidden'))).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByDisplayValue(draft)).not.toBeInTheDocument());
    expect(screen.queryByText(draft)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: label('automation.pause') })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: label('automation.edit') })).not.toBeInTheDocument();
    expect(screen.getByText('Close prices')).toBeInTheDocument();
  });

  it('hides a cached company detail and its draft when the company list is refused', async () => {
    const draft = 'draft-cron-7-7';
    let companyRefused = false;
    const current = script({ automation_id: id, name: 'Close prices', cron_expression: '0 9 * * 1-5', config_version: 3 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope === 'company') {
        if (companyRefused) throw httpError(403, 'Quarterly ledger alpha');
        return { data: { automations: [current], total: 51 } } as never;
      }
      if (params?.scope === 'personal') {
        return { data: { automations: [agent({ automation_id: 'me-1', name: 'My morning note', scope: 'personal' })], total: 1 } } as never;
      }
      if (params?.scope === 'research_tracking') {
        return { data: { automations: [agent({ automation_id: 'rs-1', name: 'Filing watch', scope: 'research_tracking' })], total: 1 } } as never;
      }
      return emptyPage();
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (requested) => {
      if (requested !== id) throw httpError(404, 'missing');
      return { data: current } as never;
    });
    renderAt(`/automations?view=manage&id=${encodeURIComponent(id)}`);

    const edit = () => inspector('Close prices').getByRole('button', { name: label('automation.edit') });
    await waitFor(() => expect(edit()).toBeEnabled());
    fireEvent.click(edit());
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: draft } });
    expect(screen.getByLabelText(label('automation.scriptCron'))).toHaveValue(draft);

    companyRefused = true;
    fireEvent.click(screen.getByRole('button', { name: `${label('automation.nextPage')} ${label('automation.groupCompany')}` }));

    expect(await screen.findByText(label('automation.detailForbidden'))).toBeInTheDocument();
    expect(await screen.findByText(label('automation.companyUnavailable'))).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByDisplayValue(draft)).not.toBeInTheDocument());
    expect(screen.queryByText(draft)).not.toBeInTheDocument();
    expect(screen.queryByText('Close prices')).not.toBeInTheDocument();
    expect(screen.queryByText('Quarterly ledger alpha')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: label('automation.pause') })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: label('automation.edit') })).not.toBeInTheDocument();
    expect(screen.getByText('My morning note')).toBeInTheDocument();
    expect(screen.getByText('Filing watch')).toBeInTheDocument();
    expect(api.getAutomationRead).toHaveBeenCalledWith(id);
  });

  it('clears a company draft after a forbidden save', async () => {
    const taskId = 'data:index-crowding-html-daily';
    let forbidden = false;
    const current = script({ automation_id: taskId, name: 'Close prices', config_version: 3 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      if (forbidden) throw httpError(403, 'restricted');
      return { data: { automations: [current], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async () => {
      if (forbidden) throw httpError(403, 'restricted');
      return { data: current } as never;
    });
    vi.mocked(api.updateCompanyScript).mockImplementation(async () => {
      forbidden = true;
      throw httpError(403, 'restricted');
    });
    renderAt('/automations?view=manage');

    const edit = () => inspector('Close prices').getByRole('button', { name: label('automation.edit') });
    await waitFor(() => expect(edit()).toBeEnabled());
    fireEvent.click(edit());
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: '7 7 * * 7' } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    await waitFor(() => expect(api.updateCompanyScript).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByDisplayValue('7 7 * * 7')).not.toBeInTheDocument());
    expect(await screen.findByText(label('automation.companyUnavailable'))).toBeInTheDocument();
    expect(screen.queryByText('Close prices')).not.toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: label('automation.groupMine') })).getByText(label('automation.scopeEmpty'))).toBeInTheDocument();
  });

  it('clears a company script after a forbidden pause', async () => {
    const taskId = 'data:index-crowding-html-daily';
    let forbidden = false;
    const current = script({ automation_id: taskId, name: 'Close prices', config_version: 3 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      if (forbidden) throw httpError(403, 'restricted');
      return { data: { automations: [current], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async () => {
      if (forbidden) throw httpError(403, 'restricted');
      return { data: current } as never;
    });
    vi.mocked(api.pauseAutomation).mockImplementation(async () => {
      forbidden = true;
      throw httpError(403, 'restricted');
    });
    renderAt('/automations?view=manage');

    const pause = () => inspector('Close prices').getByRole('button', { name: label('automation.pause') });
    await waitFor(() => expect(pause()).toBeEnabled());
    fireEvent.click(pause());

    await waitFor(() => expect(api.pauseAutomation).toHaveBeenCalledWith(taskId, 3));
    await waitFor(() => expect(screen.queryByRole('heading', { level: 2, name: 'Close prices' })).not.toBeInTheDocument());
    expect(await screen.findByText(label('automation.companyUnavailable'))).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: label('automation.pause') })).not.toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: label('automation.groupMine') })).getByText(label('automation.scopeEmpty'))).toBeInTheDocument();
  });
});

describe('cached reads and independent pages', () => {
  async function pastPaneExit() {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
  }

  it('clears a cached company script when its run list is forbidden', async () => {
    let rejectRuns!: (error: Error) => void;
    const task = script({ automation_id: 'data:prices', name: 'Close prices' });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [task], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: task } as never);
    vi.mocked(api.listAutomationExecutionReads).mockImplementation(
      () => new Promise((_resolve, reject) => {
        rejectRuns = reject;
      }),
    );
    renderAt('/automations?view=manage&id=data%3Aprices');
    await waitFor(() => expect(inspector('Close prices').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    await act(async () => {
      rejectRuns(httpError(403, 'restricted'));
    });
    await waitFor(() => {
      expect(screen.queryByRole('heading', { level: 2, name: 'Close prices' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Close prices/ })).not.toBeInTheDocument();
    });
    expect(screen.getByText(label('automation.companyUnavailable'))).toBeInTheDocument();
    expect(screen.queryByText('restricted')).not.toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: label('automation.groupMine') })).getByText(label('automation.scopeEmpty'))).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: label('automation.groupResearch') })).getByText(label('automation.scopeEmpty'))).toBeInTheDocument();
  });

  it('keeps a company script when its run list fails', async () => {
    const task = script({ automation_id: 'data:prices', name: 'Close prices' });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [task], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: task } as never);
    vi.mocked(api.listAutomationExecutionReads).mockRejectedValue(httpError(503, 'unavailable'));
    renderAt('/automations?view=manage&id=data%3Aprices');
    await waitFor(() => expect(inspector('Close prices').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    expect(screen.getByRole('heading', { level: 2, name: 'Close prices' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Close prices/ })).toBeInTheDocument();
  });

  it('clears a cached company agent when its run list is forbidden', async () => {
    let revoked = false;
    const task = agent({ automation_id: 'co-agent', name: 'Desk brief', scope: 'company', config_version: 4 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [task], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: task } as never);
    vi.mocked(api.listExecutions).mockImplementation(async () => {
      if (revoked) throw httpError(403, 'restricted');
      return { data: { executions: [], has_more: false } } as never;
    });
    const { queryClient } = renderAt('/automations?view=manage&id=co-agent');
    await waitFor(() => expect(inspector('Desk brief').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    await act(async () => {
      revoked = true;
      await queryClient.invalidateQueries({ queryKey: queryKeys.automations.executions(task.automation_id) });
    });
    await waitFor(() => {
      expect(screen.queryByRole('heading', { level: 2, name: 'Desk brief' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Desk brief/ })).not.toBeInTheDocument();
    });
    expect(screen.getByText(label('automation.companyUnavailable'))).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: label('automation.groupMine') })).getByText(label('automation.scopeEmpty'))).toBeInTheDocument();
  });

  it('keeps a company draft while a different group changes pages', async () => {
    const task = script({ automation_id: 'data:prices', name: 'Close prices' });
    let finishPage!: (value: never) => void;
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope === 'company') return { data: { automations: [task], total: 1 } } as never;
      if (params?.scope === 'research_tracking') {
        if (params.offset === 50) return new Promise<never>((resolve) => {
          finishPage = resolve;
        });
        return { data: { automations: [agent({ automation_id: 'research-0', name: 'Research first', scope: 'research_tracking' })], total: 51 } } as never;
      }
      return emptyPage();
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: task } as never);
    renderAt('/automations?view=manage');
    await waitFor(() => expect(inspector('Close prices').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    fireEvent.click(inspector('Close prices').getByRole('button', { name: label('automation.edit') }));
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: '7 7 * * 7' } });
    fireEvent.click(screen.getByRole('button', { name: `${label('automation.nextPage')} ${label('automation.groupResearch')}` }));
    await waitFor(() => expect(finishPage).toBeDefined());
    expect(screen.getByLabelText(label('automation.scriptCron'))).toHaveValue('7 7 * * 7');
    await act(async () => {
      finishPage({ data: { automations: [agent({ automation_id: 'research-50', name: 'Research second', scope: 'research_tracking' })], total: 51 } } as never);
    });
    expect(await screen.findByLabelText(label('automation.scriptCron'))).toHaveValue('7 7 * * 7');
    expect(screen.getByRole('button', { name: /Research second/ })).toBeInTheDocument();
  });

  it('asks before paging away from the open company draft', async () => {
    const task = script({ automation_id: 'data:prices', name: 'Close prices' });
    const second = script({ automation_id: 'data:next', name: 'Second company' });
    let pageTwo = false;
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      if (params.offset === 50) {
        pageTwo = true;
        return { data: { automations: [second], total: 51 } } as never;
      }
      return { data: { automations: [task], total: 51 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id) => ({ data: id === second.automation_id ? second : task }) as never);
    renderAt('/automations?view=manage');
    const nextName = `${label('automation.nextPage')} ${label('automation.groupCompany')}`;
    await waitFor(() => expect(inspector('Close prices').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    fireEvent.click(inspector('Close prices').getByRole('button', { name: label('automation.edit') }));
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: '7 7 * * 7' } });
    fireEvent.click(screen.getByRole('button', { name: nextName }));
    const dialog = await screen.findByRole('dialog');
    expect(screen.getByLabelText(label('automation.scriptCron'))).toHaveValue('7 7 * * 7');
    expect(pageTwo).toBe(false);
    fireEvent.click(within(dialog).getByRole('button', { name: label('common.cancel') }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText(label('automation.scriptCron'))).toHaveValue('7 7 * * 7');
    expect(pageTwo).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: nextName }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: label('automation.discardDraft') }));
    expect(await screen.findByRole('button', { name: /Second company/ })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByDisplayValue('7 7 * * 7')).not.toBeInTheDocument());
  });

  it('returns to an existing page when the final page becomes empty', async () => {
    let reduced = false;
    const remaining = agent({ automation_id: 'personal-0', name: 'Remaining personal', scope: 'personal' });
    const last = agent({ automation_id: 'personal-50', name: 'Last personal', scope: 'personal' });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'personal') return emptyPage();
      return { data: { automations: params.offset === 50 ? (reduced ? [] : [last]) : [remaining], total: reduced ? 50 : 51 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id) => ({ data: id === last.automation_id ? last : remaining }) as never);
    const { queryClient } = renderAt('/automations?view=manage');
    fireEvent.click(await screen.findByRole('button', { name: `${label('automation.nextPage')} ${label('automation.groupMine')}` }));
    await screen.findByRole('button', { name: /Last personal/ });
    await act(async () => {
      reduced = true;
      await queryClient.invalidateQueries();
    });
    expect(await screen.findByRole('button', { name: /Remaining personal/ })).toBeInTheDocument();
    expect(screen.queryByText(label('automation.startFrom'))).not.toBeInTheDocument();
  });

  it('keeps an off-page draft when a detail refetch fails', async () => {
    let failed = false;
    const task = script({ automation_id: 'data:offpage', name: 'Off page script' });
    vi.mocked(api.getAutomationRead).mockImplementation(async () => {
      if (failed) throw httpError(503, 'unavailable');
      return { data: task } as never;
    });
    const { queryClient } = renderAt('/automations?view=manage&id=data%3Aoffpage');
    await waitFor(() => expect(inspector('Off page script').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    fireEvent.click(inspector('Off page script').getByRole('button', { name: label('automation.edit') }));
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: '7 7 * * 7' } });
    await act(async () => {
      failed = true;
      await queryClient.invalidateQueries();
    });
    await pastPaneExit();
    expect(screen.getByDisplayValue('7 7 * * 7')).toBeInTheDocument();
    expect(screen.queryByText(label('automation.notFound'))).not.toBeInTheDocument();
  });

  it('keeps the draft when a conflict refetch fails and leaves retry off', async () => {
    const current = script({ automation_id: 'data:prices', name: 'Close prices', config_version: 3 });
    const draft = '7 7 * * 7';
    let detailFails = false;
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [current], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async () => {
      if (detailFails) throw httpError(503, 'unavailable');
      return { data: current } as never;
    });
    vi.mocked(api.updateCompanyScript).mockImplementation(async () => {
      detailFails = true;
      throw httpError(409, 'Someone else saved this task.');
    });
    renderAt('/automations?view=manage');
    await waitFor(() => expect(inspector('Close prices').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    fireEvent.click(inspector('Close prices').getByRole('button', { name: label('automation.edit') }));
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: draft } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));
    await waitFor(() => expect(vi.mocked(api.getAutomationRead).mock.calls.length).toBeGreaterThan(1));
    await pastPaneExit();
    expect(screen.getByLabelText(label('automation.scriptCron'))).toHaveValue(draft);
    expect(screen.getByRole('button', { name: label('automation.retryWithCurrent') })).toBeDisabled();
    expect(api.updateCompanyScript).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(label('automation.notFound'))).not.toBeInTheDocument();
  });

  it('retries a script with the later detail version and the same draft', async () => {
    const taskId = 'data:prices';
    const draft = '7 7 * * 7';
    const retryName = label('automation.retryWithCurrent');
    const listed = script({ automation_id: taskId, name: 'Close prices', config_version: 3, cron_expression: '0 9 * * 1-5' });
    const other = script({ automation_id: 'data:other', name: 'Other book', config_version: 11 });
    let detailMode: 'open' | 'fail' | 'other' | 'fresh' = 'open';
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [listed, other], total: 2 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      if (detailMode === 'fail') throw httpError(503, 'unavailable');
      if (detailMode === 'other') return { data: other } as never;
      if (id !== taskId) throw httpError(404, 'missing');
      const version = detailMode === 'fresh' ? 8 : 3;
      return { data: { ...listed, config_version: version } } as never;
    });
    vi.mocked(api.updateCompanyScript).mockImplementation(async () => {
      detailMode = 'fail';
      throw httpError(409, 'Someone else saved this task.');
    });
    const { queryClient } = renderAt('/automations?view=manage');
    const cron = () => screen.getByLabelText(label('automation.scriptCron'));
    const retry = () => screen.getByRole('button', { name: retryName });

    await waitFor(() => expect(inspector('Close prices').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    fireEvent.click(inspector('Close prices').getByRole('button', { name: label('automation.edit') }));
    fireEvent.change(await screen.findByLabelText(label('automation.scriptCron')), { target: { value: draft } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    await waitFor(() => {
      expect(retry()).toBeDisabled();
      expect(queryClient.isFetching({ queryKey: queryKeys.automations.all })).toBe(0);
    });
    expect(cron()).toHaveValue(draft);
    expect(api.updateCompanyScript).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.updateCompanyScript).mock.calls[0]).toEqual([
      taskId,
      { expected_config_version: 3, cron_expression: draft },
    ]);

    await act(async () => {
      detailMode = 'other';
      await queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
    });
    await waitFor(() => expect(queryClient.isFetching({ queryKey: queryKeys.automations.all })).toBe(0));
    expect(retry()).toBeDisabled();
    expect(cron()).toHaveValue(draft);
    expect(api.updateCompanyScript).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(label('automation.currentVersion', { version: 11 }))).not.toBeInTheDocument();
    expect(screen.queryByText(label('automation.notFound'))).not.toBeInTheDocument();

    await act(async () => {
      detailMode = 'fresh';
      await queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
    });
    await waitFor(() => expect(retry()).toBeEnabled());
    expect(api.updateCompanyScript).toHaveBeenCalledTimes(1);
    expect(cron()).toHaveValue(draft);
    expect(screen.getByText(label('automation.currentVersion', { version: 8 }))).toBeInTheDocument();

    fireEvent.click(retry());
    await waitFor(() => expect(api.updateCompanyScript).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.updateCompanyScript).mock.calls[1]).toEqual([
      taskId,
      { expected_config_version: 8, cron_expression: draft },
    ]);
  });

  it('retries a company agent with the later detail version and the same draft', async () => {
    const taskId = 'co-1';
    const draft = 'Desk hours';
    const retryName = label('automation.retryWithCurrent');
    const listed = agent({ automation_id: taskId, name: 'Desk', scope: 'company', config_version: 2 });
    const other = agent({ automation_id: 'co-2', name: 'Other desk', scope: 'company', config_version: 11 });
    let detailMode: 'open' | 'fail' | 'other' | 'fresh' = 'open';
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [listed, other], total: 2 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      if (detailMode === 'fail') throw httpError(503, 'unavailable');
      if (detailMode === 'other') return { data: other } as never;
      if (id !== taskId) throw httpError(404, 'missing');
      const version = detailMode === 'fresh' ? 9 : 7;
      return { data: { ...listed, config_version: version } } as never;
    });
    vi.mocked(api.updateAutomation).mockImplementation(async () => {
      detailMode = 'fail';
      throw httpError(409, 'Someone else saved this task.');
    });
    const { queryClient } = renderAt('/automations?view=manage');
    const name = () => screen.getByLabelText(label('common.name'));
    const retry = () => screen.getByRole('button', { name: retryName });

    await waitFor(() => expect(inspector('Desk').getByRole('button', { name: label('automation.edit') })).toBeEnabled());
    fireEvent.click(inspector('Desk').getByRole('button', { name: label('automation.edit') }));
    fireEvent.change(await screen.findByLabelText(label('common.name')), { target: { value: draft } });
    fireEvent.click(screen.getByRole('button', { name: label('automation.saveChanges') }));

    await waitFor(() => {
      expect(retry()).toBeDisabled();
      expect(queryClient.isFetching({ queryKey: queryKeys.automations.all })).toBe(0);
    });
    expect(name()).toHaveValue(draft);
    expect(api.updateAutomation).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.updateAutomation).mock.calls[0][0]).toBe(taskId);
    expect(vi.mocked(api.updateAutomation).mock.calls[0][1]).toMatchObject({
      name: draft,
      expected_config_version: 7,
    });
    expect(vi.mocked(api.updateAutomation).mock.calls[0][1]).not.toHaveProperty('delivery_config');

    await act(async () => {
      detailMode = 'other';
      await queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
    });
    await waitFor(() => expect(queryClient.isFetching({ queryKey: queryKeys.automations.all })).toBe(0));
    expect(retry()).toBeDisabled();
    expect(name()).toHaveValue(draft);
    expect(api.updateAutomation).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(label('automation.currentVersion', { version: 11 }))).not.toBeInTheDocument();
    expect(screen.queryByText(label('automation.notFound'))).not.toBeInTheDocument();

    await act(async () => {
      detailMode = 'fresh';
      await queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
    });
    await waitFor(() => expect(retry()).toBeEnabled());
    expect(api.updateAutomation).toHaveBeenCalledTimes(1);
    expect(name()).toHaveValue(draft);
    expect(screen.getByText(label('automation.currentVersion', { version: 9 }))).toBeInTheDocument();

    fireEvent.click(retry());
    await waitFor(() => expect(api.updateAutomation).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.updateAutomation).mock.calls[1][0]).toBe(taskId);
    expect(vi.mocked(api.updateAutomation).mock.calls[1][1]).toMatchObject({
      name: draft,
      expected_config_version: 9,
    });
    expect(vi.mocked(api.updateAutomation).mock.calls[1][1]).not.toHaveProperty('delivery_config');
    expect(vi.mocked(api.updateAutomation).mock.calls[1][1]).not.toHaveProperty('config_version');
  });

  it('keeps previous-page navigation after the next page fails', async () => {
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'research_tracking') return emptyPage();
      if (params.offset === 50) throw httpError(503, 'unavailable');
      return { data: { automations: [agent({ automation_id: 'research-first', name: 'Research first', scope: 'research_tracking' })], total: 76 } } as never;
    });
    renderAt('/automations?view=manage');
    const research = () => screen.getByRole('region', { name: label('automation.groupResearch') });
    fireEvent.click(await screen.findByRole('button', { name: `${label('automation.nextPage')} ${label('automation.groupResearch')}` }));
    await waitFor(() => expect(within(research()).getByText(label('automation.scopeLoadFailed'))).toBeInTheDocument());
    expect(within(research()).getByRole('button', { name: `${label('automation.prevPage')} ${label('automation.groupResearch')}` })).toBeEnabled();
    expect(within(research()).getByRole('button', { name: `${label('automation.nextPage')} ${label('automation.groupResearch')}` })).toBeDisabled();
    expect(within(research()).queryByText(/Page /)).not.toBeInTheDocument();
    fireEvent.click(within(research()).getByRole('button', { name: `${label('automation.prevPage')} ${label('automation.groupResearch')}` }));
    expect(await within(research()).findByRole('button', { name: /Research first/ })).toBeInTheDocument();
  });
});

describe('the feed and a personal task', () => {
  it('keeps the feed on its own reads', async () => {
    const excerpt = 'Jobs report moved stocks.';
    vi.mocked(api.listAutomations).mockResolvedValue({
      data: { automations: [agent({ automation_id: 'auto-1', name: 'Morning briefing', scope: 'personal' })], total: 1 },
    } as never);
    vi.mocked(api.listRecentRuns).mockResolvedValue({
      data: {
        executions: [
          {
            automation_execution_id: 'exec-1',
            automation_id: 'auto-1',
            automation_name: 'Morning briefing',
            trigger_type: 'cron',
            workspace_id: 'home-1',
            status: 'completed',
            conversation_thread_id: null,
            scheduled_at: '2026-11-01T13:00:00Z',
            started_at: '2026-11-01T13:00:00Z',
            completed_at: '2026-11-01T13:02:00Z',
            error_message: null,
            skip_reason: null,
            failure_reason: null,
            delivery_result: null,
            created_at: '2026-11-01T13:00:00Z',
            excerpt,
            dismissed_at: null,
          } satisfies AutomationRun,
        ],
        has_more: false,
      },
    } as never);
    renderAt('/automations?view=feed');

    expect(await screen.findByText(excerpt)).toBeInTheDocument();
    expect(api.listAutomationReads).not.toHaveBeenCalled();
  });

  it('still starts a personal automation from a blank template', async () => {
    renderAt('/automations');
    fireEvent.click(await screen.findByText(label('automation.blankAutomation')));

    expect(await screen.findByLabelText(label('common.name'))).toBeInTheDocument();
    expect(screen.getByLabelText(label('automation.instruction'))).toBeInTheDocument();
    expect(screen.queryByLabelText(label('automation.scriptCron'))).not.toBeInTheDocument();
  });

  it('opens the selected task in the phone sheet', async () => {
    mobile.on = true;
    const row = agent({ automation_id: 'phone-1', name: 'Phone desk', scope: 'company', config_version: 4 });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== 'company') return emptyPage();
      return { data: { automations: [row], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: row } as never);
    renderAt('/automations?id=phone-1');

    const sheet = await waitFor(() => {
      const node = document.querySelector('[data-sheet]');
      expect(node).toBeTruthy();
      return node as HTMLElement;
    });
    expect(within(sheet).getByRole('heading', { level: 2, name: 'Phone desk' })).toBeInTheDocument();
  });
});
