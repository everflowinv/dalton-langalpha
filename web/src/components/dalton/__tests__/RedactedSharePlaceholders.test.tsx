import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import type { Element as HastElement, Root } from 'hast';
import type { getAuthHeaders as GetAuthHeadersFn } from '@/lib/authToken';

const getAuthHeadersMock = vi.fn<typeof GetAuthHeadersFn>(async () => ({
  Authorization: 'Bearer owner-jwt-token',
}));

vi.mock('@/lib/authToken', () => ({
  getAuthHeaders: (...args: Parameters<typeof GetAuthHeadersFn>) => getAuthHeadersMock(...args),
}));

import {
  ControlledMediaError,
  RedactedImage,
  RedactedWidgetCard,
  fetchControlledMediaBlob,
  rehypeStripResourceStyles,
  resolveControlledMediaUrl,
} from '../RedactedSharePlaceholders';
import {
  buildSharedImageScope,
  getScopedImageUrl,
  revokeSharedImageScope,
} from '@/lib/dalton/sharedImageCache';

describe('RedactedSharePlaceholders', () => {
  const originalFetch = globalThis.fetch;
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;

  let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;
  let createObjectURLMock: ReturnType<typeof vi.fn<typeof URL.createObjectURL>>;
  let revokeObjectURLMock: ReturnType<typeof vi.fn<typeof URL.revokeObjectURL>>;

  beforeEach(() => {
    vi.unstubAllEnvs();
    getAuthHeadersMock.mockClear();
    fetchMock = vi.fn<typeof fetch>();
    createObjectURLMock = vi.fn<typeof URL.createObjectURL>(() => 'blob:mock-media-url');
    revokeObjectURLMock = vi.fn<typeof URL.revokeObjectURL>();
    globalThis.fetch = fetchMock;
    URL.createObjectURL = createObjectURLMock;
    URL.revokeObjectURL = revokeObjectURLMock;
  });

  afterEach(() => {
    revokeSharedImageScope('tok-pub');
    globalThis.fetch = originalFetch;
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    vi.unstubAllEnvs();
  });

  it('resolves root-relative /api/v1/... URLs with VITE_API_BASE_URL and leaves absolute URLs unchanged', () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000/');
    expect(resolveControlledMediaUrl('/api/v1/threads/t1/media/m1')).toBe(
      'http://localhost:8000/api/v1/threads/t1/media/m1',
    );
    expect(resolveControlledMediaUrl('https://cdn.example/img.png')).toBe(
      'https://cdn.example/img.png',
    );
  });

  it('fetches owner controlled media with cors, no-store, credentials:include and auth headers on trusted origin', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000');
    fetchMock.mockResolvedValueOnce(
      new Response(new Blob(['png-bytes'], { type: 'image/png' }), { status: 200 }),
    );

    const url = await fetchControlledMediaBlob('/api/v1/threads/t1/media/tok-1', { kind: 'owner' });

    expect(url).toBe('blob:mock-media-url');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = fetchMock.mock.calls[0];
    expect(calledUrl).toBe('http://localhost:8000/api/v1/threads/t1/media/tok-1');
    expect(init).toMatchObject({
      mode: 'cors',
      cache: 'no-store',
      credentials: 'include',
      headers: { Authorization: 'Bearer owner-jwt-token' },
    });
  });

  it('rejects owner controlled media requests to untrusted origins without calling fetch or reading credentials', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000');

    await expect(
      fetchControlledMediaBlob('https://evil.example/api/v1/threads/t1/media/tok-1', { kind: 'owner' }),
    ).rejects.toThrow(/untrusted/i);

    await expect(
      fetchControlledMediaBlob('http://127.0.0.1:3999/api/v1/threads/t1/media/tok-1', { kind: 'owner' }),
    ).rejects.toThrow(/untrusted/i);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(getAuthHeadersMock).not.toHaveBeenCalled();
  });

  it('fetches public controlled media with cors, no-store, credentials:omit and stores in sharedImageCache', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000');
    fetchMock.mockResolvedValueOnce(
      new Response(new Blob(['public-img'], { type: 'image/png' }), { status: 200 }),
    );

    const url = await fetchControlledMediaBlob('/api/v1/public/shared/tok-pub/media/ref-1', {
      kind: 'public',
    });

    expect(url).toBe('blob:mock-media-url');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = fetchMock.mock.calls[0];
    expect(calledUrl).toBe('http://localhost:8000/api/v1/public/shared/tok-pub/media/ref-1');
    expect(init).toMatchObject({
      mode: 'cors',
      cache: 'no-store',
      credentials: 'omit',
    });
    expect(getAuthHeadersMock).not.toHaveBeenCalled();

    const scope = buildSharedImageScope('tok-pub', true);
    expect(getScopedImageUrl(scope, 'http://localhost:8000/api/v1/public/shared/tok-pub/media/ref-1')).toBe(
      'blob:mock-media-url',
    );
  });

  it('throws ControlledMediaError with status 403 on forbidden responses', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000');
    fetchMock.mockResolvedValueOnce(new Response('forbidden', { status: 403 }));

    let caught: unknown;
    try {
      await fetchControlledMediaBlob('/api/v1/public/shared/tok-pub/media/ref-403', {
        kind: 'public',
      });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ControlledMediaError);
    expect((caught as ControlledMediaError).status).toBe(403);
    expect((caught as ControlledMediaError).response.status).toBe(403);
  });

  it('strips listed attributes and resource elements in rehypeStripResourceStyles while keeping redacted-image', () => {
    const tree: Root = {
      type: 'root',
      children: [
        {
          type: 'element',
          tagName: 'div',
          properties: {
            style: 'background:url(https://evil.example/a.png)',
            title: 'SECRET_TITLE',
            ariaLabel: 'SECRET_ARIA',
            'aria-description': 'SECRET_ARIA_DASH',
            dataSecret: 'SECRET_DATA',
            'data-token': 'SECRET_DATA_DASH',
            srcSet: 'https://evil.example/1x.png 1x',
            poster: 'https://evil.example/poster.png',
            background: 'https://evil.example/bg.png',
            className: ['safe-box'],
          },
          children: [
            { type: 'element', tagName: 'img', properties: { src: 'https://evil.example/i.png' }, children: [] },
            { type: 'element', tagName: 'picture', properties: {}, children: [] },
            { type: 'element', tagName: 'source', properties: { srcset: 'a.png' }, children: [] },
            { type: 'element', tagName: 'video', properties: { poster: 'v.png' }, children: [] },
            { type: 'element', tagName: 'audio', properties: { src: 'a.mp3' }, children: [] },
            { type: 'element', tagName: 'iframe', properties: { src: 'https://evil.example' }, children: [] },
            { type: 'element', tagName: 'object', properties: { data: 'a.swf' }, children: [] },
            { type: 'element', tagName: 'embed', properties: { src: 'a.swf' }, children: [] },
            {
              type: 'element',
              tagName: 'svg',
              properties: { style: 'background:url(x)' },
              children: [
                { type: 'element', tagName: 'image', properties: { href: 'x.png' }, children: [] },
                { type: 'element', tagName: 'feImage', properties: { href: 'y.png' }, children: [] },
              ],
            },
            {
              type: 'element',
              tagName: 'redacted-image',
              properties: { alt: 'Revenue chart', title: 'drop-me', dataSecret: 'drop-me' },
              children: [],
            },
            {
              type: 'element',
              tagName: 'a',
              properties: { href: 'https://example.com', title: 'SECRET_LINK_TITLE' },
              children: [{ type: 'text', value: 'Public Link' }],
            },
          ],
        },
      ],
    };

    rehypeStripResourceStyles()(tree);

    const container = tree.children[0] as HastElement;
    expect(container.properties).toEqual({ className: ['safe-box'] });

    const remainingTags = container.children
      .filter((c): c is HastElement => c.type === 'element')
      .map((c) => c.tagName);
    expect(remainingTags).toEqual(['svg', 'redacted-image', 'a']);

    const svg = container.children[0] as HastElement;
    expect(svg.properties).toEqual({});
    expect(svg.children).toEqual([]);

    const redactedImg = container.children[1] as HastElement;
    expect(redactedImg.properties).toEqual({ alt: 'Revenue chart' });

    const anchor = container.children[2] as HastElement;
    expect(anchor.properties).toEqual({ href: 'https://example.com' });
  });

  it('renders static RedactedWidgetCard without iframe and RedactedImage without img', () => {
    const { container } = render(
      <div>
        <RedactedWidgetCard />
        <RedactedImage alt="Quarterly Revenue" />
      </div>,
    );

    expect(container.querySelectorAll('iframe')).toHaveLength(0);
    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(screen.getByText('Quarterly Revenue')).toBeInTheDocument();
  });
});
