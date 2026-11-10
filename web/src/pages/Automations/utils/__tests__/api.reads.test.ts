// @vitest-environment node
// DALTON-HOOK(company-automations): read API contract for agent and script tasks.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  AgentAutomationRead,
  AgentAutomationRunRead,
  Automation,
  AutomationExecution,
  AutomationHistory,
  AutomationRead,
  AutomationRun,
  AutomationRunRead,
  ScriptAutomationRead,
  ScriptAutomationRunRead,
} from '@/types/automation';
import {
  automationExecutionReadQuery,
  automationHistoryQuery,
  automationReadListQuery,
  automationRunReadQuery,
  getAutomationRead,
  listAutomationExecutionReads,
  listAutomationHistory,
  listAutomationReads,
  listAutomationRunReads,
} from '../api';

const get = vi.hoisted(() => vi.fn());

vi.mock('@/api/client', () => ({
  api: { get },
}));

const AGENT_ID = '550e8400-e29b-41d4-a716-446655440000';
const SCRIPT_ID = 'data:daily-prices';
const FANCY_ID = 'data:job/key with space';

type Expect<T extends true> = T;
type NotAssignable<A, B> = [A] extends [B] ? false : true;

const scriptIsNotAnAgentTask: Expect<NotAssignable<ScriptAutomationRead, Automation>> = true;
const scriptRunIsNotAnAgentRun: Expect<NotAssignable<ScriptAutomationRunRead, AutomationRun>> = true;
const scriptRunIsNotAnExecution: Expect<NotAssignable<ScriptAutomationRunRead, AutomationExecution>> = true;
const agentHasInstruction: Expect<'instruction' extends keyof AgentAutomationRead ? true : false> = true;
const scriptOmitsInstruction: Expect<
  'instruction' extends keyof ScriptAutomationRead ? false : true
> = true;
const scriptOmitsUser: Expect<'user_id' extends keyof ScriptAutomationRead ? false : true> = true;
const scriptOmitsWorkspace: Expect<'workspace_id' extends keyof ScriptAutomationRead ? false : true> = true;
const scriptOmitsTrigger: Expect<'trigger_type' extends keyof ScriptAutomationRead ? false : true> = true;
const scriptRunOmitsTrigger: Expect<'trigger_type' extends keyof ScriptAutomationRunRead ? false : true> = true;
const agentLastRunOmitsEngineKind: Expect<
  'engine_kind' extends keyof NonNullable<AgentAutomationRead['last_execution']> ? false : true
> = true;

function omittedAgentInstruction(row: Extract<AutomationRead, { engine_kind: 'script' }>): unknown {
  // @ts-expect-error script tasks have no agent instruction
  return row.instruction;
}

function omittedScriptTrigger(row: Extract<AutomationRunRead, { engine_kind: 'script' }>): unknown {
  // @ts-expect-error script runs do not include trigger_type
  return row.trigger_type;
}

function taskLabel(row: AutomationRead): string {
  if (row.engine_kind === 'script') return `${row.name}:${row.last_execution?.log_tail ?? ''}`;
  return `${row.name}:${row.instruction}`;
}

function runLabel(row: AutomationRunRead): string {
  if (row.engine_kind === 'script') return `${row.automation_name}:${row.status}`;
  return `${row.automation_name}:${row.trigger_type}`;
}

function execution(id = 'exec-1'): AutomationExecution {
  return {
    automation_execution_id: id,
    automation_id: AGENT_ID,
    status: 'completed',
    conversation_thread_id: null,
    scheduled_at: '2026-11-01T09:00:00Z',
    started_at: '2026-11-01T09:00:01Z',
    completed_at: '2026-11-01T09:00:20Z',
    error_message: null,
    skip_reason: null,
    failure_reason: null,
    delivery_result: null,
    created_at: '2026-11-01T09:00:00Z',
    excerpt: 'Done.',
    dismissed_at: null,
  };
}

