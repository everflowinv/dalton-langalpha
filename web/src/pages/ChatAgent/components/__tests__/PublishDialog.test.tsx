/**
 * DALTON-HOOK(monitors): updated from publishArtifact to publishMonitor with monitor API mocks.
 * The Publish dialog is the review step: the list it shows is exactly what a
 * publish sends, and the server's 409 is the one answer that never reads as
 * success. Network calls are mocked at the artifacts/monitors api leaf; the hooks
 * and the error shape helpers (`filesChangedIn`, `shareCapIn`) stay real so the
 * tests read an API client error the way the dialog ships. The record comes
 * from the get-or-create (`ensureArtifact`), and a write's refresh reads it
 * back by path (`findArtifact`), so `serve` answers both.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '@/test/utils';
import { runAuthResets } from '@/lib/authResets';
import { notePageLoads, notePageOpen } from '@/lib/pageLoads';
import type { Artifact, ArtifactStatus, ArtifactVersion, ShareFileEntry } from '@/types/api';
import type { MonitorSummary } from '@/pages/Monitors/api';

vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));

// The page the dialog loads out of sight: it answers at once, unless a test
// holds it, which is the one place its wait matters.
const scout = vi.hoisted(() => ({ settle: null as (() => void) | null, hold: false, grantFails: false }));
vi.mock('../viewers/html/BridgedFrame', () => ({
  BridgedFrame: ({ onUnbridged }: { onUnbridged?: () => void }) => {
    React.useEffect(() => {
      if (scout.hold) scout.settle = onUnbridged ?? null;
      else onUnbridged?.();
    });
    return null;
  },
}));

// Its own grant, which a test may have never arrive.
vi.mock('../viewers/html/liveFrame', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../viewers/html/liveFrame')>();
  return {
    ...actual,
    useLiveFrame: (options: Parameters<typeof actual.useLiveFrame>[0]) => {
      const live = actual.useLiveFrame(options);
      return scout.grantFails ? { ...live, source: null, failed: true } : live;
    },
  };
});

vi.mock('@/pages/ChatAgent/utils/api/artifacts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/utils/api/artifacts')>();
  return {
    ...actual,
    findArtifact: vi.fn<typeof actual.findArtifact>(),
    ensureArtifact: vi.fn<typeof actual.ensureArtifact>(),
    getArtifactStatus: vi.fn<typeof actual.getArtifactStatus>(),
    listArtifactVersions: vi.fn<typeof actual.listArtifactVersions>(async () => []),
    unshareArtifact: vi.fn<typeof actual.unshareArtifact>(),
    setArtifactKeepUpdated: vi.fn<typeof actual.setArtifactKeepUpdated>(),
    setArtifactFileLeftOut: vi.fn<typeof actual.setArtifactFileLeftOut>(),
  };
});

// DALTON-HOOK(monitors): typed monitors API doubles for PublishDialog submission tests
vi.mock('@/pages/Monitors/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/Monitors/api')>();
  return {
    ...actual,
    getMonitorByArtifact: vi.fn<typeof actual.getMonitorByArtifact>(async () => null),
    listCompanyMembers: vi.fn<typeof actual.listCompanyMembers>(async () => []),
    publishMonitor: vi.fn<typeof actual.publishMonitor>(),
    updateMonitor: vi.fn<typeof actual.updateMonitor>(),
  };
});

import { toast } from '@/components/ui/use-toast';
import {
  ensureArtifact,
  findArtifact,
  getArtifactStatus,
  listArtifactVersions,
  unshareArtifact,
  setArtifactFileLeftOut,
} from '@/pages/ChatAgent/utils/api/artifacts';
import { getMonitorByArtifact, publishMonitor } from '@/pages/Monitors/api';
import PublishDialog from '../PublishDialog';

const mockFind = vi.mocked(findArtifact);
const mockEnsure = vi.mocked(ensureArtifact);
const mockStatus = vi.mocked(getArtifactStatus);
const mockVersions = vi.mocked(listArtifactVersions);
const mockUnshare = vi.mocked(unshareArtifact);
const mockGetMonitor = vi.mocked(getMonitorByArtifact);
const mockPublishMonitor = vi.mocked(publishMonitor);

const WS = 'ws-1';
const ID = '6f1c2a7e-3b4d-4e5f-8a9b-0c1d2e3f4a5b';
const CODE = 'k7f2m9q1x4z8';
const ENTRY = 'results/report.html';
const HREF = `${window.location.origin}/a/${CODE}`;

function artifact(overrides: Partial<Artifact> = {}): Artifact {
  return {
    artifact_id: ID,
    code: CODE,
    url: `/a/${CODE}`,
    workspace_id: WS,
    path: ENTRY,
    name: 'report.html',
    shared: false,
    shared_at: null,
    keep_updated: false,
    latest_seq: 0,
    bound_thread_id: null,
    open_comments: 0,
    unread: 0,
    created_at: '2026-10-31T00:00:00Z',
    ...overrides,
  };
}

function version(seq: number, overrides: Partial<ArtifactVersion> = {}): ArtifactVersion {
  return {
    seq,
    entry_path: ENTRY,
    origin: 'owner',
    title: 'Q3 report',
    label: null,
    byte_len: 43_520,
    published_at: '2026-10-31T12:00:00Z',
    ...overrides,
  };
}

const ENTRY_ROW: ShareFileEntry = { path: ENTRY, size: 40_960, reason: 'entry', optional: false };
const STYLE_ROW: ShareFileEntry = { path: 'results/style.css', size: 512, reason: 'style', optional: false };
const CHART_ROW: ShareFileEntry = { path: 'results/charts/rev.png', size: 2_048, reason: 'page', optional: false };

function status(rows: ShareFileEntry[], overrides: Partial<ArtifactStatus> = {}): ArtifactStatus {
  return {
    latest_seq: 0,
    live_sha256: 'a'.repeat(64),
    page_sha256: 'c'.repeat(64),
    files: rows,
    left_out: [],
    total_size: rows.reduce((s, r) => s + (r.size ?? 0), 0),
    changed: null,
    drift: null,
    ...overrides,
  };
}

const NO_DRIFT = { added: [], removed: [], changed: [] };

function monitorSummary(next: Artifact, overrides: Partial<MonitorSummary> = {}): MonitorSummary {
  return {
    monitor_id: '60000000-0000-4000-8000-000000000001',
    slug: 'report',
    artifact_id: next.artifact_id,
    artifact_code: next.code,
    workspace_id: next.workspace_id,
    entry_path: next.path,
    owner_user_id: 'u-1',
    owner_name: 'Owner',
    owner_email: 'owner@company.com',
    title: 'report',
    category: 'other',
    sort_order: 0,
    visibility: 'own',
    member_user_ids: [],
    published_seq: next.latest_seq || 1,
    keep_updated: next.keep_updated,
    source_kind: 'workspace',
    source_ref: `${next.workspace_id}:${next.path}`,
    acl_revision: 1,
    last_publish_error: null,
    last_publish_failed_at: null,
    data_as_of: null,
    created_at: next.created_at,
    updated_at: next.created_at,
    can_manage: true,
    ...overrides,
  };
}

/** The record the server answers from now on, whether made or read. */
function serve(record: Artifact, monitor: MonitorSummary | null = null) {
  mockEnsure.mockResolvedValue(record);
  mockFind.mockResolvedValue(record);
  mockGetMonitor.mockResolvedValue(monitor);
}

