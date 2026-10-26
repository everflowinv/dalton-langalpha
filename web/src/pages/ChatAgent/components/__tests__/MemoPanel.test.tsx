import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
// DALTON-HOOK(multi-file-upload): multi-upload tests assert the batch
// invalidates the memo list exactly once, via the injected query client.
import { renderWithProviders, createTestQueryClient } from '@/test/utils';

// ---------------------------------------------------------------------------
// Mocks — keep MemoPanel mountable in jsdom by stubbing heavy deps.
// ---------------------------------------------------------------------------

// DALTON-HOOK(multi-file-upload): passthrough stand-in, but with the real
// summary template baked in for the key whose interpolated content the
// multi-upload tests lock (count, file names, reasons). Keys without an entry
// come through unchanged, as before.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, opts?: Record<string, unknown>) => {
    const templates: Record<string, string> = {
      'memoPanel.errors.uploadFailedSummary': '{{count}} 个文件上传失败：{{details}}',
    };
    const template = templates[key] ?? key;
    if (!opts) return template;
    let out = template;
    for (const [k, v] of Object.entries(opts)) {
      out = out.replace(new RegExp(`{{\\s*${k}\\s*}}`, 'g'), String(v));
    }
    return out;
  } }),
}));

// Stub Markdown — full ESM toolchain is too heavy for jsdom and we just need
// to assert that content reaches it.
vi.mock('../Markdown', () => ({
  default: ({ content }: { content: string }) => (
    <div data-testid="markdown-content">{content}</div>
  ),
}));

// Render Radix Dialog inline (no portal) so Testing Library finds its children.
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open: boolean }) =>
    open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dialog-content">{children}</div>
  ),
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));

vi.mock('@/components/ui/hover-card', () => ({
  HoverCard: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  HoverCardTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  HoverCardContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

// Mock the entire api module so we control list/read/write/delete behavior.
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  listUserMemos: vi.fn(),
  readUserMemo: vi.fn(),
  uploadUserMemo: vi.fn(),
  writeUserMemo: vi.fn(),
  deleteUserMemo: vi.fn(),
  regenerateUserMemo: vi.fn(),
  triggerUserMemoDownload: vi.fn(),
  downloadUserMemoBlobUrl: vi.fn(),
  // workspace listing happens inside MemoPanel via useWorkspaces → the API client.
  // We stub the workspaces hook below; this remains for the api path used
  // by useUserMemoList / useReadUserMemo / etc.
  getWorkspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
}));

// useWorkspaces is fired only when there's at least one sandbox-sourced memo,
// but stub it to keep the test deterministic regardless of fixtures.
vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [] } }),
}));

import {
  listUserMemos,
  readUserMemo,
  uploadUserMemo,
  writeUserMemo,
  deleteUserMemo,
  regenerateUserMemo,
  triggerUserMemoDownload,
  downloadUserMemoBlobUrl,
  type MemoUploadResponse,
} from '@/pages/ChatAgent/utils/api';
import MemoPanel from '../MemoPanel';

const mockList = listUserMemos as Mock;
const mockRead = readUserMemo as Mock;
// DALTON-HOOK(multi-file-upload): typed mock — implementations return a
// MemoUploadResponse, matching the real uploadUserMemo signature.
const mockUpload = vi.mocked(uploadUserMemo);
const uploadResponse = (name: string): MemoUploadResponse =>
  ({ key: name, original_filename: name, metadata_status: 'pending' });
const mockWrite = writeUserMemo as Mock;
const mockDelete = deleteUserMemo as Mock;
const mockRegen = regenerateUserMemo as Mock;
const mockDownload = triggerUserMemoDownload as Mock;
const mockBlobUrl = downloadUserMemoBlobUrl as Mock;

