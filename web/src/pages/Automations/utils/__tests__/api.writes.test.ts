// @vitest-environment node
// DALTON-HOOK(company-automations): company write bodies stay numeric and encoded.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { pauseAutomation, resumeAutomation, updateAutomation, updateCompanyScript } from '../api';

const post = vi.hoisted(() => vi.fn());
const patch = vi.hoisted(() => vi.fn());

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(),
    post,
    patch,
    delete: vi.fn(),
    put: vi.fn(),
  },
}));

beforeEach(() => {
  post.mockReset();
  patch.mockReset();
  post.mockResolvedValue({ data: {} });
  patch.mockResolvedValue({ data: {} });
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

  it('patches a company agent with the numeric version on the encoded path', async () => {
    await updateAutomation('auto-1', { name: 'Desk', expected_config_version: 7 });
    expect(patch.mock.calls[0]).toEqual([
      '/api/v1/automations/auto-1',
      { name: 'Desk', expected_config_version: 7 },
    ]);
  });
});
