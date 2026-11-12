// DALTON-HOOK(company-automations): modification history, rollback, and the recycle bin.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { useLocation } from 'react-router';
import { toast } from '@/components/ui/use-toast';
import i18n from '@/i18n';
import { queryKeys } from '@/lib/queryKeys';
import { renderWithProviders } from '@/test/utils';
import type { AgentAutomationRead, AutomationHistory, AutomationHistoryEntry, AutomationRead, ScriptAutomationRead } from '@/types/automation';
import * as api from '../utils/api';
import Automations from '../Automations';

const auth = vi.hoisted(() => ({ userId: 'user-1' }));
const home = vi.hoisted(() => ({ getHomeWorkspace: vi.fn() }));
const credentials = vi.hoisted(() => ({ callerTier: null as string | null, callerTierPending: false }));

vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => false }));
vi.mock('@/hooks/useHomeTimezone', () => ({ useHomeTimezone: () => 'America/New_York' }));
vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [] }, isPending: false, isLoading: false }),
}));
vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));
vi.mock('@/pages/ChatAgent/utils/api/workspaces', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getHomeWorkspace: home.getHomeWorkspace,
}));
vi.mock('@/hooks/useTeamCredentials', () => ({
  useTeamCredentials: () => ({
    isShared: false,
    canDisconnect: true,
    canManage: true,
    isPm: true,
    canInvite: false,
    callerTier: credentials.callerTier,
    callerTierPending: credentials.callerTierPending,
  }),
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
    listAutomationHistory: vi.fn(),
    getAutomationRead: vi.fn(),
    getDeliveryOptions: vi.fn(),
    deleteAutomation: vi.fn(),
    restoreAutomation: vi.fn(),
    rollbackAutomation: vi.fn(),
  };
});

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
    workspace_id: null,
    llm_model: null,
    additional_context: null,
    thread_strategy: 'new',
    conversation_thread_id: null,
    status: 'active',
    max_failures: 3,
    failure_count: 0,
    delivery_config: null,
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
    last_run_at: null,
    updated_by: null,
    dalton_notify_category: null,
    dalton_notify_effective: null,
    dalton_notify_source: null,
    dalton_notify_route_targets: null,
    last_execution: null,
    ...over,
  };
}

function entry(over: Partial<AutomationHistoryEntry> & Pick<AutomationHistoryEntry, 'version' | 'action'>): AutomationHistoryEntry {
  return {
    prev_version: null,
    restored_from_version: null,
    actor_user_id: 'user-9',
    changed_at: '2026-11-04T15:00:00Z',
    changes: {},
    snapshot: {},
    ...over,
  };
}