beforeEach(() => {
  vi.clearAllMocks();
  // Default: no memos. Individual tests override.
  mockList.mockResolvedValue({ entries: [], truncated: false });
  // jsdom doesn't ship URL.createObjectURL/revokeObjectURL — the PDF viewer
  // path uses them in cleanup.
  if (!URL.createObjectURL) {
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      writable: true,
      value: vi.fn(() => 'blob:fake-pdf'),
    });
  }
  if (!URL.revokeObjectURL) {
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      writable: true,
      value: vi.fn(),
    });
  }
});

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

describe('MemoPanel — empty state', () => {
  it('renders the empty title when there are no memos', async () => {
    renderWithProviders(<MemoPanel />);

    await waitFor(() =>
      expect(screen.getByText('memoPanel.empty.title')).toBeInTheDocument(),
    );
    expect(screen.getByText('memoPanel.empty.hint')).toBeInTheDocument();
    // Upload button is enabled in empty state
    expect(screen.getByText('memoPanel.uploadButton')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// List rendering with status badges
// ---------------------------------------------------------------------------

describe('MemoPanel — list rendering', () => {
  const baseEntry = {
    key: '',
    original_filename: null,
    mime_type: null,
    size_bytes: 100,
    description: null,
    metadata_status: 'ready' as const,
    created_at: '2026-01-01T00:00:00Z',
    modified_at: null,
    source_kind: null,
    source_workspace_id: null,
    source_path: null,
    sha256: null,
  };

  it('lists memos and shows the "Generating" badge for pending entries', async () => {
    mockList.mockResolvedValue({
      entries: [
        { ...baseEntry, key: 'pending-1', original_filename: 'pending.md', metadata_status: 'pending' },
        { ...baseEntry, key: 'ready-1', original_filename: 'ready.md', metadata_status: 'ready' },
      ],
      truncated: false,
    });

    renderWithProviders(<MemoPanel />);

    await waitFor(() => expect(screen.getByText('pending.md')).toBeInTheDocument());
    expect(screen.getByText('ready.md')).toBeInTheDocument();
    // Both badges visible (translation keys come through unchanged)
    expect(screen.getByText('memoPanel.status.pending')).toBeInTheDocument();
    expect(screen.getByText('memoPanel.status.ready')).toBeInTheDocument();
  });

  it('shows the "Failed" badge when metadata generation failed', async () => {
    mockList.mockResolvedValue({
      entries: [
        { ...baseEntry, key: 'failed-1', original_filename: 'broken.md', metadata_status: 'failed' },
      ],
      truncated: false,
    });

    renderWithProviders(<MemoPanel />);

    await waitFor(() => expect(screen.getByText('broken.md')).toBeInTheDocument());
    expect(screen.getByText('memoPanel.status.failed')).toBeInTheDocument();
  });

  it('leaves the date cell empty for an unparseable created_at', async () => {
    mockList.mockResolvedValue({
      entries: [{ ...baseEntry, key: 'odd-1', original_filename: 'odd.md', created_at: 'not-a-date' }],
      truncated: false,
    });

    renderWithProviders(<MemoPanel />);

    const row = (await screen.findByText('odd.md')).closest('tr');
    expect(row?.textContent).not.toContain('Invalid Date');
    expect(row?.querySelector('td:last-child')?.textContent).toBe('');
  });
});

// ---------------------------------------------------------------------------
// Row click → viewer pane
// ---------------------------------------------------------------------------

describe('MemoPanel — viewer', () => {
  const mdEntry = {
    key: 'note.md',
    original_filename: 'note.md',
    mime_type: 'text/markdown',
    size_bytes: 12,
    description: null,
    metadata_status: 'ready' as const,
    created_at: '2026-01-01T00:00:00Z',
    modified_at: null,
    source_kind: null,
    source_workspace_id: null,
    source_path: null,
    sha256: null,
  };

  it('switches to the viewer when a row is clicked and renders Markdown content', async () => {
    mockList.mockResolvedValue({ entries: [mdEntry], truncated: false });
    mockRead.mockResolvedValue({
      key: 'note.md',
      content: '# Hello memo',
      encoding: 'utf-8',
      mime_type: 'text/markdown',
      size_bytes: 12,
      original_filename: 'note.md',
      description: null,
      summary: null,
      metadata_status: 'ready',
      metadata_error: null,
      created_at: null,
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
    });

    renderWithProviders(<MemoPanel />);
    const user = userEvent.setup();

    const row = await screen.findByText('note.md');
    await user.click(row);

    // Wait for read query to resolve and content to flow through Markdown stub
    const md = await screen.findByTestId('markdown-content');
    expect(md.textContent).toBe('# Hello memo');

    // Viewer header has a back button + delete button
    expect(screen.getByTitle('memoPanel.backToList')).toBeInTheDocument();
    expect(screen.getByTitle('memoPanel.actions.delete')).toBeInTheDocument();
  });

  it('opens delete confirmation when the trash icon is clicked', async () => {
    mockList.mockResolvedValue({ entries: [mdEntry], truncated: false });
    mockRead.mockResolvedValue({
      key: 'note.md',
      content: 'x',
      encoding: 'utf-8',
      mime_type: 'text/markdown',
      size_bytes: 1,
      original_filename: 'note.md',
      description: null,
      summary: null,
      metadata_status: 'ready',
      metadata_error: null,
      created_at: null,
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
    });

    renderWithProviders(<MemoPanel />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('note.md'));
    await user.click(screen.getByTitle('memoPanel.actions.delete'));

    // Dialog now visible
    expect(await screen.findByText('memoPanel.confirm.deleteTitle')).toBeInTheDocument();
    // Body interpolates the file name
    expect(screen.getByText(/note\.md/)).toBeInTheDocument();
  });

  it('confirming delete calls deleteUserMemo with the key', async () => {
    mockList.mockResolvedValue({ entries: [mdEntry], truncated: false });
    mockRead.mockResolvedValue({
      key: 'note.md',
      content: 'x',
      encoding: 'utf-8',
      mime_type: 'text/markdown',
      size_bytes: 1,
      original_filename: 'note.md',
      description: null,
      summary: null,
      metadata_status: 'ready',
      metadata_error: null,
      created_at: null,
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
    });
    mockDelete.mockResolvedValue(undefined);

    renderWithProviders(<MemoPanel />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('note.md'));
    await user.click(screen.getByTitle('memoPanel.actions.delete'));
    await user.click(await screen.findByText('memoPanel.confirm.deleteConfirm'));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('note.md'));
  });

  it('clicking download invokes triggerUserMemoDownload', async () => {
    mockList.mockResolvedValue({ entries: [mdEntry], truncated: false });
    mockRead.mockResolvedValue({
      key: 'note.md',
      content: 'x',
      encoding: 'utf-8',
      mime_type: 'text/markdown',
      size_bytes: 1,
      original_filename: 'note.md',
      description: null,
      summary: null,
      metadata_status: 'ready',
      metadata_error: null,
      created_at: null,
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
    });
    mockDownload.mockResolvedValue(undefined);

    renderWithProviders(<MemoPanel />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('note.md'));
    await user.click(screen.getByTitle('memoPanel.actions.download'));

    expect(mockDownload).toHaveBeenCalledWith('note.md', 'note.md');
  });
});

// ---------------------------------------------------------------------------
// Failed status → regenerate flow
// ---------------------------------------------------------------------------

describe('MemoPanel — regenerate on failed memo', () => {
  it('shows a regenerate button in viewer header that triggers regenerateUserMemo', async () => {
    const failedEntry = {
      key: 'broken.md',
      original_filename: 'broken.md',
      mime_type: 'text/markdown',
      size_bytes: 12,
      description: null,
      metadata_status: 'failed' as const,
      created_at: '2026-01-01T00:00:00Z',
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
      sha256: null,
    };
    mockList.mockResolvedValue({ entries: [failedEntry], truncated: false });
    mockRead.mockResolvedValue({
      key: 'broken.md',
      content: '',
      encoding: 'utf-8',
      mime_type: 'text/markdown',
      size_bytes: 0,
      original_filename: 'broken.md',
      description: null,
      summary: null,
      metadata_status: 'failed',
      metadata_error: 'boom',
      created_at: null,
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
    });
    mockRegen.mockResolvedValue({
      key: 'broken.md',
      original_filename: 'broken.md',
      metadata_status: 'pending',
    });

    renderWithProviders(<MemoPanel />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('broken.md'));
    const regenBtn = await screen.findByTitle('memoPanel.actions.regenerate');
    await user.click(regenBtn);

    expect(mockRegen).toHaveBeenCalledWith('broken.md');
  });
});

// ---------------------------------------------------------------------------
// Edit → save flow for a markdown memo
// ---------------------------------------------------------------------------

describe('MemoPanel — edit + save', () => {
  it('save calls writeUserMemo with the edited content', async () => {
    const entry = {
      key: 'note.md',
      original_filename: 'note.md',
      mime_type: 'text/markdown',
      size_bytes: 5,
      description: null,
      metadata_status: 'ready' as const,
      created_at: '2026-01-01T00:00:00Z',
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
      sha256: null,
    };
    mockList.mockResolvedValue({ entries: [entry], truncated: false });
    mockRead.mockResolvedValue({
      key: 'note.md',
      content: 'old',
      encoding: 'utf-8',
      mime_type: 'text/markdown',
      size_bytes: 3,
      original_filename: 'note.md',
      description: null,
      summary: null,
      metadata_status: 'ready',
      metadata_error: null,
      created_at: null,
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
    });
    mockWrite.mockResolvedValue({
      key: 'note.md',
      original_filename: 'note.md',
      metadata_status: 'pending',
    });

    renderWithProviders(<MemoPanel />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('note.md'));
    // Wait for read to populate so the Edit button enables
    await screen.findByTestId('markdown-content');

    await user.click(screen.getByTitle('memoPanel.actions.edit'));

    // textarea now visible — replace with new content
    const textarea = await screen.findByDisplayValue('old');
    await user.clear(textarea);
    await user.type(textarea, 'new!');

    await user.click(screen.getByText('memoPanel.actions.save'));

    await waitFor(() =>
      expect(mockWrite).toHaveBeenCalledWith('note.md', 'new!'),
    );
  });
});

// ---------------------------------------------------------------------------
// Drag-and-drop upload
// ---------------------------------------------------------------------------

describe('MemoPanel — drag-and-drop upload', () => {
  it('drops a markdown file → calls uploadUserMemo with that file', async () => {
    mockList.mockResolvedValue({ entries: [], truncated: false });
    mockUpload.mockResolvedValue({
      key: 'dropped.md',
      original_filename: 'dropped.md',
      metadata_status: 'pending',
    });

    const { container } = renderWithProviders(<MemoPanel />);

    await waitFor(() =>
      expect(screen.getByText('memoPanel.empty.title')).toBeInTheDocument(),
    );

    const dropTarget = container.firstChild as HTMLElement;
    expect(dropTarget).toBeTruthy();

    const file = new File(['# hi'], 'dropped.md', { type: 'text/markdown' });

    // The drag handlers gate on dataTransfer.types containing 'Files'.
    fireEvent.dragEnter(dropTarget, {
      dataTransfer: { types: ['Files'], files: [file] },
    });
    fireEvent.drop(dropTarget, {
      dataTransfer: { types: ['Files'], files: [file] },
    });

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));
    // DALTON-HOOK(multi-file-upload): doUpload calls uploadUserMemo directly
    // (file, onProgress) so the batch can invalidate the list once at the end.
    expect(mockUpload).toHaveBeenCalledWith(file, expect.anything());
  });

  it('rejects unsupported file types with an inline error', async () => {
    mockList.mockResolvedValue({ entries: [], truncated: false });

    const { container } = renderWithProviders(<MemoPanel />);
    await waitFor(() =>
      expect(screen.getByText('memoPanel.empty.title')).toBeInTheDocument(),
    );

    const dropTarget = container.firstChild as HTMLElement;
    const badFile = new File(['x'], 'image.png', { type: 'image/png' });

    fireEvent.dragEnter(dropTarget, {
      dataTransfer: { types: ['Files'], files: [badFile] },
    });
    fireEvent.drop(dropTarget, {
      dataTransfer: { types: ['Files'], files: [badFile] },
    });

    // DALTON-HOOK(multi-file-upload): rejections are collected per file and
    // surface as one summary strip naming the file and its reason.
    expect(
      await screen.findByText('1 个文件上传失败：image.png (memoPanel.errors.unsupportedType)'),
    ).toBeInTheDocument();
    expect(mockUpload).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// DALTON-HOOK(multi-file-upload): serial multi-file upload — per-file
// failures, one list refresh per batch.
// ---------------------------------------------------------------------------

describe('MemoPanel — multi-file upload', () => {
  it('drops several files → uploads each serially and refreshes the memo list once', async () => {
    mockList.mockResolvedValue({ entries: [], truncated: false });
    mockUpload.mockImplementation(async () => uploadResponse('queued.md'));

    const client = createTestQueryClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { container } = renderWithProviders(<MemoPanel />, { queryClient: client });

    await waitFor(() =>
      expect(screen.getByText('memoPanel.empty.title')).toBeInTheDocument(),
    );

    const dropTarget = container.firstChild as HTMLElement;
    const files = [
      new File(['a'], 'a.md', { type: 'text/markdown' }),
      new File(['b'], 'b.md', { type: 'text/markdown' }),
      new File(['c'], 'c.md', { type: 'text/markdown' }),
    ];

    fireEvent.drop(dropTarget, {
      dataTransfer: { types: ['Files'], files },
    });

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(3));
    // The invalidate fires once after the whole batch settles — waiting for it
    // here also guarantees the batch has finished before the count is read.
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(1));
    // No failure summary when everything went up.
    expect(screen.queryByText('memoPanel.errors.uploadFailedSummary')).not.toBeInTheDocument();
  });

  it('a rejected file does not stop the batch; failures are summarised per file', async () => {
    mockList.mockResolvedValue({ entries: [], truncated: false });
    mockUpload.mockImplementation(async (file: File) => {
      if (file.name === 'broken.md') throw new Error('boom');
      return uploadResponse(file.name);
    });

    const client = createTestQueryClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { container } = renderWithProviders(<MemoPanel />, { queryClient: client });

    await waitFor(() =>
      expect(screen.getByText('memoPanel.empty.title')).toBeInTheDocument(),
    );

    const dropTarget = container.firstChild as HTMLElement;
    fireEvent.drop(dropTarget, {
      dataTransfer: {
        types: ['Files'],
        files: [
          new File(['1'], 'first.md', { type: 'text/markdown' }),
          new File(['2'], 'broken.md', { type: 'text/markdown' }),
          new File(['3'], 'last.md', { type: 'text/markdown' }),
        ],
      },
    });

    // The failed file is not the end of the batch.
    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(3));
    // The summary locks its content: count, file name, and the raw error.
    expect(
      await screen.findByText('1 个文件上传失败：broken.md (boom)'),
    ).toBeInTheDocument();
    // The summary only renders after the loop, so the batch's single list
    // invalidation has already happened by now.
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
  });

  it('uploads stay serial: the second file only starts after the first resolves', async () => {
    mockList.mockResolvedValue({ entries: [], truncated: false });
    let active = 0;
    let maxConcurrent = 0;
    mockUpload.mockImplementation(async () => {
      active++;
      maxConcurrent = Math.max(maxConcurrent, active);
      await new Promise((resolve) => setTimeout(resolve, 0));
      active--;
      return uploadResponse('queued.md');
    });

    const client = createTestQueryClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { container } = renderWithProviders(<MemoPanel />, { queryClient: client });

    await waitFor(() =>
      expect(screen.getByText('memoPanel.empty.title')).toBeInTheDocument(),
    );

    const dropTarget = container.firstChild as HTMLElement;
    fireEvent.drop(dropTarget, {
      dataTransfer: {
        types: ['Files'],
        files: [
          new File(['1'], 'one.md', { type: 'text/markdown' }),
          new File(['2'], 'two.md', { type: 'text/markdown' }),
        ],
      },
    });

    // Both files went up, and the batch's single invalidation (which only
    // fires after the loop) means the concurrency peak is final.
    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(1));
    expect(maxConcurrent).toBe(1);
  });

  // DALTON-HOOK(multi-file-upload): unmount drops queued files; the in-flight
  // upload finishes on the wire but writes no state and invalidates nothing.
  it('unmounting mid-upload drops queued files and never invalidates the list', async () => {
    mockList.mockResolvedValue({ entries: [], truncated: false });
    let release: () => void = () => {};
    mockUpload.mockImplementation(() => new Promise<MemoUploadResponse>((resolve) => {
      release = () => resolve(uploadResponse('one.md'));
    }));

    const client = createTestQueryClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { container, unmount } = renderWithProviders(<MemoPanel />, { queryClient: client });

    await waitFor(() =>
      expect(screen.getByText('memoPanel.empty.title')).toBeInTheDocument(),
    );

    const dropTarget = container.firstChild as HTMLElement;
    fireEvent.drop(dropTarget, {
      dataTransfer: {
        types: ['Files'],
        files: [
          new File(['1'], 'one.md', { type: 'text/markdown' }),
          new File(['2'], 'two.md', { type: 'text/markdown' }),
        ],
      },
    });
    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));

    unmount();
    release();
    // Let the in-flight upload's resolution reach the now-guarded loop.
    await new Promise((resolve) => setTimeout(resolve, 0));

    // two.md never started; one.md's late success invalidated nothing.
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// PDF viewer — uses object embed via blob URL
// ---------------------------------------------------------------------------

describe('MemoPanel — PDF viewer', () => {
  it('fetches a blob URL and embeds it in an <object> tag', async () => {
    const pdfEntry = {
      key: 'doc.pdf',
      original_filename: 'doc.pdf',
      mime_type: 'application/pdf',
      size_bytes: 999,
      description: null,
      metadata_status: 'ready' as const,
      created_at: '2026-01-01T00:00:00Z',
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
      sha256: null,
    };
    mockList.mockResolvedValue({ entries: [pdfEntry], truncated: false });
    mockRead.mockResolvedValue({
      key: 'doc.pdf',
      content: '',
      encoding: 'binary',
      mime_type: 'application/pdf',
      size_bytes: 999,
      original_filename: 'doc.pdf',
      description: null,
      summary: null,
      metadata_status: 'ready',
      metadata_error: null,
      created_at: null,
      modified_at: null,
      source_kind: null,
      source_workspace_id: null,
      source_path: null,
    });
    mockBlobUrl.mockResolvedValue('blob:fake-pdf');

    const { container } = renderWithProviders(<MemoPanel />);
    const user = userEvent.setup();

    await user.click(await screen.findByText('doc.pdf'));

    await waitFor(() => expect(mockBlobUrl).toHaveBeenCalledWith('doc.pdf'));

    await waitFor(() => {
      const obj = container.querySelector('object');
      expect(obj).toBeTruthy();
      expect(obj?.getAttribute('data')).toBe('blob:fake-pdf');
      expect(obj?.getAttribute('type')).toBe('application/pdf');
    });
  });
});
