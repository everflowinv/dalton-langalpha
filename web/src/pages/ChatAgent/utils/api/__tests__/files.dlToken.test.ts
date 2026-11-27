// @vitest-environment jsdom
/**
 * Download ticket (`dl_token`) handling in `triggerFileDownload` / `saveWorkspaceFile`.
 *
 * When `/files/download-url` returns `url: null` and a short-lived `dl_token`,
 * the browser triggers a direct `<a href=...>` download against
 * `/api/v1/dalton/workspace-files/download?dl_token=...` instead of buffering
 * the entire file as an in-memory Blob. When both are absent under Dalton auth,
 * existing Blob fallback behavior is preserved.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: {
      get: vi.fn(),
      getUri: vi.fn((config: { url?: string; params?: Record<string, unknown> }) => {
        const params = new URLSearchParams();
        for (const [k, v] of Object.entries(config.params ?? {})) {
          if (v !== undefined && v !== null) params.set(k, String(v));
        }
        const qs = params.toString();
        return qs ? `${config.url ?? ''}?${qs}` : (config.url ?? '');
      }),
    },
  };
});
vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
}));
vi.mock('@/lib/supabase', () => ({ supabase: null }));
vi.mock('@/lib/authToken', () => ({
  authGeneration: vi.fn(() => 0),
  bearerTokenOf: vi.fn(() => null),
  getAccessToken: vi.fn(async () => null),
  refreshAccessToken: vi.fn(async () => null),
}));
vi.mock('../../../components/filePanel/downloadNotice', () => ({
  withDownloadNotice: vi.fn(
    async (
      _key: string,
      _name: string,
      run: (report: () => void, current: () => boolean) => Promise<unknown>,
    ) => {
      await run(() => {}, () => true);
    },
  ),
  workspaceDownloadKey: vi.fn((ws: string, path: string) => `${ws}:${path}`),
}));

import { api } from '@/api/client';
import { triggerFileDownload } from '../files';

const mockGet = api.get as unknown as Mock;
const mockGetUri = api.getUri as unknown as Mock;

describe('saveWorkspaceFile dl_token handling', () => {
  let clickedHrefs: string[] = [];
  let clickSpy: ReturnType<typeof vi.spyOn>;
  const origCreateObjectURL = URL.createObjectURL;
  const origRevokeObjectURL = URL.revokeObjectURL;

  beforeEach(() => {
    clickedHrefs = [];
    mockGet.mockReset();
    mockGetUri.mockClear();
    URL.createObjectURL = vi.fn(() => 'blob:mock-url');
    URL.revokeObjectURL = vi.fn();
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedHrefs.push(this.getAttribute('href') ?? '');
    });
  });

  afterEach(() => {
    clickSpy.mockRestore();
    URL.createObjectURL = origCreateObjectURL;
    URL.revokeObjectURL = origRevokeObjectURL;
  });

  it('uses /api/v1/dalton/workspace-files/download with dl_token directly and skips blob fetch when url is null', async () => {
    mockGet.mockResolvedValueOnce({
      data: { url: null, dl_token: 'ticket-abc-123' },
    });

    await triggerFileDownload('ws-1', 'reports/big.parquet');

    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith('/api/v1/workspaces/ws-1/files/download-url', {
      params: { path: 'reports/big.parquet', backup_ok: true },
    });
    expect(mockGetUri).toHaveBeenCalledWith({
      url: '/api/v1/dalton/workspace-files/download',
      params: { dl_token: 'ticket-abc-123' },
    });
    expect(clickedHrefs).toEqual([
      '/api/v1/dalton/workspace-files/download?dl_token=ticket-abc-123',
    ]);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('falls back to blob download when both url and dl_token are absent under Dalton auth', async () => {
    mockGet
      .mockResolvedValueOnce({
        data: { url: null },
      })
      .mockResolvedValueOnce({
        data: new Blob(['hello']),
        headers: {},
      });

    await triggerFileDownload('ws-1', 'notes/small.txt');

    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(mockGet.mock.calls[1][0]).toBe('/api/v1/workspaces/ws-1/files/download');
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(clickedHrefs).toEqual(['blob:mock-url']);
  });
});