function historyFor(id: string, active: number, config: number, entries: AutomationHistoryEntry[]): AutomationHistory {
  return {
    automation_id: id,
    scope: 'company',
    engine_kind: 'agent',
    config_version: config,
    active_version: active,
    entries,
    total: entries.length,
    limit: 50,
    offset: 0,
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

/** The version rows only. A change line is a nested list item and is not a version. */
function versionRows(list: HTMLElement): HTMLElement[] {
  return [...list.querySelectorAll<HTMLElement>(':scope > li')];
}

function LocationProbe() {
  const { search } = useLocation();
  return <output data-testid="search">{search}</output>;
}

beforeEach(() => {
  auth.userId = 'user-1';
  credentials.callerTier = null;
  credentials.callerTierPending = false;
  localStorage.clear();
  home.getHomeWorkspace.mockResolvedValue({ workspace_id: 'home-1', is_home: true });
  vi.mocked(api.listAutomations).mockResolvedValue({ data: { automations: [], total: 0 } } as never);
  vi.mocked(api.listExecutions).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listRecentRuns).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listAutomationExecutionReads).mockResolvedValue({ data: { executions: [], has_more: false } } as never);
  vi.mocked(api.listAutomationReads).mockResolvedValue(emptyPage());
  vi.mocked(api.listAutomationHistory).mockResolvedValue({
    data: historyFor('none', 1, 1, []),
  } as never);
  vi.mocked(api.getAutomationRead).mockRejectedValue(httpError(404, 'missing'));
  vi.mocked(api.getDeliveryOptions).mockResolvedValue({ data: { enabled: false, apps: {}, workspace_id: null } } as never);
  vi.mocked(api.deleteAutomation).mockResolvedValue({ data: {} } as never);
  vi.mocked(api.restoreAutomation).mockResolvedValue({ data: {} } as never);
  vi.mocked(api.rollbackAutomation).mockResolvedValue({ data: {} } as never);
  vi.mocked(toast).mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('modification history and rollback', () => {
  const desk = agent({ automation_id: 'co-agent', name: 'Desk brief', scope: 'company', config_version: 6, active_version: 4 });
  const versions = historyFor('co-agent', 4, 6, [
    entry({
      version: 2,
      action: 'create',
      prev_version: null,
      snapshot: { name: 'Desk brief' },
    }),
    entry({
      version: 4,
      action: 'update',
      prev_version: 3,
      changes: { cron_expression: { old: '0 6 * * *', new: '0 7 * * *' } },
      snapshot: { cron_expression: '0 7 * * *' },
    }),
    entry({
      version: 3,
      action: 'rollback',
      prev_version: 2,
      snapshot: { cron_expression: '0 6 * * *', name: 'Desk brief' },
    }),
  ]);

  function serve() {
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted || params?.scope !== 'company') return emptyPage();
      return { data: { automations: [desk], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: desk } as never);
    vi.mocked(api.listAutomationHistory).mockResolvedValue({ data: versions } as never);
  }

  it('lists versions newest first and marks the one in effect', async () => {
    serve();
    renderAt('/automations?view=manage');
    fireEvent.click(await screen.findByRole('button', { name: /Desk brief/ }));
    const pane = inspector('Desk brief');
    fireEvent.click(await pane.findByRole('tab', { name: label('automation.modificationHistory') }));
    const list = await pane.findByRole('list', { name: label('automation.modificationHistory') });
    const items = versionRows(list);
    expect(items.map((item) => item.textContent?.match(/v\d+/)?.[0])).toEqual(['v4', 'v3', 'v2']);
    expect(items[0]).toHaveTextContent(label('automation.historyCurrent'));
    expect(items[1]).not.toHaveTextContent(label('automation.historyCurrent'));
    expect(items[2]).not.toHaveTextContent(label('automation.historyCurrent'));
    expect(items[0]).toHaveTextContent(label('automation.actionUpdate'));
    expect(items[0]).toHaveTextContent('user-9');
    expect(items[0]).toHaveTextContent(
      label('automation.historyChange', { field: label('automation.fieldCron'), old: '0 6 * * *', next: '0 7 * * *' }),
    );
    expect(items[1]).toHaveTextContent(label('automation.actionRollback'));
    expect(items[2]).toHaveTextContent(label('automation.actionCreate'));
    expect(pane.getAllByRole('button', { name: label('automation.rollbackButton', { version: 3 }) }).length).toBeGreaterThan(0);
  });

  it('rolls back to the previous version and refreshes after a conflict', async () => {
    serve();
    renderAt('/automations?view=manage&id=co-agent');
    expect(await screen.findByRole('heading', { level: 2, name: 'Desk brief' })).toBeInTheDocument();
    const pane = inspector('Desk brief');
    const rollback = () => pane.getAllByRole('button', { name: label('automation.rollbackButton', { version: 3 }) })[0];
    fireEvent.click(await waitFor(() => rollback()));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('list')).toHaveTextContent('0 6 * * *');
    expect(within(dialog).getByText(label('automation.rollbackConfirm', { version: 3 }))).toBeInTheDocument();
    const historyCalls = vi.mocked(api.listAutomationHistory).mock.calls.length;
    const detailCalls = vi.mocked(api.getAutomationRead).mock.calls.length;
    fireEvent.click(within(dialog).getByRole('button', { name: label('automation.rollbackSubmit') }));
    await waitFor(() => expect(api.rollbackAutomation).toHaveBeenCalledWith('co-agent', 3, 6));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(toast).not.toHaveBeenCalled();

    vi.mocked(api.rollbackAutomation).mockRejectedValueOnce(httpError(409, 'stale version'));
    fireEvent.click(rollback());
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: label('automation.rollbackSubmit') }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ description: label('automation.versionConflict') })),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(vi.mocked(api.listAutomationHistory).mock.calls.length).toBeGreaterThan(historyCalls));
    await waitFor(() => expect(vi.mocked(api.getAutomationRead).mock.calls.length).toBeGreaterThan(detailCalls));
  });

  it('shows a script modification history and rolls that script back', async () => {
    const prices = script({ automation_id: 'data:prices', name: 'Close prices', config_version: 5, active_version: 5 });
    const page = historyFor('data:prices', 5, 5, [
      entry({ version: 5, action: 'update', prev_version: 4, snapshot: { cron_expression: '0 9 * * 1-5' } }),
      entry({ version: 4, action: 'create', prev_version: null, snapshot: { cron_expression: '0 8 * * 1-5' } }),
    ]);
    page.engine_kind = 'script';
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted || params?.scope !== 'company') return emptyPage();
      return { data: { automations: [prices], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: prices } as never);
    vi.mocked(api.listAutomationHistory).mockResolvedValue({ data: page } as never);
    renderAt('/automations?view=manage');
    expect(await screen.findByRole('heading', { level: 2, name: 'Close prices' })).toBeInTheDocument();
    const pane = inspector('Close prices');
    fireEvent.click(await pane.findByRole('tab', { name: label('automation.modificationHistory') }));
    const list = await pane.findByRole('list', { name: label('automation.modificationHistory') });
    const items = within(list).getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('v5');
    expect(items[0]).toHaveTextContent(label('automation.historyCurrent'));
    fireEvent.click(pane.getAllByRole('button', { name: label('automation.rollbackButton', { version: 4 }) })[0]);
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: label('automation.rollbackSubmit') }));
    await waitFor(() => expect(api.rollbackAutomation).toHaveBeenCalledWith('data:prices', 4, 5));
  });

  it('translates pause, maintenance pause, and seed, and keeps an unknown action', async () => {
    const desk = agent({ automation_id: 'co-agent', name: 'Desk brief', scope: 'company', config_version: 8, active_version: 8 });
    const page = historyFor('co-agent', 8, 8, [
      entry({ version: 8, action: 'pause', prev_version: 7 }),
      entry({ version: 7, action: 'maintenance_pause_stale_binding', prev_version: 6 }),
      entry({ version: 6, action: 'seed', prev_version: 5 }),
      entry({ version: 5, action: 'custom_action', prev_version: null }),
    ]);
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted || params?.scope !== 'company') return emptyPage();
      return { data: { automations: [desk], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: desk } as never);
    vi.mocked(api.listAutomationHistory).mockResolvedValue({ data: page } as never);
    renderAt('/automations?view=manage&id=co-agent');
    expect(await screen.findByRole('heading', { level: 2, name: 'Desk brief' })).toBeInTheDocument();
    const pane = inspector('Desk brief');
    fireEvent.click(await pane.findByRole('tab', { name: label('automation.modificationHistory') }));
    const list = await pane.findByRole('list', { name: label('automation.modificationHistory') });
    const items = versionRows(list);
    expect(items[0]).toHaveTextContent(label('automation.actionPause'));
    expect(items[1]).toHaveTextContent(label('automation.actionMaintenancePause'));
    expect(items[2]).toHaveTextContent(label('automation.actionSeed'));
    expect(items[3]).toHaveTextContent('custom_action');
  });

  it('keeps rollback disabled until the target snapshot loads from a later page', async () => {
    const desk = agent({ automation_id: 'co-agent', name: 'Desk brief', scope: 'company', config_version: 6, active_version: 80 });
    const first = historyFor('co-agent', 80, 6, [
      entry({ version: 80, action: 'update', prev_version: 3, snapshot: { cron_expression: '0 7 * * *' } }),
    ]);
    first.total = 80;
    let releaseLater!: () => void;
    const later = new Promise<void>((resolve) => {
      releaseLater = resolve;
    });
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted || params?.scope !== 'company') return emptyPage();
      return { data: { automations: [desk], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: desk } as never);
    vi.mocked(api.listAutomationHistory).mockImplementation(async (_id, params) => {
      const offset = params?.offset ?? 0;
      if (offset === 0) return { data: first } as never;
      await later;
      return {
        data: {
          ...first,
          offset,
          limit: params?.limit ?? 200,
          entries: [entry({ version: 3, action: 'update', prev_version: 2, snapshot: { cron_expression: '0 6 * * *' } })],
        },
      } as never;
    });
    renderAt('/automations?view=manage&id=co-agent');
    expect(await screen.findByRole('heading', { level: 2, name: 'Desk brief' })).toBeInTheDocument();
    const pane = inspector('Desk brief');
    const rollback = () => pane.getAllByRole('button', { name: label('automation.rollbackButton', { version: 3 }) })[0];
    fireEvent.click(await waitFor(() => rollback()));
    const dialog = await screen.findByRole('dialog');
    const submit = () => within(dialog).getByRole('button', { name: label('automation.rollbackSubmit') });
    expect(submit()).toBeDisabled();
    expect(within(dialog).getByText(label('automation.rollbackSnapshotLoading'))).toBeInTheDocument();
    expect(within(dialog).queryByText('0 6 * * *')).not.toBeInTheDocument();
    releaseLater();
    await waitFor(() => expect(submit()).toBeEnabled());
    expect(within(dialog).getByRole('list')).toHaveTextContent('0 6 * * *');
    fireEvent.click(submit());
    await waitFor(() => expect(api.rollbackAutomation).toHaveBeenCalledWith('co-agent', 3, 6));
  });

  it('keeps rollback disabled when the target version is not in the loaded history', async () => {
    const desk = agent({ automation_id: 'co-agent', name: 'Desk brief', scope: 'company', config_version: 6, active_version: 4 });
    const page = historyFor('co-agent', 4, 6, [
      entry({ version: 4, action: 'update', prev_version: 1, snapshot: { name: 'Desk brief' } }),
    ]);
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted || params?.scope !== 'company') return emptyPage();
      return { data: { automations: [desk], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: desk } as never);
    vi.mocked(api.listAutomationHistory).mockResolvedValue({ data: page } as never);
    renderAt('/automations?view=manage&id=co-agent');
    expect(await screen.findByRole('heading', { level: 2, name: 'Desk brief' })).toBeInTheDocument();
    const pane = inspector('Desk brief');
    fireEvent.click(await waitFor(() => pane.getAllByRole('button', { name: label('automation.rollbackButton', { version: 1 }) })[0]));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('button', { name: label('automation.rollbackSubmit') })).toBeDisabled();
    expect(within(dialog).getByText(label('automation.rollbackSnapshotMissing', { version: 1 }))).toBeInTheDocument();
    expect(api.rollbackAutomation).not.toHaveBeenCalled();
  });

  it('confirms a rollback when the loaded snapshot has no summary fields', async () => {
    const desk = agent({ automation_id: 'co-agent', name: 'Desk brief', scope: 'company', config_version: 6, active_version: 4 });
    const page = historyFor('co-agent', 4, 6, [
      entry({ version: 4, action: 'update', prev_version: 3, snapshot: { name: 'Desk brief' } }),
      entry({ version: 3, action: 'create', prev_version: null, snapshot: {} }),
    ]);
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted || params?.scope !== 'company') return emptyPage();
      return { data: { automations: [desk], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: desk } as never);
    vi.mocked(api.listAutomationHistory).mockResolvedValue({ data: page } as never);
    renderAt('/automations?view=manage&id=co-agent');
    expect(await screen.findByRole('heading', { level: 2, name: 'Desk brief' })).toBeInTheDocument();
    const pane = inspector('Desk brief');
    fireEvent.click(await waitFor(() => pane.getAllByRole('button', { name: label('automation.rollbackButton', { version: 3 }) })[0]));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(label('automation.rollbackVersionOnly', { version: 3 }))).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: label('automation.rollbackSubmit') })).toBeEnabled();
  });
});

