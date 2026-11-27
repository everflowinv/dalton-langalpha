import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '@/test/utils';
import { buildServeUrl, versionPrefix } from '@/pages/ChatAgent/components/viewers/html/wsfilesUrl';
import { FileBody } from '@/pages/SharedChat/SharedFileView';
import SharedPageView from '@/pages/SharedChat/SharedPageView';
import type { SharedPublicFileMetadata } from '@/pages/SharedChat/api';
import {
  beginScopedImageRead,
  buildSharedImageScope,
  commitScopedImageUrl,
  revokeSharedImageScope,
} from '../sharedImageCache';
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

  it('keeps root-anchored subdocument directory semantics for relative and root-prefixed targets', () => {
    // Opening /reports/shared/overview.md from reports/q4/memo.md under anonymous /m/
    expect(
      resolveSharedRelativePath('legacy-public-md.md', '/reports/shared/overview.md', {
        rootRelative: true,
        anonymousEntry: true,
      }),
    ).toBe('/reports/shared/overview.md');
    expect(
      resolveSharedRelativePath('/reports/shared/overview.md', 'logo.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBe('/reports/shared/logo.png');
    expect(
      resolveSharedRelativePath('/reports/shared/overview.md', './detail.md', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBe('/reports/shared/detail.md');
    expect(
      resolveSharedRelativePath('/reports/shared/overview.md', '../q4/charts/rev.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBe('/reports/q4/charts/rev.png');
    expect(
      resolveSharedRelativePath('/reports/shared/overview.md', '/assets/banner.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBe('/assets/banner.png');
    expect(
      resolveSharedRelativePath('/reports/shared/overview.md', '../../../escape.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBeNull();
    expect(
      resolveSharedRelativePath('/overview.md', 'logo.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBe('/logo.png');
    expect(
      resolveSharedRelativePath('/overview.md', '../escape.png', {
        rootRelative: false,
        anonymousEntry: true,
      }),
    ).toBeNull();

    // Direct mode (/a/ or authenticated /m/): leading / resolves against version root, subdoc relative against subdoc dir
    expect(resolveSharedRelativePath('reports/q4/memo.md', '/reports/shared/overview.md')).toBe(
      'reports/shared/overview.md',
    );
    expect(resolveSharedImageCandidates('reports/shared/overview.md', 'logo.png')).toEqual([
      'reports/shared/logo.png',
    ]);
    expect(resolveSharedRelativePath('reports/shared/overview.md', '/assets/guide.md')).toBe(
      'assets/guide.md',
    );
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

  it('resolves subdocument relative images/links and version-root links after opening /reports/shared/overview.md on anonymous /m/', async () => {
    const user = userEvent.setup();
    const anonFrameBase = '/api/v1/dalton/monitors/public/legacy-public-md/v/2';
    const anonEntryUrl = `${anonFrameBase}/`;
    const entryDir = 'reports/q4';

    // Mirrors backend _resolve_public_entry_relative_path("reports/q4/memo.md", rel)
    const resolveBackendRel = (rel: string): string | null => {
      const stack = rel.startsWith('/') ? [] : entryDir.split('/').filter(Boolean);
      for (const seg of rel.split('/')) {
        if (!seg || seg === '.') continue;
        if (seg === '..') {
          if (stack.length === 0) return null;
          stack.pop();
          continue;
        }
        stack.push(seg);
      }
      return stack.length > 0 ? stack.join('/') : null;
    };

    const files: Record<string, { body: string; contentType: string }> = {
      'reports/q4/memo.md': {
        body: '# Q4 Memo\n\nOpen [Shared Overview](/reports/shared/overview.md).\n',
        contentType: 'text/markdown; charset=utf-8',
      },
      'reports/shared/overview.md': {
        body: [
          '# Shared Overview',
          '',
          '![Shared Logo](logo.png)',
          '',
          'Read [Sibling Detail](./detail.md) or [Root Guide](/assets/guide.md).',
        ].join('\n'),
        contentType: 'text/markdown; charset=utf-8',
      },
      'reports/shared/logo.png': {
        body: 'shared-logo-bytes',
        contentType: 'image/png',
      },
      'reports/shared/detail.md': {
        body: '# Sibling Detail Page\n',
        contentType: 'text/markdown; charset=utf-8',
      },
      'assets/guide.md': {
        body: '# Root Guide Page\n',
        contentType: 'text/markdown; charset=utf-8',
      },
    };

    let blobCounter = 0;
    URL.createObjectURL = vi.fn<typeof URL.createObjectURL>(() => `blob:subdoc-${++blobCounter}`);

    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url === anonEntryUrl) {
        const entry = files['reports/q4/memo.md'];
        return {
          ok: true,
          status: 200,
          headers: new Headers({ 'content-type': entry.contentType }),
          text: async () => entry.body,
          blob: async () => new Blob([entry.body], { type: entry.contentType }),
        } as Response;
      }
      const parsed = new URL(url, 'http://localhost');
      const rel = parsed.searchParams.get('rel');
      const resolved = rel ? resolveBackendRel(rel) : null;
      const hit = resolved ? files[resolved] : undefined;
      if (!hit) {
        return { ok: false, status: 404, headers: new Headers() } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': hit.contentType }),
        text: async () => hit.body,
        blob: async () => new Blob([hit.body], { type: hit.contentType }),
      } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithProviders(
      <FileBody
        scope="monitor:anon-subdoc:v2"
        path="legacy-public-md.md"
        frameBase={anonFrameBase}
        entryUrl={anonEntryUrl}
        anonymousEntry
        onDownload={vi.fn<() => void>()}
        downloading={false}
      />,
    );

    expect(await screen.findByRole('heading', { level: 1, name: 'Q4 Memo' })).toBeInTheDocument();
    const overviewLink = await screen.findByRole('link', { name: 'Shared Overview' });
    await user.click(overviewLink);

    expect(await screen.findByRole('heading', { level: 1, name: 'Shared Overview' })).toBeInTheDocument();
    const logoImg = await screen.findByAltText('Shared Logo');
    await waitFor(() => expect(logoImg).toHaveAttribute('src', 'blob:subdoc-1'));

    const requestedUrls = fetchMock.mock.calls.map(([input]) => String(input));
    expect(requestedUrls).toContain(`${anonFrameBase}/?rel=%2Freports%2Fshared%2Flogo.png`);
    expect(requestedUrls).not.toContain(`${anonFrameBase}/?rel=reports%2Fshared%2Flogo.png`);

    const detailLink = await screen.findByRole('link', { name: 'Sibling Detail' });
    expect(detailLink).toHaveAttribute('href', `${anonFrameBase}/?rel=%2Freports%2Fshared%2Fdetail.md`);

    const rootGuideLink = await screen.findByRole('link', { name: 'Root Guide' });
    expect(rootGuideLink).toHaveAttribute('href', `${anonFrameBase}/?rel=%2Fassets%2Fguide.md`);

    await user.click(detailLink);
    expect(await screen.findByRole('heading', { level: 1, name: 'Sibling Detail Page' })).toBeInTheDocument();
  });

  it('does not reuse entry image cache when a subdocument references a missing same-named logo.png', async () => {
    const user = userEvent.setup();
    let blobCounter = 0;
    URL.createObjectURL = vi.fn<typeof URL.createObjectURL>(() => `blob:same-name-${++blobCounter}`);

    mockServedFiles({
      'reports/q4/memo.md': {
        body: '# Entry Doc\n\n![Entry Logo](logo.png)\n\nSee [Sub Overview](../shared/overview.md).\n',
        contentType: 'text/markdown; charset=utf-8',
      },
      'reports/q4/logo.png': {
        body: 'entry-logo-bytes',
        contentType: 'image/png',
      },
      // Subdocument also references logo.png, but reports/shared/logo.png is NOT included in the snapshot (404).
      'reports/shared/overview.md': {
        body: '# Sub Overview\n\n![Sub Logo](logo.png)\n',
        contentType: 'text/markdown; charset=utf-8',
      },
    });

    renderWithProviders(
      <SharedPageView
        code="cacheiso1234"
        metadata={makeVisitorMetadata('reports/q4/memo.md')}
        onStale={vi.fn<() => void>()}
      />,
    );

    expect(await screen.findByRole('heading', { level: 1, name: 'Entry Doc' })).toBeInTheDocument();
    const entryImg = await screen.findByAltText('Entry Logo');
    await waitFor(() => expect(entryImg).toHaveAttribute('src', 'blob:same-name-1'));

    const subLink = await screen.findByRole('link', { name: 'Sub Overview' });
    await user.click(subLink);

    expect(await screen.findByRole('heading', { level: 1, name: 'Sub Overview' })).toBeInTheDocument();
    expect(await screen.findByText('logo.png could not be loaded')).toBeInTheDocument();
    expect(screen.queryByAltText('Sub Logo')).toBeNull();
    expect(screen.queryByAltText('Entry Logo')).toBeNull();
  });

  it('routes controlled-media image URLs in /a/ (SharedPageView) and /m/ (FileBody) through version prefix without viewer credentials or sharedImageCache reuse', async () => {
    const staleScope = buildSharedImageScope('tok-leak', true);
    const { epoch } = beginScopedImageRead(staleScope);
    commitScopedImageUrl(
      staleScope,
      '/api/v1/public/shared/tok-leak/media/m-leak',
      epoch,
      'blob:stale-shared-chat-image',
    );

    try {
      const mdWithControlledUrls = [
        '# Published Report',
        '',
        '![Owner Controlled](/api/v1/threads/t-99/media/m-owner)',
        '![Public Controlled](/api/v1/public/shared/tok-leak/media/m-leak)',
        '![Absolute Owner](https://app.example.com/api/v1/threads/t-99/media/m-abs)',
      ].join('\n');

      // 1. /a/ SharedPageView (version prefix)
      const fetchA = mockServedFiles({
        'reports/controlled.md': { body: mdWithControlledUrls, contentType: 'text/markdown; charset=utf-8' },
      });

      const viewA = renderWithProviders(
        <SharedPageView
          code="ctrlcode1234"
          metadata={makeVisitorMetadata('reports/controlled.md')}
          onStale={vi.fn<() => void>()}
        />,
      );

      expect(await screen.findByRole('heading', { level: 1, name: 'Published Report' })).toBeInTheDocument();
      expect(await screen.findByText('m-owner could not be loaded')).toBeInTheDocument();
      expect(await screen.findByText('m-leak could not be loaded')).toBeInTheDocument();
      expect(await screen.findByText('m-abs could not be loaded')).toBeInTheDocument();
      expect(screen.queryByRole('img')).toBeNull();

      for (const [input, init] of fetchA.mock.calls) {
        const url = String(input);
        expect(url.startsWith(PREFIX)).toBe(true);
        expect(url).not.toMatch(/^\/api\/v1\/threads\//);
        expect(url).not.toMatch(/^\/api\/v1\/public\/shared\//);
        expect((init as RequestInit | undefined)?.credentials).not.toBe('include');
      }
      expect(fetchA.mock.calls.map(([input]) => String(input))).toEqual(
        expect.arrayContaining([
          buildServeUrl(PREFIX, 'api/v1/threads/t-99/media/m-owner'),
          buildServeUrl(PREFIX, 'api/v1/public/shared/tok-leak/media/m-leak'),
        ]),
      );
      viewA.unmount();

      // 2. /m/ anonymous public monitor document (version prefix + ?rel=)
      const anonFrameBase = '/api/v1/dalton/monitors/public/ctrl-mon/v/2';
      const anonEntryUrl = `${anonFrameBase}/`;
      const fetchM = vi.fn<typeof fetch>(async (input) => {
        const url = String(input);
        if (url === anonEntryUrl) {
          return {
            ok: true,
            status: 200,
            headers: new Headers({ 'content-type': 'text/markdown; charset=utf-8' }),
            text: async () => mdWithControlledUrls,
            blob: async () => new Blob([mdWithControlledUrls], { type: 'text/markdown; charset=utf-8' }),
          } as Response;
        }
        return { ok: false, status: 404, headers: new Headers() } as Response;
      });
      vi.stubGlobal('fetch', fetchM);

      renderWithProviders(
        <FileBody
          scope="monitor:ctrl-mon:v2"
          path="ctrl-mon.md"
          frameBase={anonFrameBase}
          entryUrl={anonEntryUrl}
          anonymousEntry
          onDownload={vi.fn<() => void>()}
          downloading={false}
        />,
      );

      expect(await screen.findByRole('heading', { level: 1, name: 'Published Report' })).toBeInTheDocument();
      expect(await screen.findByText('m-owner could not be loaded')).toBeInTheDocument();
      expect(await screen.findByText('m-leak could not be loaded')).toBeInTheDocument();
      expect(await screen.findByText('m-abs could not be loaded')).toBeInTheDocument();
      expect(screen.queryByRole('img')).toBeNull();

      for (const [input, init] of fetchM.mock.calls) {
        const url = String(input);
        expect(url.startsWith(`${anonFrameBase}/`)).toBe(true);
        expect((init as RequestInit | undefined)?.credentials).not.toBe('include');
      }
      expect(fetchM.mock.calls.map(([input]) => String(input))).toEqual(
        expect.arrayContaining([
          `${anonFrameBase}/?rel=%2Fapi%2Fv1%2Fthreads%2Ft-99%2Fmedia%2Fm-owner`,
          `${anonFrameBase}/?rel=%2Fapi%2Fv1%2Fpublic%2Fshared%2Ftok-leak%2Fmedia%2Fm-leak`,
        ]),
      );
    } finally {
      revokeSharedImageScope('tok-leak');
    }
  });
});
