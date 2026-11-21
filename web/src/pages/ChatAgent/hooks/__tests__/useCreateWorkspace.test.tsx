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

  // DALTON-HOOK(workspace-dedup): preserves unbound coverage unit matches without workspace_id or owner_name
  it('parses unbound coverage unit matches from company_workspace_exists', async () => {
    const { companyWorkspaceExistsOf } = await import('../useCreateWorkspace');
    const refusal = {
      response: {
        status: 409,
        data: {
          detail: {
            code: 'company_workspace_exists',
            message: '「Anthropic」已登记为团队覆盖公司（主分析师 Richard），尚未绑定工作区',
            matches: [
              {
                unbound_unit: true,
                display_name: 'Anthropic, PBC',
                ticker: 'ANT',
                lead_analyst_name: 'Richard',
                can_read: true,
              },
            ],
          },
        },
      },
    };
    const parsed = companyWorkspaceExistsOf(refusal);
    expect(parsed).toEqual({
      message: '「Anthropic」已登记为团队覆盖公司（主分析师 Richard），尚未绑定工作区',
      matches: [
        {
          workspace_id: null,
          workspace_name: null,
          owner_name: null,
          display_name: 'Anthropic, PBC',
          lead_analyst_name: 'Richard',
          ticker: 'ANT',
          can_read: true,
          unbound_unit: true,
        },
      ],
    });
  });

  // DALTON-HOOK(workspace-dedup): forward coverage registration fields and parse 409 model prefill suggestion
  it('forwards coverageKind/ticker/displayName and parses model suggestion from 409', async () => {
    const { companyWorkspaceExistsOf } = await import('../useCreateWorkspace');
    const refusal = {
      response: {
        status: 409,
        data: {
          detail: {
            code: 'company_workspace_exists',
            message: 'dup',
            matches: [{ workspace_name: 'NVDA', owner_name: 'Richard', can_read: false }],
            suggestion: {
              coverage_kind: 'company',
              ticker: 'NVDA',
              display_name: 'NVIDIA Corporation',
            },
          },
        },
      },
    };
    const parsed = companyWorkspaceExistsOf(refusal);
    expect(parsed?.suggestion).toEqual({
      coverage_kind: 'company',
      ticker: 'NVDA',
      display_name: 'NVIDIA Corporation',
    });

    const created = { workspace_id: 'ws-nvda', name: 'Nvidia' };
    (createWorkspace as Mock).mockResolvedValueOnce(created);
    const { result, queryClient } = renderHookWithProviders(() => useCreateWorkspace());
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    const out = await result.current({
      name: 'Nvidia',
      description: '',
      coverageKind: 'company',
      ticker: 'NVDA',
      displayName: 'NVIDIA Corporation',
    });
    expect(out).toBe(created);
    expect(createWorkspace).toHaveBeenLastCalledWith('Nvidia', '', {}, undefined, undefined, {
      kind: 'company',
      ticker: 'NVDA',
      displayName: 'NVIDIA Corporation',
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.dalton.coverageUnits() });
  });

  // DALTON-HOOK(workspace-dedup): coverage_suggestion does not invalidate computers and forwards skipCoverageSuggestion
  it('parses coverage_suggestion without invalidating computers and forwards skipCoverageSuggestion', async () => {
    const { coverageSuggestionOf } = await import('../useCreateWorkspace');
    const suggestionRefusal = Object.assign(new Error('Request failed with status code 409'), {
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
    expect(coverageSuggestionOf(suggestionRefusal)).toEqual({
      message: '「Nvidia」识别为公司（代码 NVDA）',
      suggestion: {
        coverage_kind: 'company',
        ticker: 'NVDA',
        display_name: 'NVIDIA Corporation',
      },
    });

    (createWorkspace as Mock).mockRejectedValueOnce(suggestionRefusal);
    const { result, queryClient } = renderHookWithProviders(() => useCreateWorkspace());
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

    await expect(result.current({ name: 'Nvidia', description: '' })).rejects.toBe(suggestionRefusal);
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: queryKeys.computers.lists() });

    const created = { workspace_id: 'ws-nvda-skip', name: 'Nvidia' };
    (createWorkspace as Mock).mockResolvedValueOnce(created);
    const out = await result.current({
      name: 'Nvidia',
      description: '',
      skipCoverageSuggestion: true,
    });
    expect(out).toBe(created);
    expect(createWorkspace).toHaveBeenLastCalledWith(
      'Nvidia',
      '',
      {},
      undefined,
      undefined,
      undefined,
      true,
    );
  });
});