describe('recycle bin', () => {
  const live = agent({ automation_id: 'me-1', name: 'Morning note', scope: 'personal', config_version: 4 });
  const deleted = agent({
    automation_id: 'me-old',
    name: 'Old morning',
    scope: 'personal',
    config_version: 8,
    deleted_at: '2026-11-05T12:00:00Z',
    deleted_by: 'user-2',
  });
  const prices = script({ automation_id: 'data:prices', name: 'Close prices', config_version: 5 });

  function serve() {
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted) {
        if (params.scope !== 'personal') return emptyPage();
        return { data: { automations: [deleted], total: 1 } } as never;
      }
      if (params?.scope === 'personal') return { data: { automations: [live], total: 1 } } as never;
      if (params?.scope === 'company') return { data: { automations: [prices], total: 1 } } as never;
      return emptyPage();
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      if (id === live.automation_id) return { data: live } as never;
      if (id === prices.automation_id) return { data: prices } as never;
      throw httpError(404, 'missing');
    });
  }

  it('moves a task into the recycle bin with its current version', async () => {
    serve();
    renderAt('/automations?view=manage');
    fireEvent.click(await screen.findByRole('button', { name: /Close prices/ }));
    const pane = inspector('Close prices');
    fireEvent.click(await pane.findByRole('button', { name: label('automation.moveToRecycle') }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(label('automation.recycleConfirm', { name: 'Close prices' }))).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: label('automation.moveToRecycle') }));
    await waitFor(() => expect(api.deleteAutomation).toHaveBeenCalledWith('data:prices', 5));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('closes the confirm and refreshes when a delete conflicts', async () => {
    serve();
    vi.mocked(api.deleteAutomation).mockRejectedValueOnce(httpError(409, 'stale version'));
    renderAt('/automations?view=manage&id=me-1');
    expect(await screen.findByRole('heading', { level: 2, name: 'Morning note' })).toBeInTheDocument();
    const pane = inspector('Morning note');
    const detailCalls = vi.mocked(api.getAutomationRead).mock.calls.length;
    fireEvent.click(await pane.findByRole('button', { name: label('automation.moveToRecycle') }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: label('automation.moveToRecycle') }));
    await waitFor(() => expect(api.deleteAutomation).toHaveBeenCalledWith('me-1', 4));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ description: label('automation.versionConflict') })),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(vi.mocked(api.getAutomationRead).mock.calls.length).toBeGreaterThan(detailCalls));
  });

  it('lists only deleted rows from include_deleted and offers restore alone', async () => {
    serve();
    renderAt('/automations?view=manage');
    const personal = await screen.findByRole('region', { name: label('automation.groupMine') });
    const toggle = await within(personal).findByRole('button', { name: label('automation.recycleBin', { total: 1 }) });
    expect(api.listAutomationReads).toHaveBeenCalledWith({
      scope: 'personal',
      includeDeleted: true,
      limit: 50,
      offset: 0,
    });
    expect(api.listAutomationReads).toHaveBeenCalledWith({ scope: 'personal', limit: 50, offset: 0 });
    fireEvent.click(toggle);
    expect(await within(personal).findByRole('button', { name: /Old morning/ })).toBeInTheDocument();
    expect(within(personal).getByText(/user-2/)).toBeInTheDocument();
    expect(within(personal).queryByRole('button', { name: /Morning note/ })).not.toBeInTheDocument();
    fireEvent.click(within(personal).getByRole('button', { name: /Old morning/ }));
    const pane = inspector('Old morning');
    expect(pane.getByRole('button', { name: label('automation.restoreTask') })).toBeEnabled();
    expect(pane.queryByRole('button', { name: label('automation.runNow') })).not.toBeInTheDocument();
    expect(pane.queryByRole('button', { name: label('automation.edit') })).not.toBeInTheDocument();
    expect(pane.queryByRole('button', { name: label('automation.pause') })).not.toBeInTheDocument();
    expect(pane.queryByRole('button', { name: label('automation.moveToRecycle') })).not.toBeInTheDocument();
    expect(pane.queryByRole('button', { name: label('automation.rollbackButton', { version: 1 }) })).not.toBeInTheDocument();
  });

  it('restores a deleted task and leaves the bin after a conflict still open', async () => {
    serve();
    renderAt('/automations?view=manage');
    const personal = await screen.findByRole('region', { name: label('automation.groupMine') });
    fireEvent.click(await within(personal).findByRole('button', { name: label('automation.recycleBin', { total: 1 }) }));
    fireEvent.click(await within(personal).findByRole('button', { name: /Old morning/ }));
    fireEvent.click(inspector('Old morning').getByRole('button', { name: label('automation.restoreTask') }));
    await waitFor(() => expect(api.restoreAutomation).toHaveBeenCalledWith('me-old', 8));
    await waitFor(() => expect(within(personal).queryByRole('button', { name: /Old morning/ })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByRole('heading', { level: 2, name: 'Old morning' })).not.toBeInTheDocument());
    expect(within(personal).getByRole('button', { name: label('automation.recycleBin', { total: 1 }) })).toHaveAttribute(
      'aria-pressed',
      'false',
    );

    fireEvent.click(within(personal).getByRole('button', { name: label('automation.recycleBin', { total: 1 }) }));
    fireEvent.click(await within(personal).findByRole('button', { name: /Old morning/ }));
    vi.mocked(api.restoreAutomation).mockRejectedValueOnce(httpError(409, 'stale version'));
    const reads = vi.mocked(api.listAutomationReads).mock.calls.length;
    fireEvent.click(inspector('Old morning').getByRole('button', { name: label('automation.restoreTask') }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ description: label('automation.versionConflict') })),
    );
    expect(inspector('Old morning').getByRole('button', { name: label('automation.restoreTask') })).toBeInTheDocument();
    expect(within(personal).getByRole('button', { name: label('automation.recycleBin', { total: 1 }) })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await waitFor(() => expect(vi.mocked(api.listAutomationReads).mock.calls.length).toBeGreaterThan(reads));
  });
});