function agentRead(): AgentAutomationRead {
  return {
    automation_id: AGENT_ID,
    user_id: 'user-1',
    name: 'Morning note',
    description: null,
    trigger_type: 'cron',
    cron_expression: '0 9 * * 1-5',
    timezone: 'America/New_York',
    trigger_config: null,
    next_run_at: null,
    last_run_at: null,
    instruction: 'Summarize the book.',
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
    last_execution: execution(),
    engine_kind: 'agent',
    scope: 'personal',
    notify_storage_mode: 'legacy',
    config_version: 2,
    active_version: 2,
    created_by: 'user-1',
    updated_by: null,
  };
}

function scriptRead(): ScriptAutomationRead {
  return {
    automation_id: SCRIPT_ID,
    name: 'Daily prices',
    status: 'active',
    enabled: true,
    paused_by_user: false,
    cron_expression: '0 6 * * *',
    timezone: 'UTC',
    config_version: 4,
    active_version: 4,
    updated_at: '2026-11-03T00:00:00Z',
    notify_storage_mode: 'task_row',
    engine_kind: 'script',
    scope: 'company',
    description: null,
    next_run_at: null,
    last_run_at: '2026-11-03T06:00:00Z',
    updated_by: 'user-9',
    dalton_notify_category: 'data_task',
    dalton_notify_effective: true,
    dalton_notify_source: 'task_row',
    dalton_notify_route_targets: [],
    last_execution: {
      automation_execution_id: 'run-1',
      automation_id: SCRIPT_ID,
      status: 'succeeded',
      scheduled_at: '2026-11-03T06:00:00Z',
      created_at: '2026-11-03T06:00:01Z',
      engine_kind: 'script',
      conversation_thread_id: null,
      started_at: '2026-11-03T06:00:02Z',
      completed_at: '2026-11-03T06:00:10Z',
      exit_code: 0,
      failure_reason: null,
      notify_status: 'sent',
      triggered_by_user_id: null,
      log_tail: 'wrote prices',
      trigger_source: 'schedule',
    },
  };
}

function agentRun(): AgentAutomationRunRead {
  return {
    ...execution('exec-agent'),
    automation_name: 'Morning note',
    trigger_type: 'cron',
    workspace_id: null,
    engine_kind: 'agent',
  };
}

function scriptRun(): ScriptAutomationRunRead {
  return {
    automation_execution_id: 'exec-script',
    automation_id: SCRIPT_ID,
    automation_name: 'Daily prices',
    status: 'timed_out',
    scheduled_at: '2026-11-03T06:00:00Z',
    created_at: '2026-11-03T06:00:01Z',
    engine_kind: 'script',
    conversation_thread_id: null,
    started_at: '2026-11-03T06:00:02Z',
    completed_at: '2026-11-03T06:05:00Z',
    exit_code: 124,
    failure_reason: 'timed_out',
    notify_status: null,
    triggered_by_user_id: null,
    log_tail: 'still running',
    trigger_source: 'schedule',
    workspace_id: null,
    excerpt: null,
  };
}

function historyBody(): AutomationHistory {
  return {
    automation_id: SCRIPT_ID,
    scope: 'company',
    engine_kind: 'script',
    config_version: 4,
    active_version: 3,
    entries: [
      {
        version: 4,
        prev_version: 3,
        restored_from_version: null,
        actor_user_id: null,
        changed_at: null,
        action: 'update',
        changes: { cron_expression: { old: '0 6 * * *', new: '0 7 * * *' } },
        snapshot: { cron_expression: '0 7 * * *' },
      },
    ],
    total: 80,
    limit: 50,
    offset: 50,
  };
}

function answer<T>(data: T) {
  return { data, status: 200, statusText: 'OK', headers: {} };
}

function sentParams(index = 0): Record<string, unknown> {
  const config = get.mock.calls[index]?.[1] as { params?: Record<string, unknown> } | undefined;
  if (!config?.params) throw new Error('missing params');
  return config.params;
}

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue(answer({}));
});

