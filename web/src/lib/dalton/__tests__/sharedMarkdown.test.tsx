import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '@/test/utils';
import { buildServeUrl, versionPrefix } from '@/pages/ChatAgent/components/viewers/html/wsfilesUrl';
import SharedPageView from '@/pages/SharedChat/SharedPageView';
import type { SharedPublicFileMetadata } from '@/pages/SharedChat/api';
import { isMarkdownFile, resolveSharedImageCandidates, resolveSharedRelativePath } from '../sharedMarkdown';

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}));

const CODE = 'mdshare12345';
const FRAME_BASE = '/api/v1/dalton/monitors/g/d1.signed-grant/';
const SEQ = 2;
const PREFIX = versionPrefix(FRAME_BASE, SEQ);

function makeVisitorMetadata(path: string, overrides: Partial<SharedPublicFileMetadata> = {}): SharedPublicFileMetadata {
  return {
    kind: 'file',
    access: 'public',
    audience: 'organization',
    artifact_id: '71000000-0000-4000-8000-000000000901',
    name: path.split('/').pop() || path,
    path,
    title: null,
    seq: SEQ,
    frame_base: FRAME_BASE,
    isolated: false,
    expires_in: 12 * 60 * 60,
    ...overrides,
  };
}

function mockServedFiles(files: Record<string, { body: string; contentType: string }>) {
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    const hit = Object.entries(files).find(([filePath]) => url === buildServeUrl(PREFIX, filePath));
    if (!hit) {
      return {
        ok: false,
        status: 404,
        headers: new Headers(),
      } as Response;
    }
    const [, entry] = hit;
    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': entry.contentType }),
      text: async () => entry.body,
      blob: async () => new Blob([entry.body], { type: entry.contentType }),
      arrayBuffer: async () => new TextEncoder().encode(entry.body).buffer,
    } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('sharedMarkdown helpers', () => {
  it('identifies .md and .markdown files case-insensitively', () => {
    expect(isMarkdownFile('notes/report.md')).toBe(true);
    expect(isMarkdownFile('notes/README.MD')).toBe(true);
    expect(isMarkdownFile('notes/guide.markdown')).toBe(true);
    expect(isMarkdownFile('notes/plain.txt')).toBe(false);
    expect(isMarkdownFile('notes/page.html')).toBe(false);
    expect(isMarkdownFile(null)).toBe(false);
  });

  it('resolves document-relative paths strictly without workspace-root fallback and blocks escaping traversals', () => {
    expect(resolveSharedImageCandidates('reports/q4/memo.md', './charts/rev.png')).toEqual([
      'reports/q4/charts/rev.png',
    ]);
    expect(resolveSharedImageCandidates('reports/q4/memo.md', '../shared/logo.png')).toEqual([
      'reports/shared/logo.png',
    ]);
    // No fallback to workspace root charts/rev.png
    expect(resolveSharedImageCandidates('reports/q4/memo.md', 'charts/rev.png')).toEqual([
      'reports/q4/charts/rev.png',
    ]);
    expect(resolveSharedRelativePath('reports/q4/memo.md', './appendix.md#sec-1')).toBe(
      'reports/q4/appendix.md',
    );
    expect(resolveSharedImageCandidates('reports/memo.md', '../../../etc/passwd.png')).toEqual([]);
    expect(resolveSharedRelativePath('reports/memo.md', '../../../etc/passwd.md')).toBeNull();
  });

  it('preserves parent traversals for anonymous /m/ entries so backend resolves them against the withheld entry directory', () => {
    expect(
      resolveSharedRelativePath('legacy-public-md.md', '../shared/logo.png', {
        rootRelative: true,
        anonymousEntry: true,
      }),
    ).toBe('../shared/logo.png');
    expect(
      resolveSharedRelativePath('../shared/overview.md', 'logo.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBe('../shared/logo.png');
    expect(
      resolveSharedRelativePath('../shared/overview.md', '../q4/charts/rev.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBe('../q4/charts/rev.png');
    expect(
      resolveSharedRelativePath('legacy-public-md.md', '..', {
        rootRelative: true,
        anonymousEntry: true,
      }),
    ).toBeNull();
  });
});

describe('SharedPageView Markdown rendering & security', () => {
  beforeEach(() => {
    URL.createObjectURL = vi.fn<typeof URL.createObjectURL>(() => 'blob:shared-md-image');
    URL.revokeObjectURL = vi.fn<typeof URL.revokeObjectURL>();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders shared .md headings, bold, lists, tables, code blocks, links, relative images, inaccessible placeholders, and toggles raw view', async () => {
    const user = userEvent.setup();
    const mdContent = [
      '# Q4 Investment Memo',
      '',
      'Key takeaway: **revenue accelerated** in Q4.',
      '',
      '- First thesis point',
      '- Second thesis point',
      '',
      '| Quarter | Revenue | Margin |',
      '| --- | --- | --- |',
      '| Q3 | $1.2B | 42% |',
      '| Q4 | $1.5B | 45% |',
      '',
      '```python',
      'growth = (1.5 - 1.2) / 1.2',
      '```',
      '',
      'Read [External Docs](https://example.com/docs), [Allowed Appendix](./appendix.md), [Internal Draft](./draft.md), or [Outside](../../../etc/passwd.md).',
      '',
      '![Revenue Chart](./charts/rev.png)',
      '![Root Decoy Chart](charts/decoy.png)',
      '![Restricted Chart](./charts/private.png)',
    ].join('\n');

    const fetchMock = mockServedFiles({
      'reports/memo.md': { body: mdContent, contentType: 'text/markdown; charset=utf-8' },
      'reports/appendix.md': { body: '# Appendix Details\n**Permitted sibling doc**', contentType: 'text/markdown; charset=utf-8' },
      'reports/charts/rev.png': { body: 'png-bytes', contentType: 'image/png' },
      // Same-named file at workspace root must NOT be loaded as fallback for reports/memo.md
      'charts/decoy.png': { body: 'wrong-root-image', contentType: 'image/png' },
      'draft.md': { body: '# Wrong Root Draft', contentType: 'text/markdown; charset=utf-8' },
    });

    renderWithProviders(
      <SharedPageView
        code={CODE}
        metadata={makeVisitorMetadata('reports/memo.md')}
        onStale={vi.fn<() => void>()}
      />,
    );

    // Heading, bold, list, table, code block rendered via Markdown component (no raw ** markers)
    expect(await screen.findByRole('heading', { level: 1, name: 'Q4 Investment Memo' })).toBeInTheDocument();
    const boldEl = screen.getByText('revenue accelerated');
    expect(boldEl.tagName).toBe('STRONG');
    expect(screen.queryByText(/\*\*revenue accelerated\*\*/)).toBeNull();
    expect(screen.getByText('First thesis point')).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('$1.5B')).toBeInTheDocument();
    expect(screen.getByText(/growth/)).toBeInTheDocument();

    // External link stays clickable
    expect(screen.getByRole('link', { name: 'External Docs' })).toHaveAttribute('href', 'https://example.com/docs');

    // Authorized relative link resolves in current version and becomes openable
    const allowedLink = await screen.findByRole('link', { name: 'Allowed Appendix' });
    expect(allowedLink).toHaveAttribute('href', buildServeUrl(PREFIX, 'reports/appendix.md'));

    // Missing/unauthorized relative links render as non-clickable placeholders (no workspace-root fallback)
    const placeholders = await screen.findAllByTestId('shared-link-placeholder');
    expect(placeholders.map((el) => el.textContent).join(' ')).toContain('Internal Draft');
    expect(placeholders.map((el) => el.textContent).join(' ')).toContain('Outside');
    expect(screen.queryByRole('link', { name: 'Internal Draft' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Outside' })).toBeNull();

    // Shared relative image resolves under reports/charts/rev.png; missing/decoy/unshared images show placeholders
    const allowedImg = await screen.findByAltText('Revenue Chart');
    await waitFor(() => expect(allowedImg).toHaveAttribute('src', 'blob:shared-md-image'));
    expect(await screen.findByText(/decoy\.png/i)).toBeInTheDocument();
    expect(await screen.findByText(/private\.png/i)).toBeInTheDocument();
    const requestedUrls = fetchMock.mock.calls.map(([input]) => String(input));
    expect(requestedUrls).not.toContain(buildServeUrl(PREFIX, 'charts/decoy.png'));
    expect(requestedUrls).not.toContain(buildServeUrl(PREFIX, 'draft.md'));

    // Clicking an authorized relative link opens the target doc inside the viewer with a Back bar
    await user.click(allowedLink);
    expect(await screen.findByRole('heading', { level: 1, name: 'Appendix Details' })).toBeInTheDocument();
    expect(screen.getByText('Permitted sibling doc').tagName).toBe('STRONG');
    await user.click(screen.getByRole('button', { name: /Back to memo\.md/i }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Q4 Investment Memo' })).toBeInTheDocument();

    // View raw / View rendered toggle and Download button are both present
    expect(screen.getByRole('button', { name: /Download/i })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /View raw/i }));
    expect(await screen.findByText(/\*\*revenue accelerated\*\*/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /View rendered/i }));
    expect((await screen.findByText('revenue accelerated')).tagName).toBe('STRONG');
  });

  it('sanitizes raw HTML and scripts so embedded scripts and event handlers never execute', async () => {
    const xssMarkdown = [
      '# Security Check',
      '',
      '**Safe bold**',
      '',
      '<script>window.__sharedMdXss = "pwned";</script>',
      '<img src="https://example.com/x.png" onerror="window.__sharedMdXss = \'img\'" alt="probe" />',
      '<iframe src="https://evil.example/frame"></iframe>',
      '<a href="javascript:alert(1)">Click trap</a>',
      '<div onclick="window.__sharedMdXss = \'div\'">Plain text</div>',
    ].join('\n');

    mockServedFiles({
      'notes/sec.markdown': { body: xssMarkdown, contentType: 'text/markdown; charset=utf-8' },
    });

    const { container } = renderWithProviders(
      <SharedPageView
        code={CODE}
        metadata={makeVisitorMetadata('notes/sec.markdown')}
        onStale={vi.fn<() => void>()}
      />,
    );

    expect(await screen.findByRole('heading', { level: 1, name: 'Security Check' })).toBeInTheDocument();
    expect(screen.getByText('Safe bold').tagName).toBe('STRONG');

    // Dangerous elements and attributes are stripped by rehype-sanitize
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
    expect(container.querySelector('[onclick]')).toBeNull();
    for (const anchor of Array.from(container.querySelectorAll('a'))) {
      expect(anchor.getAttribute('href') ?? '').not.toMatch(/^javascript:/i);
    }
    expect((window as unknown as Record<string, unknown>).__sharedMdXss).toBeUndefined();
  });

  it('keeps plain text files (.txt) displayed as-is without a Markdown raw toggle', async () => {
    mockServedFiles({
      'notes/log.txt': {
        body: '# Not a heading\n**still literal stars**',
        contentType: 'text/plain; charset=utf-8',
      },
    });

    renderWithProviders(
      <SharedPageView
        code={CODE}
        metadata={makeVisitorMetadata('notes/log.txt')}
        onStale={vi.fn<() => void>()}
      />,
    );

    expect(await screen.findByText(/\*\*still literal stars\*\*/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1, name: 'Not a heading' })).toBeNull();
    expect(screen.queryByRole('button', { name: /View raw/i })).toBeNull();
    expect(screen.getByRole('button', { name: /Download/i })).toBeInTheDocument();
  });
});