describe('scope in the URL', () => {
  const desk = agent({ automation_id: 'co-agent', name: 'Desk brief', scope: 'company', config_version: 2 });

  it('selects the company group from ?scope=company even when the stored view is the feed', async () => {
    localStorage.setItem('automations:view', 'feed');
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.includeDeleted || params?.scope !== 'company') return emptyPage();
      return { data: { automations: [desk], total: 1 } } as never;
    });
    vi.mocked(api.getAutomationRead).mockResolvedValue({ data: desk } as never);
    renderAt('/automations?scope=company');
    const company = await screen.findByRole('region', { name: label('automation.groupCompany') });
    expect(company).toHaveAttribute('aria-current', 'true');
    expect(screen.getByRole('region', { name: label('automation.groupMine') })).not.toHaveAttribute('aria-current');
    expect(await screen.findByRole('heading', { level: 2, name: 'Desk brief' })).toBeInTheDocument();
  });

  it('falls back to personal when an external member opens the company group', async () => {
    const secret = 'Quarterly ledger alpha';
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope === 'company') throw httpError(403, secret);
      if (params?.scope === 'personal' && !params.includeDeleted) {
        return { data: { automations: [agent({ automation_id: 'me-1', name: 'Morning note', scope: 'personal' })], total: 1 } } as never;
      }
      return emptyPage();
    });
    renderWithProviders(
      <>
        <Automations />
        <LocationProbe />
      </>,
      { route: '/automations?scope=company' },
    );
    const personal = await screen.findByRole('region', { name: label('automation.groupMine') });
    await waitFor(() => expect(personal).toHaveAttribute('aria-current', 'true'));
    expect(screen.queryByRole('region', { name: label('automation.groupCompany') })).not.toBeInTheDocument();
    expect(screen.queryByText(label('automation.companyUnavailable'))).not.toBeInTheDocument();
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('search').textContent).toContain('scope=personal'));
    expect(screen.getByTestId('search').textContent).toContain('view=manage');
  });

  it('does not request the company list or its recycle bin for an external member', async () => {
    credentials.callerTier = 'external';
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope === 'company') throw new Error('company read');
      if (params?.scope === 'personal' && !params.includeDeleted) {
        return { data: { automations: [agent({ automation_id: 'me-1', name: 'Morning note', scope: 'personal' })], total: 1 } } as never;
      }
      return emptyPage();
    });
    renderAt('/automations?view=manage');
    expect(await screen.findByRole('button', { name: /Morning note/ })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: label('automation.groupCompany') })).not.toBeInTheDocument();
    expect(api.listAutomationReads).not.toHaveBeenCalledWith(expect.objectContaining({ scope: 'company' }));
    expect(api.listAutomationReads).toHaveBeenCalledWith(
      expect.objectContaining({ scope: 'personal', includeDeleted: true, limit: 50, offset: 0 }),
    );
  });

  it('does not request company tasks while the caller tier is still loading', async () => {
    credentials.callerTierPending = true;
    renderAt('/automations?view=manage');
    await waitFor(() => expect(api.listAutomationReads).toHaveBeenCalledWith(expect.objectContaining({ scope: 'personal' })));
    expect(api.listAutomationReads).not.toHaveBeenCalledWith(expect.objectContaining({ scope: 'company' }));
    expect(screen.queryByRole('region', { name: label('automation.groupCompany') })).not.toBeInTheDocument();
  });
});

