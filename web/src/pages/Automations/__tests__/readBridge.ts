import { vi } from 'vitest';
import type { Automation } from '@/types/automation';

interface ListedAutomations {
  data: { automations: Automation[] };
}

type ListAutomations = (params: { limit?: number; offset?: number }) => Promise<ListedAutomations>;

/**
 * Personal manage reads for a page test. `listAutomations` is the same mock
 * the test exports, so these helpers never import the API module themselves.
 * Company and research stay empty. A missing detail is a 404.
 */
export function personalReadMocks(listAutomations: ListAutomations) {
  const listAutomationReads = vi.fn(async (params?: { scope?: string; limit?: number; offset?: number }) => {
    if (params?.scope !== 'personal') {
      return { data: { automations: [] as Automation[], total: 0 }, status: 200, statusText: 'OK', headers: {} };
    }
    return listAutomations({ limit: params.limit ?? 100, offset: params.offset ?? 0 });
  });

  const getAutomationRead = vi.fn(async (id: string) => {
    const list = await listAutomations({ limit: 100, offset: 0 });
    const found = list.data.automations.find((item) => item.automation_id === id);
    if (!found) {
      const missing = new Error('not found');
      Object.assign(missing, { response: { status: 404, data: { detail: 'not found' } } });
      throw missing;
    }
    return {
      data: {
        ...found,
        engine_kind: 'agent' as const,
        scope: 'personal' as const,
        notify_storage_mode: 'default',
        config_version: 1,
        active_version: 1,
        created_by: null,
        updated_by: null,
        additional_context: null,
      },
      status: 200,
      statusText: 'OK',
      headers: {},
    };
  });

  return { listAutomationReads, getAutomationRead };
}
