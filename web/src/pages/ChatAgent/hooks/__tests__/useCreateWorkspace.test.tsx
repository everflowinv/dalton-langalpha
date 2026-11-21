/**
 * A refused create re-reads the computers, whoever opened the form: a refusal
 * for one still being prepared means the list the form chose from is behind.
 */
import { describe, it, expect, vi } from 'vitest';
import type { Mock } from 'vitest';

import { renderHookWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';

vi.mock('../../utils/api', () => ({ createWorkspace: vi.fn() }));

import { createWorkspace } from '../../utils/api';
import { useCreateWorkspace } from '../useCreateWorkspace';

describe('useCreateWorkspace', () => {
  it('re-reads the computers on a refusal, and passes the refusal on', async () => {
    const refusal = Object.assign(new Error('Request failed with status code 409'), {
      response: {
        status: 409,
        data: { detail: { code: 'computer_preparing', message: 'Computer is still being prepared' } },
      },
    });
    (createWorkspace as Mock).mockRejectedValue(refusal);
    const { result, queryClient } = renderHookWithProviders(() => useCreateWorkspace());
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

    await expect(result.current({ name: 'Raced', description: '' })).rejects.toBe(refusal);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.computers.lists() });
  });

  // DALTON-HOOK(workspace-dedup): company_workspace_exists does not invalidate computers and allowDuplicateCompany forwards override
  it('does not treat company_workspace_exists as a computer refusal, and forwards allowDuplicateCompany', async () => {
    const dupRefusal = Object.assign(new Error('Request failed with status code 409'), {
      response: {
        status: 409,
        data: {
          detail: {
            code: 'company_workspace_exists',
            message: '「Anthropic」已有工作区「ANT」（所有者 Richard），请直接在那里继续。确实要另建，请确认后重试。',
            matches: [
              {
                workspace_id: 'ws-ant',
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
    });
    (createWorkspace as Mock).mockRejectedValueOnce(dupRefusal);
    const { result, queryClient } = renderHookWithProviders(() => useCreateWorkspace());
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

    await expect(result.current({ name: 'Anthropic', description: '' })).rejects.toBe(dupRefusal);
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: queryKeys.computers.lists() });

    const created = { workspace_id: 'ws-new', name: 'Anthropic' };
    (createWorkspace as Mock).mockResolvedValueOnce(created);
    const out = await result.current({
      name: 'Anthropic',
      description: 'Notes',
      computerId: 'comp-1',
      allowDuplicateCompany: true,
    });
    expect(out).toBe(created);
    expect(createWorkspace).toHaveBeenLastCalledWith('Anthropic', 'Notes', {}, 'comp-1', true);
  });
});