describe('selecting the same task from the recycle bin', () => {
  function stickyClient() {
    return new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 60 * 60_000 },
        mutations: { retry: false },
      },
    });
  }

  function installMutable(scope: 'personal' | 'company', liveRow: AutomationRead) {
    const state = { live: [liveRow], deleted: [] as AutomationRead[] };
    vi.mocked(api.listAutomationReads).mockImplementation(async (params) => {
      if (params?.scope !== scope) return emptyPage();
      const rows = params.includeDeleted ? state.deleted : state.live;
      return { data: { automations: rows, total: rows.length } } as never;
    });
    vi.mocked(api.getAutomationRead).mockImplementation(async (id: string) => {
      if (state.deleted.some((task) => task.automation_id === id)) throw httpError(404, 'missing');
      const found = state.live.find((task) => task.automation_id === id);
      if (!found) throw httpError(404, 'missing');
      return { data: found } as never;
    });
    vi.mocked(api.deleteAutomation).mockImplementation(async (id: string) => {
      const index = state.live.findIndex((task) => task.automation_id === id);
      if (index >= 0) {
        const [row] = state.live.splice(index, 1);
        state.deleted.unshift({ ...row, deleted_at: '2026-11-05T12:00:00Z', deleted_by: 'user-2' });
      }
      return { data: {} } as never;
    });
    vi.mocked(api.restoreAutomation).mockImplementation(async (id: string) => {
      const index = state.deleted.findIndex((task) => task.automation_id === id);
      if (index >= 0) {
        const [row] = state.deleted.splice(index, 1);
        state.live.unshift({ ...row, deleted_at: null, deleted_by: null });
      }
      return { data: {} } as never;
    });
  }

  async function deleteThenRestore(scope: 'personal' | 'company', liveRow: AutomationRead, groupKey: string, version: number) {
    const id = liveRow.automation_id;
    const name = liveRow.name;
    installMutable(scope, liveRow);
    const queryClient = stickyClient();
    renderWithProviders(<Automations />, { route: '/automations?view=manage', queryClient });
    const region = () => screen.getByRole('region', { name: label(groupKey) });
    const bin = (total: number) => within(region()).getByRole('button', { name: label('automation.recycleBin', { total }) });

    fireEvent.click(await within(await screen.findByRole('region', { name: label(groupKey) })).findByRole('button', { name: label('automation.recycleBin', { total: 0 }) }));
    expect(within(region()).getByText(label('automation.recycleEmpty'))).toBeInTheDocument();
    expect(within(region()).queryByText(label('automation.scopeEmpty'))).not.toBeInTheDocument();
    fireEvent.click(bin(0));

    fireEvent.click(await within(region()).findByRole('button', { name: new RegExp(name) }));
    expect(await screen.findByRole('heading', { level: 2, name })).toBeInTheDocument();
    expect(inspector(name).getByRole('button', { name: label('automation.edit') })).toBeInTheDocument();
    expect(inspector(name).queryByRole('button', { name: label('automation.restoreTask') })).not.toBeInTheDocument();
    if (scope === 'personal') {
      expect(inspector(name).getByRole('button', { name: label('automation.runNow') })).toBeInTheDocument();
    }
    await waitFor(() =>
      expect(queryClient.getQueryData(queryKeys.automations.readDetail('user-1', id))).toMatchObject({ automation_id: id }),
    );

    fireEvent.click(inspector(name).getByRole('button', { name: label('automation.moveToRecycle') }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: label('automation.moveToRecycle') }));
    await waitFor(() => expect(api.deleteAutomation).toHaveBeenCalledWith(id, version));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(queryClient.getQueryData(queryKeys.automations.readDetail('user-1', id))).toBeUndefined());
    await waitFor(() => expect(screen.queryByRole('heading', { level: 2, name })).not.toBeInTheDocument());
    await within(region()).findByRole('button', { name: label('automation.recycleBin', { total: 1 }) });

    queryClient.setQueryData(queryKeys.automations.readDetail('user-1', id), liveRow);
    fireEvent.click(bin(1));
    const runsForTask = () =>
      (scope === 'personal' ? vi.mocked(api.listExecutions) : vi.mocked(api.listAutomationExecutionReads)).mock.calls.filter(
        (call) => call[0] === id,
      ).length;
    const runsBefore = runsForTask();
    fireEvent.click(await within(region()).findByRole('button', { name: new RegExp(name) }));
    await waitFor(() => expect(screen.getAllByRole('heading', { level: 2, name })).toHaveLength(1));
    const deletedPane = inspector(name);
    expect(deletedPane.getByRole('button', { name: label('automation.restoreTask') })).toBeEnabled();
    expect(deletedPane.getByText(/user-2/)).toBeInTheDocument();
    expect(deletedPane.queryByRole('button', { name: label('automation.runNow') })).not.toBeInTheDocument();
    expect(deletedPane.queryByRole('button', { name: label('automation.edit') })).not.toBeInTheDocument();
    expect(deletedPane.queryByRole('button', { name: label('automation.pause') })).not.toBeInTheDocument();
    expect(deletedPane.queryByRole('button', { name: label('automation.moveToRecycle') })).not.toBeInTheDocument();
    expect(runsForTask()).toBe(runsBefore);

    fireEvent.click(deletedPane.getByRole('button', { name: label('automation.restoreTask') }));
    await waitFor(() => expect(api.restoreAutomation).toHaveBeenCalledWith(id, version));
    await within(region()).findByRole('button', { name: label('automation.recycleBin', { total: 0 }) });
    expect(bin(0)).toHaveAttribute('aria-pressed', 'false');
    await waitFor(() => expect(screen.getAllByRole('heading', { level: 2, name })).toHaveLength(1));
    await waitFor(() => expect(inspector(name).queryByRole('button', { name: label('automation.restoreTask') })).not.toBeInTheDocument());
    expect(inspector(name).getByRole('button', { name: label('automation.edit') })).toBeInTheDocument();
    if (scope === 'personal') {
      expect(inspector(name).getByRole('button', { name: label('automation.runNow') })).toBeInTheDocument();
    }
    expect(within(region()).getByRole('button', { name: new RegExp(name) })).toBeInTheDocument();
  }

  it('returns a deleted personal task to the live list and drops its cached detail', async () => {
    await deleteThenRestore(
      'personal',
      agent({ automation_id: 'me-1', name: 'Morning note', scope: 'personal', config_version: 4 }),
      'automation.groupMine',
      4,
    );
  });

  it('returns a deleted data: script to the live list and drops its cached detail', async () => {
    await deleteThenRestore(
      'company',
      script({ automation_id: 'data:prices', name: 'Close prices', config_version: 5 }),
      'automation.groupCompany',
      5,
    );
  });
});