describe('automation read requests', () => {
  it('lists tasks with the default page and leaves scope and status off', async () => {
    await listAutomationReads();
    expect(get).toHaveBeenCalledWith('/api/v1/automations', {
      params: { limit: 50, offset: 0 },
    });
    expect(sentParams()).not.toHaveProperty('scope');
    expect(sentParams()).not.toHaveProperty('status');
    expect(automationReadListQuery()).toEqual({ scope: null, status: null, limit: 50, offset: 0 });
    expect(automationReadListQuery({ limit: 50, offset: 0 })).toEqual(automationReadListQuery());
  });

  it('sends personal, research_tracking, and company scopes unchanged', async () => {
    for (const scope of ['personal', 'research_tracking', 'company'] as const) {
      get.mockClear();
      await listAutomationReads({ scope });
      expect(get).toHaveBeenCalledWith('/api/v1/automations', {
        params: { scope, limit: 50, offset: 0 },
      });
      await listAutomationRunReads({ scope });
      expect(get).toHaveBeenCalledWith('/api/v1/automations/executions', {
        params: { scope, limit: 20, offset: 0 },
      });
    }
    expect(automationReadListQuery({ scope: 'personal' }).scope).toBe('personal');
    expect(automationReadListQuery().scope).toBeNull();
  });

  it('sends status and an explicit list page without rewriting the status', async () => {
    await listAutomationReads({ status: 'succeeded', limit: 10, offset: 20 });
    expect(get).toHaveBeenCalledWith('/api/v1/automations', {
      params: { status: 'succeeded', limit: 10, offset: 20 },
    });
    get.mockClear();
    await listAutomationReads({ status: '' });
    expect(get).toHaveBeenCalledWith('/api/v1/automations', {
      params: { status: '', limit: 50, offset: 0 },
    });
  });

  it('lists the run feed with the default page and leaves scope, status, and thread off', async () => {
    await listAutomationRunReads();
    expect(get).toHaveBeenCalledWith('/api/v1/automations/executions', {
      params: { limit: 20, offset: 0 },
    });
    expect(sentParams()).not.toHaveProperty('scope');
    expect(sentParams()).not.toHaveProperty('status');
    expect(sentParams()).not.toHaveProperty('thread_id');
    expect(automationRunReadQuery()).toEqual({
      scope: null,
      status: null,
      thread_id: null,
      limit: 20,
      offset: 0,
    });
    expect(automationRunReadQuery({ thread_id: undefined })).toEqual(automationRunReadQuery());
  });

  it('sends feed scope, status, thread_id, and page fields unchanged', async () => {
    const threadId = '11111111-1111-4111-8111-111111111111';
    await listAutomationRunReads({
      scope: 'company',
      status: 'timed_out',
      thread_id: threadId,
      limit: 5,
      offset: 15,
    });
    expect(get).toHaveBeenCalledWith('/api/v1/automations/executions', {
      params: {
        scope: 'company',
        status: 'timed_out',
        thread_id: threadId,
        limit: 5,
        offset: 15,
      },
    });
  });

  it('returns mixed agent and script rows with the server total and has_more', async () => {
    const listed = answer({ automations: [agentRead(), scriptRead()], total: 41 });
    get.mockResolvedValueOnce(listed);
    await expect(listAutomationReads({ scope: 'company' })).resolves.toBe(listed);
    const script = listed.data.automations[1];
    expect(script.engine_kind).toBe('script');
    if (script.engine_kind !== 'script') throw new Error('expected a script task');
    expect(script.last_execution?.status).toBe('succeeded');
    expect(script).not.toHaveProperty('instruction');
    expect(script).not.toHaveProperty('trigger_type');

    const feed = answer({ executions: [agentRun(), scriptRun()], has_more: true });
    get.mockResolvedValueOnce(feed);
    await expect(listAutomationRunReads({ scope: 'company' })).resolves.toBe(feed);
    const run = feed.data.executions[1];
    expect(run.engine_kind).toBe('script');
    if (run.engine_kind !== 'script') throw new Error('expected a script run');
    expect(run.status).toBe('timed_out');
    expect(run.workspace_id).toBeNull();
    expect(run.excerpt).toBeNull();
    expect(run).not.toHaveProperty('trigger_type');
    expect(feed.data.has_more).toBe(true);
    expect(feed.data.executions).toHaveLength(2);
  });

  it('encodes a full data id for detail, runs, and history', async () => {
    await getAutomationRead(SCRIPT_ID);
    expect(get).toHaveBeenCalledWith(`/api/v1/automations/${encodeURIComponent(SCRIPT_ID)}`);
    expect(decodeURIComponent(encodeURIComponent(SCRIPT_ID))).toBe(SCRIPT_ID);
    expect(get.mock.calls[0]).toHaveLength(1);

    get.mockClear();
    await listAutomationExecutionReads(FANCY_ID, { limit: 7, offset: 3 });
    expect(get).toHaveBeenCalledWith(
      `/api/v1/automations/${encodeURIComponent(FANCY_ID)}/executions`,
      { params: { limit: 7, offset: 3 } },
    );
    expect(decodeURIComponent(encodeURIComponent(FANCY_ID))).toBe(FANCY_ID);

    get.mockClear();
    const history = answer(historyBody());
    get.mockResolvedValueOnce(history);
    await expect(listAutomationHistory(FANCY_ID, { limit: 50, offset: 50 })).resolves.toBe(history);
    expect(get).toHaveBeenCalledWith(
      `/api/v1/automations/${encodeURIComponent(FANCY_ID)}/history`,
      { params: { limit: 50, offset: 50 } },
    );
    expect(history.data.total).toBe(80);
    expect(history.data.entries).toHaveLength(1);
    expect(history.data.entries[0].changed_at).toBeNull();
    expect(history.data.entries[0].actor_user_id).toBeNull();
    expect(history.data.entries[0].restored_from_version).toBeNull();
  });

  it('requests a non-uuid id without checking or truncating it', async () => {
    await getAutomationRead('not-a-uuid');
    expect(get).toHaveBeenCalledWith('/api/v1/automations/not-a-uuid');
  });

  it('reads one task with no scope and uses each route default page', async () => {
    await getAutomationRead(AGENT_ID);
    expect(get).toHaveBeenCalledWith(`/api/v1/automations/${AGENT_ID}`);

    get.mockClear();
    await listAutomationExecutionReads(AGENT_ID);
    expect(get).toHaveBeenCalledWith(`/api/v1/automations/${AGENT_ID}/executions`, {
      params: { limit: 20, offset: 0 },
    });
    expect(automationExecutionReadQuery()).toEqual({ limit: 20, offset: 0 });

    get.mockClear();
    await listAutomationHistory(SCRIPT_ID);
    expect(get).toHaveBeenCalledWith(`/api/v1/automations/${encodeURIComponent(SCRIPT_ID)}/history`, {
      params: { limit: 50, offset: 0 },
    });
    expect(automationHistoryQuery()).toEqual({ limit: 50, offset: 0 });
    expect(automationHistoryQuery({ offset: 50 })).toEqual({ limit: 50, offset: 50 });
  });

  it('does not request an empty id', () => {
    expect(() => getAutomationRead('')).toThrow(/Missing automation id/);
    expect(() => listAutomationExecutionReads('')).toThrow(/Missing automation id/);
    expect(() => listAutomationHistory('')).toThrow(/Missing automation id/);
    expect(get).not.toHaveBeenCalled();
  });
});

