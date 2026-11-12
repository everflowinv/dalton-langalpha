// @vitest-environment node
// DALTON-HOOK(company-automations): company write bodies stay numeric and encoded.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { deleteAutomation, pauseAutomation, restoreAutomation, resumeAutomation, rollbackAutomation, updateAutomation, updateCompanyScript } from '../api';

const post = vi.hoisted(() => vi.fn());
const patch = vi.hoisted(() => vi.fn());
const remove = vi.hoisted(() => vi.fn());

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(),
    post,
    patch,
    delete: remove,
    put: vi.fn(),
  },
}));

beforeEach(() => {
  post.mockReset();
  patch.mockReset();
  remove.mockReset();
  post.mockResolvedValue({ data: {} });
  patch.mockResolvedValue({ data: {} });
  remove.mockResolvedValue({ data: {} });
});

describe('automation writes', () => {
  it('pauses a personal task with no body', async () => {
    await pauseAutomation('auto-1');
    expect(post.mock.calls[0]).toEqual(['/api/v1/automations/auto-1/pause']);
  });

  it('pauses a company script with the version in the body', async () => {
    await pauseAutomation('data:prices', 4);
    expect(post.mock.calls[0]).toEqual([
      '/api/v1/automations/data%3Aprices/pause',
      { expected_config_version: 4 },
    ]);
  });

  it('resumes a company script with the version in the body', async () => {
    await resumeAutomation('data:prices', 5);
    expect(post.mock.calls[0]).toEqual([
      '/api/v1/automations/data%3Aprices/resume',
      { expected_config_version: 5 },
    ]);
  });

  it('patches a company script with only the changed schedule and the version', async () => {
    await updateCompanyScript('data:job/key', {
      expected_config_version: 3,
      cron_expression: '0 10 * * 1-5',
    });
    expect(patch.mock.calls[0]).toEqual([
      `/api/v1/automations/${encodeURIComponent('data:job/key')}`,
      { expected_config_version: 3, cron_expression: '0 10 * * 1-5' },
    ]);
  });

  it('deletes into the recycle bin with the version in the body', async () => {
    await deleteAutomation('data:prices', 4);
    expect(remove.mock.calls[0]).toEqual([
      '/api/v1/automations/data%3Aprices',
      { data: { expected_config_version: 4 } },
    ]);
  });

  it('restores a deleted task with the version in the body', async () => {
    await restoreAutomation('auto-1', 8);
    expect(post.mock.calls[0]).toEqual(['/api/v1/automations/auto-1/restore', { expected_config_version: 8 }]);
  });

  it('rolls back to the previous version with both versions in the body', async () => {
    await rollbackAutomation('data:prices', 3, 9);
    expect(post.mock.calls[0]).toEqual([
      '/api/v1/automations/data%3Aprices/rollback',
      { target_version: 3, expected_config_version: 9 },
    ]);
  });

  it('patches a company agent with the numeric version on the encoded path', async () => {
    await updateAutomation('auto-1', { name: 'Desk', expected_config_version: 7 });
    expect(patch.mock.calls[0]).toEqual([
      '/api/v1/automations/auto-1',
      { name: 'Desk', expected_config_version: 7 },
    ]);
  });
});