/**
 * DALTON-HOOK(monitors): publishMonitor answers with a MonitorDetailResponse and
 * keeps the underlying artifact record refreshed.
 */
function answers(next: Artifact, rest: { version?: ArtifactVersion; cut?: boolean; monitor?: MonitorSummary } = {}) {
  return async () => {
    const mon = rest.monitor ?? monitorSummary(next);
    serve(next, mon);
    return {
      monitor: mon,
      versions: [rest.version ?? version(next.latest_seq || 1)],
      monitor_grant: 'm1.test-grant',
      frame_base: '/api/v1/dalton/monitors/g/m1.test-grant/',
      data_base: '/api/v1/dalton/monitors/g/m1.test-grant/data',
      expires_in: 43200,
      cut: rest.cut ?? true,
    };
  };
}

function apiError(status: number, detail: unknown) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });
}

function renderDialog(path = ENTRY) {
  return renderWithProviders(<PublishDialog open workspaceId={WS} path={path} onClose={() => {}} />);
}

/** The review list, named by its heading. */
const filesList = (count: number) => screen.getByRole('list', { name: new RegExp(`shares ${count} files?$`) });
const publishButton = () => screen.getByRole('button', { name: 'Publish' });
const updateButton = () => screen.getByRole('button', { name: 'Update' });