describe('automation read discriminators', () => {
  it('narrows agent and script rows and does not read agent fields from a script', () => {
    expect(scriptIsNotAnAgentTask).toBe(true);
    expect(scriptRunIsNotAnAgentRun).toBe(true);
    expect(scriptRunIsNotAnExecution).toBe(true);
    expect(agentHasInstruction).toBe(true);
    expect(scriptOmitsInstruction).toBe(true);
    expect(scriptOmitsUser).toBe(true);
    expect(scriptOmitsWorkspace).toBe(true);
    expect(scriptOmitsTrigger).toBe(true);
    expect(scriptRunOmitsTrigger).toBe(true);
    expect(agentLastRunOmitsEngineKind).toBe(true);

    const agent = agentRead();
    const script = scriptRead();
    expect(taskLabel(agent)).toBe('Morning note:Summarize the book.');
    expect(taskLabel(script)).toBe('Daily prices:wrote prices');
    expect(omittedAgentInstruction(script)).toBeUndefined();
    expect(runLabel(agentRun())).toBe('Morning note:cron');
    expect(runLabel(scriptRun())).toBe('Daily prices:timed_out');
    expect(omittedScriptTrigger(scriptRun())).toBeUndefined();
    expect(agent.last_execution).not.toHaveProperty('engine_kind');
  });
});