describe('PublishDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    runAuthResets();
    mockVersions.mockResolvedValue([]);
    mockGetMonitor.mockResolvedValue(null);
  });

  describe('a private file', () => {
    beforeEach(() => {
      serve(artifact());
      mockStatus.mockResolvedValue(status([ENTRY_ROW, STYLE_ROW]));
    });

    // DALTON-HOOK(monitors): lock boxes while publishMonitor is pending
    it('locks the boxes while a publish carries the list', async () => {
      const LINKED: ShareFileEntry = { path: 'results/other.html', size: 100, reason: 'link', optional: true };
      mockStatus.mockResolvedValue(status([ENTRY_ROW, LINKED]));
      mockPublishMonitor.mockReturnValue(new Promise(() => {}));
      renderDialog();

      const box = await screen.findByRole('checkbox', { name: 'Include other.html' });
      await waitFor(() => expect(publishButton()).toBeEnabled());
      expect(box).toBeEnabled();
      await userEvent.click(publishButton());
      await waitFor(() => expect(box).toBeDisabled());
    });

    it('offers a box on what the page does not need now, and none on what it loads', async () => {
      const GUESS: ShareFileEntry = { path: 'results/c.json', size: 2, reason: 'script', optional: true };
      const OLD: ShareFileEntry = { path: 'results/old.csv', size: 1, reason: 'loaded', optional: true };
      const NOW: ShareFileEntry = { path: 'results/now.csv', size: 1, reason: 'loaded', optional: false };
      mockStatus.mockResolvedValue(status([ENTRY_ROW, GUESS, OLD, NOW]));
      renderDialog();

      await screen.findByText('now.csv');
      expect(screen.getByRole('checkbox', { name: 'Include c.json' })).toBeChecked();
      expect(screen.getByRole('checkbox', { name: 'Include old.csv' })).toBeChecked();
      expect(screen.queryByRole('checkbox', { name: 'Include now.csv' })).toBeNull();
    });

    // A library file left out while the computer is stopped: the stored copy
    // never keeps it, so nothing can say its size.
    it('shows a left-out row with no size where nothing could read one', async () => {
      const LIB: ShareFileEntry = { path: 'results/vendor/lib.js', size: null, reason: 'script', optional: true };
      mockStatus.mockResolvedValue(status([ENTRY_ROW], { left_out: [LIB] }));
      renderDialog();

      const row = (await screen.findByText('lib.js')).closest('li');
      expect(row?.textContent).toContain('left out');
      expect(row?.textContent).not.toMatch(/\d\s*(B|KB|MB)/);
      expect(screen.getByRole('checkbox', { name: 'Include lib.js' })).not.toBeChecked();
    });

    it('lists the files past a cap, holds Publish, and takes off what puts it over', async () => {
      const RAW: ShareFileEntry = { path: 'results/raw.zip', size: 40_000_000, reason: 'link', optional: true };
      mockStatus.mockResolvedValue(status([ENTRY_ROW, RAW], {
        over_cap: { code: 'too_many_bytes', limit: 25 * 1024 * 1024 },
        live_sha256: null,
        page_sha256: null,
      }));
      vi.mocked(setArtifactFileLeftOut).mockResolvedValue(artifact());
      renderDialog();

      await screen.findByText('raw.zip');
      expect(screen.getByText(/which is more than a link can carry\. Leave out files below until it fits\./)).toBeInTheDocument();
      expect(publishButton()).toBeDisabled();

      mockStatus.mockResolvedValue(status([ENTRY_ROW], { left_out: [RAW] }));
      await userEvent.click(screen.getByRole('checkbox', { name: 'Include raw.zip' }));

      expect(setArtifactFileLeftOut).toHaveBeenCalledWith(artifact().artifact_id, RAW.path, true);
      await waitFor(() => expect(publishButton()).toBeEnabled());
      expect(screen.queryByText(/Leave out files below/)).toBeNull();
    });

    it('warns of files the page loads from the site root, which no link serves', async () => {
      mockStatus.mockResolvedValue(status([ENTRY_ROW], { root_refs: ['/assets/index-4f2a.js', '/assets/index.css'] }));
      renderDialog();

      const warning = await screen.findByText(/This page loads files from the site root/);
      expect(warning).toHaveTextContent(
        'This page loads files from the site root, such as /assets/index-4f2a.js, which visitors won\'t get. Build it with relative paths, for example with vite build --base=./.',
      );
      expect(within(warning).getByText('vite build --base=./').tagName).toBe('CODE');
      // A warning, not a refusal.
      await waitFor(() => expect(publishButton()).toBeEnabled());
    });

    it('says nothing about leaving out where no file past a cap has a box', async () => {
      mockStatus.mockResolvedValue(status([ENTRY_ROW, STYLE_ROW], {
        over_cap: { code: 'too_many_files', limit: 200 },
        live_sha256: null,
        page_sha256: null,
      }));
      renderDialog();

      await screen.findByText(/references more than 200 files/);
      expect(screen.queryByText(/Leave out files below/)).toBeNull();
      expect(publishButton()).toBeDisabled();
    });

    it('holds a box still while its own change is on its way', async () => {
      const LINKED: ShareFileEntry = { path: 'results/other.html', size: 100, reason: 'link', optional: true };
      mockStatus.mockResolvedValue(status([ENTRY_ROW, LINKED]));
      vi.mocked(setArtifactFileLeftOut).mockReturnValue(new Promise(() => {}));
      renderDialog();

      const box = await screen.findByRole('checkbox', { name: 'Include other.html' });
      await userEvent.click(box);
      await waitFor(() => expect(box).toBeDisabled());
      await userEvent.click(box);
      expect(setArtifactFileLeftOut).toHaveBeenCalledTimes(1);
    });

    it('says to start the computer when only it has the files a page uses', async () => {
      mockStatus.mockRejectedValue(apiError(409, { code: 'needs_computer' }));
      renderDialog();

      expect(await screen.findByText(
        'This page uses files that are only on the running computer. Start the computer, then publish.',
      )).toBeInTheDocument();
      expect(publishButton()).toBeDisabled();
    });

    it('lets the owner take a linked page off the list, and only a linked page', async () => {
      const LINKED: ShareFileEntry = { path: 'results/other.html', size: 100, reason: 'link', optional: true };
      mockStatus.mockResolvedValue(status([ENTRY_ROW, STYLE_ROW, LINKED]));
      vi.mocked(setArtifactFileLeftOut).mockResolvedValue(artifact());
      renderDialog();

      await screen.findByText('other.html');
      // The entry and what the page loads have no box.
      expect(within(filesList(3)).getAllByRole('checkbox')).toHaveLength(1);
      const box = screen.getByRole('checkbox', { name: 'Include other.html' });
      expect(box).toBeChecked();

      mockStatus.mockResolvedValue(status([ENTRY_ROW, STYLE_ROW], { left_out: [LINKED] }));
      await userEvent.click(box);

      expect(setArtifactFileLeftOut).toHaveBeenCalledWith(artifact().artifact_id, LINKED.path, true);
      // The list says so, in the same place, and a publish sends only what stays.
      await within(filesList(2)).findByText('left out');
      expect(screen.getByRole('checkbox', { name: 'Include other.html' })).not.toBeChecked();
      expect(within(filesList(2)).getAllByRole('listitem').map((row) => row.textContent)).toEqual([
        expect.stringContaining('report.html'),
        expect.stringContaining('other.html'),
        expect.stringContaining('style.css'),
      ]);
      // DALTON-HOOK(monitors): publishMonitor receives reviewed file list without opted-out linked file
      await waitFor(() => expect(publishButton()).toBeEnabled());
      await userEvent.click(publishButton());
      await waitFor(() => expect(mockPublishMonitor).toHaveBeenCalled());
      expect(mockPublishMonitor.mock.calls[0][0].files).toEqual([ENTRY_ROW.path, STYLE_ROW.path]);
    });

    it('holds Publish while a file is coming off the list', async () => {
      const LINKED: ShareFileEntry = { path: 'results/other.html', size: 100, reason: 'link', optional: true };
      mockStatus.mockResolvedValue(status([ENTRY_ROW, LINKED]));
      let finish = (_: Artifact) => {};
      vi.mocked(setArtifactFileLeftOut).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
      renderDialog();

      await screen.findByText('other.html');
      await waitFor(() => expect(publishButton()).toBeEnabled());
      await userEvent.click(screen.getByRole('checkbox', { name: 'Include other.html' }));

      // Already says what was asked, and a publish now could carry the old list.
      expect(screen.getByRole('checkbox', { name: 'Include other.html' })).not.toBeChecked();
      expect(publishButton()).toBeDisabled();

      await act(async () => { finish(artifact()); });
    });

    it('tells a keyboard or a pointer the whole path of a row and why it goes along', async () => {
      mockStatus.mockResolvedValue(status([ENTRY_ROW, { ...CHART_ROW, reason: 'link', optional: true }]));
      renderDialog();
      await screen.findByText('rev.png');
      const list = filesList(2);
      const row = within(list).getByText('rev.png').closest('li')!;
      expect(within(row).getByText('linked by page')).toBeInTheDocument();
      expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

      await userEvent.tab();
      row.focus();

      const tip = await screen.findByRole('tooltip');
      expect(tip).toHaveTextContent(CHART_ROW.path);
      expect(tip).toHaveTextContent('The page links to it');
    });

    it('shows the link, Private, and the files a publish would carry with each reason', async () => {
      renderDialog();

      expect(await screen.findByText(HREF)).toBeInTheDocument();
      expect(screen.getByText('Publish report.html')).toBeInTheDocument();
      expect(screen.getByText('Private')).toBeInTheDocument();
      // One get-or-create, and no read by path beside it.
      expect(mockEnsure).toHaveBeenCalledTimes(1);
      expect(mockEnsure).toHaveBeenCalledWith(WS, ENTRY);
      expect(mockFind).not.toHaveBeenCalled();

      expect(await screen.findByText('Publishing shares 2 files')).toBeInTheDocument();
      const rows = within(filesList(2)).getAllByRole('listitem');
      expect(rows).toHaveLength(2);
      expect(within(rows[0]).getByText('report.html')).toBeInTheDocument();
      expect(within(rows[0]).getByText('this file')).toBeInTheDocument();
      expect(within(rows[1]).getByText('in CSS')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Unshare' })).not.toBeInTheDocument();
      expect(screen.queryByText('View as visitor')).not.toBeInTheDocument();
    });

    // DALTON-HOOK(monitors): publish asserts publishMonitor payload and monitor permalink
    it('Publish sends exactly the reviewed list, the trimmed note and keep updated', async () => {
      mockPublishMonitor.mockImplementation(answers(
        artifact({ shared: false, latest_seq: 1, keep_updated: true }),
        { version: version(1, { label: 'First cut' }) },
      ));
      const user = userEvent.setup();
      renderDialog();

      await screen.findByText('Publishing shares 2 files');
      await waitFor(() => expect(publishButton()).toBeEnabled());
      await user.type(screen.getByPlaceholderText('Optional, shown in the version list'), '  First cut  ');
      await user.click(screen.getByRole('switch', { name: 'Auto-update when files change' }));
      // Nothing is sent until Publish: the switch is part of the publish.
      expect(mockPublishMonitor).not.toHaveBeenCalled();
      await user.click(publishButton());

      expect(mockPublishMonitor).toHaveBeenCalledTimes(1);
      expect(mockPublishMonitor).toHaveBeenCalledWith({
        workspace_id: WS,
        path: ENTRY,
        title: 'report',
        category: 'other',
        visibility: 'own',
        member_user_ids: [],
        keep_updated: true,
        label: 'First cut',
        files: [ENTRY, STYLE_ROW.path],
        loaded: [],
        earlier: [],
        page_sha256: 'c'.repeat(64),
      });
      expect(await screen.findByText('Published v1')).toBeInTheDocument();
      expect(updateButton()).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Open in new window' })).toHaveAttribute(
        'href',
        `${window.location.origin}/monitors/report`,
      );
    });

    // DALTON-HOOK(monitors): default publishMonitor submission with empty note and auto-update off
    it('an empty note sends null, and keep updated starts off', async () => {
      mockPublishMonitor.mockImplementation(answers(artifact({ shared: false, latest_seq: 1 })));
      const user = userEvent.setup();
      renderDialog();

      await waitFor(() => expect(publishButton()).toBeEnabled());
      expect(screen.getByRole('switch', { name: 'Auto-update when files change' })).toHaveAttribute('aria-checked', 'false');
      await user.type(screen.getByPlaceholderText('Optional, shown in the version list'), '   ');
      await user.click(publishButton());

      expect(mockPublishMonitor).toHaveBeenCalledWith({
        workspace_id: WS,
        path: ENTRY,
        title: 'report',
        category: 'other',
        visibility: 'own',
        member_user_ids: [],
        keep_updated: false,
        label: null,
        files: [ENTRY, STYLE_ROW.path],
        loaded: [],
        earlier: [],
        page_sha256: 'c'.repeat(64),
      });
    });
  });

  it('starts every open afresh: a note left behind is gone', async () => {
    serve(artifact());
    mockStatus.mockResolvedValue(status([ENTRY_ROW]));
    const user = userEvent.setup();
    const dialog = (open: boolean) => <PublishDialog open={open} workspaceId={WS} path={ENTRY} onClose={() => {}} />;
    const { rerender } = renderWithProviders(dialog(true));

    await user.type(await screen.findByPlaceholderText('Optional, shown in the version list'), 'draft');
    rerender(dialog(false));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    rerender(dialog(true));

    expect(await screen.findByPlaceholderText('Optional, shown in the version list')).toHaveValue('');
  });

  it('makes the record of a file never recorded, then lists it', async () => {
    mockFind.mockResolvedValue(null);
    mockEnsure.mockResolvedValue(artifact());
    mockStatus.mockResolvedValue(status([ENTRY_ROW]));
    renderDialog('/home/workspace/results/report.html');

    expect(await screen.findByText('Publishing shares 1 file')).toBeInTheDocument();
    expect(mockEnsure).toHaveBeenCalledTimes(1);
    expect(mockEnsure).toHaveBeenCalledWith(WS, ENTRY);
    expect(screen.getByText(HREF)).toBeInTheDocument();
  });

  // DALTON-HOOK(monitors): 409 files_changed conflict prompts owner review before second publishMonitor submit
  it('a 409 files_changed shows the new list and waits for a second confirm', async () => {
    serve(artifact());
    const serverList = [ENTRY_ROW, STYLE_ROW, CHART_ROW];
    mockStatus
      .mockResolvedValueOnce(status([ENTRY_ROW, STYLE_ROW]))
      .mockResolvedValue(status(serverList));
    mockPublishMonitor
      .mockRejectedValueOnce(apiError(409, { code: 'files_changed', files: serverList }))
      .mockImplementationOnce(answers(artifact({ shared: false, latest_seq: 1 })));
    const user = userEvent.setup();
    renderDialog();

    await screen.findByText('Publishing shares 2 files');
    await waitFor(() => expect(publishButton()).toBeEnabled());
    await user.click(publishButton());

    expect(await screen.findByRole('status')).toHaveTextContent(
      'The file changed while this was open. Review the list and publish again.',
    );
    // The list is the server's now, and nothing pretended the publish went through.
    expect(await screen.findByText('Publishing shares 3 files')).toBeInTheDocument();
    expect(screen.getByText('rev.png')).toBeInTheDocument();
    expect(screen.getByText('Private')).toBeInTheDocument();
    expect(mockPublishMonitor).toHaveBeenCalledTimes(1);
    expect(toast).not.toHaveBeenCalled();

    // The second attempt confirms the list the owner was just shown.
    await waitFor(() => expect(publishButton()).toBeEnabled());
    await user.click(publishButton());
    expect(mockPublishMonitor).toHaveBeenCalledTimes(2);
    expect(mockPublishMonitor).toHaveBeenLastCalledWith({
      workspace_id: WS,
      path: ENTRY,
      title: 'report',
      category: 'other',
      visibility: 'own',
      member_user_ids: [],
      keep_updated: false,
      label: null,
      files: serverList.map((f) => f.path),
      loaded: [],
      earlier: [],
      page_sha256: 'c'.repeat(64),
    });
    expect(await screen.findByText('Published v1')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  describe('a published file', () => {
    // DALTON-HOOK(monitors): keep unshare test on non-monitor legacy shared files
    it('Unshare stops the link and keeps the versions', async () => {
      serve(artifact({ shared: true, shared_at: '2026-10-31T12:00:00Z', latest_seq: 2 }));
      mockStatus.mockResolvedValue(status([ENTRY_ROW, STYLE_ROW], { latest_seq: 2, changed: false, drift: NO_DRIFT }));
      mockVersions.mockResolvedValue([version(2, { label: 'Q3 numbers' }), version(1)]);
      mockUnshare.mockImplementation(async () => {
        const next = artifact({ latest_seq: 2 });
        serve(next);
        return next;
      });
      const user = userEvent.setup();
      renderDialog();

      expect(await screen.findByText('Published v2')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Unshare' }));

      expect(mockUnshare).toHaveBeenCalledWith(ID);
      expect(await screen.findByText('Private')).toBeInTheDocument();
      expect(screen.getByText('Not shared. Its versions are kept, and publishing again uses the same link.')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Unshare' })).not.toBeInTheDocument();
      expect(screen.getByText('Q3 numbers')).toBeInTheDocument();
      expect(mockPublishMonitor).not.toHaveBeenCalled();
    });

    it('lists the versions newest first, marking the ones keep updated cut', async () => {
      serve(artifact({ shared: true, latest_seq: 3, keep_updated: true }));
      mockStatus.mockResolvedValue(status([ENTRY_ROW], { latest_seq: 3, changed: false, drift: NO_DRIFT }));
      mockVersions.mockResolvedValue([
        version(3, { origin: 'keep_updated' }),
        version(2, { label: 'Q3 numbers' }),
        version(1),
      ]);
      renderDialog();

      const list = await screen.findByRole('list', { name: 'Versions' });
      await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(3));
      const rows = within(list).getAllByRole('listitem');
      expect(within(rows[0]).getByText('v3')).toBeInTheDocument();
      expect(within(rows[0]).getByText('kept updated')).toBeInTheDocument();
      expect(within(rows[0]).getByText('Oct 31')).toBeInTheDocument();
      expect(within(rows[1]).getByText('Q3 numbers')).toBeInTheDocument();
      expect(within(list).getAllByText('kept updated')).toHaveLength(1);
      // DALTON-HOOK(monitors): Auto-update toggle and drift update submit through publishMonitor
      expect(screen.getByRole('switch', { name: 'Auto-update when files change' })).toHaveAttribute('aria-checked', 'true');
    });

    it('files the entry newly reads wait for Update: a notice, the rows marked, and Update carries them', async () => {
      serve(artifact({ shared: true, latest_seq: 1 }));
      mockStatus.mockResolvedValue(status([ENTRY_ROW, STYLE_ROW, CHART_ROW], {
        latest_seq: 1,
        changed: true,
        drift: { added: [CHART_ROW.path], removed: [], changed: [] },
      }));
      mockPublishMonitor.mockImplementation(answers(artifact({ shared: false, latest_seq: 2 })));
      const user = userEvent.setup();
      renderDialog();

      expect(await screen.findByText(
        "1 new file waits for Update. Visitors won't get it until you update.",
      )).toBeInTheDocument();
      expect(screen.getByText('Changed since v1. Visitors see v1 until you update.')).toBeInTheDocument();
      expect(screen.getByText('Updating shares 3 files')).toBeInTheDocument();
      const rows = within(filesList(3)).getAllByRole('listitem');
      const added = rows.find((row) => within(row).queryByText('rev.png'))!;
      expect(within(added).getByText('not shared yet')).toBeInTheDocument();
      // Only the added row carries the mark; the others keep their reason.
      expect(screen.getAllByText('not shared yet')).toHaveLength(1);
      expect(within(rows[0]).getByText('this file')).toBeInTheDocument();

      await waitFor(() => expect(publishButton()).toBeEnabled());
      await user.click(publishButton());
      expect(mockPublishMonitor).toHaveBeenCalledWith({
        workspace_id: WS,
        path: ENTRY,
        title: 'report',
        category: 'other',
        visibility: 'own',
        member_user_ids: [],
        keep_updated: false,
        label: null,
        files: [ENTRY, STYLE_ROW.path, CHART_ROW.path],
        loaded: [],
        earlier: [],
        page_sha256: 'c'.repeat(64),
      });
    });
  });

  describe('answers that are not a new version', () => {
    // DALTON-HOOK(monitors): verify publishMonitor unchanged/cap/missing/loaded/failure answers
    it('a publish that cut nothing says so quietly', async () => {
      // Shared before and stopped: publishing again resumes at the latest version.
      serve(artifact({ latest_seq: 2 }));
      mockStatus.mockResolvedValue(status([ENTRY_ROW], { latest_seq: 2, changed: false }));
      mockPublishMonitor.mockImplementation(answers(artifact({ shared: false, latest_seq: 2 }), { cut: false }));
      const user = userEvent.setup();
      renderDialog();

      await waitFor(() => expect(publishButton()).toBeEnabled());
      await user.click(publishButton());

      expect(await screen.findByRole('status')).toHaveTextContent('Nothing changed since v2, so no new version was made.');
      expect(screen.getByText('Published v2')).toBeInTheDocument();
      expect(toast).not.toHaveBeenCalled();
    });

    it('a 422 cap on the status names it and holds Publish', async () => {
      serve(artifact());
      mockStatus.mockRejectedValue(apiError(422, { code: 'too_many_bytes', limit: 256 * 1024 * 1024 }));
      renderDialog();

      expect(await screen.findByText(
        'The files this one references add up to more than 256 MB, which is more than a link can carry.',
      )).toHaveAttribute('role', 'status');
      expect(screen.queryByRole('list')).not.toBeInTheDocument();
      expect(publishButton()).toBeDisabled();
    });

    it('a 404 file_missing on publish says the file is gone', async () => {
      serve(artifact());
      mockStatus.mockResolvedValue(status([ENTRY_ROW]));
      mockPublishMonitor.mockRejectedValue(apiError(404, { code: 'file_missing' }));
      const user = userEvent.setup();
      renderDialog();

      await waitFor(() => expect(publishButton()).toBeEnabled());
      await user.click(publishButton());

      expect(await screen.findByText('This file is no longer in the workspace.')).toHaveAttribute('role', 'status');
      expect(toast).not.toHaveBeenCalled();
    });

    // DALTON-HOOK(monitors): publishMonitor forwards loaded files and failure status handling
    it('reads the list with what the page reported, and sends that same report', async () => {
      const frame = {};
      notePageOpen(WS, ENTRY, frame);
      notePageLoads(WS, ENTRY, ['data/built-in-code.csv'], frame);
      serve(artifact());
      mockStatus.mockResolvedValue(status([ENTRY_ROW]));
      mockPublishMonitor.mockImplementation(answers(artifact({ shared: false, latest_seq: 1 })));
      const user = userEvent.setup();
      renderDialog();

      await screen.findByText('report.html');
      await waitFor(() => expect(publishButton()).toBeEnabled());
      await user.click(publishButton());

      expect(mockStatus).toHaveBeenCalledWith(ID, { now: ['data/built-in-code.csv'], earlier: [] });
      expect(mockPublishMonitor.mock.calls[0][0].loaded).toEqual(['data/built-in-code.csv']);
      // With the page's code it was read under, which the server checks.
      expect(mockPublishMonitor.mock.calls[0][0].page_sha256).toBe('c'.repeat(64));
    });

    it('reads the list before the page loads and again after, and only then lets it go out', async () => {
      serve(artifact());
      mockStatus.mockResolvedValue(status([ENTRY_ROW]));
      scout.hold = true;
      try {
        renderDialog();

        // The first read names the code the page is about to run.
        await screen.findByText('report.html');
        await waitFor(() => expect(scout.settle).not.toBeNull());
        expect(mockStatus).toHaveBeenCalledTimes(1);
        expect(publishButton()).toBeDisabled();
        act(() => scout.settle?.());
        await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(2));
        await waitFor(() => expect(publishButton()).toBeEnabled());
      } finally {
        scout.hold = false;
        scout.settle = null;
      }
    });

    it('holds Publish when the page could not run, and runs it again on Retry', async () => {
      serve(artifact());
      mockStatus.mockResolvedValue(status([ENTRY_ROW]));
      scout.grantFails = true;
      try {
        const user = userEvent.setup();
        renderDialog();

        // Published from its source alone, a file its code builds would be missing.
        expect(await screen.findByText("The file can't be read right now. Try again shortly.")).toBeInTheDocument();
        expect(publishButton()).toBeDisabled();

        scout.grantFails = false;
        await user.click(screen.getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(publishButton()).toBeEnabled());
        expect(screen.queryByText("The file can't be read right now. Try again shortly.")).not.toBeInTheDocument();
      } finally {
        scout.grantFails = false;
      }
    });

    // DALTON-HOOK(monitors): unhandled failure on publishMonitor shows toast and stays Private
    it('any other failure toasts and leaves the file private', async () => {
      serve(artifact());
      mockStatus.mockResolvedValue(status([ENTRY_ROW]));
      mockPublishMonitor.mockRejectedValue(apiError(500, 'boom'));
      vi.spyOn(console, 'error').mockImplementation(() => {});
      const user = userEvent.setup();
      renderDialog();

      await waitFor(() => expect(publishButton()).toBeEnabled());
      await user.click(publishButton());

      await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({
        description: "Couldn't publish. Please try again.",
        variant: 'destructive',
      })));
      expect(screen.getByText('Private')).toBeInTheDocument();
    });
  });
});
