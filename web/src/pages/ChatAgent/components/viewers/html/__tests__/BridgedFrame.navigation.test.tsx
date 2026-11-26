/**
 * A script that sets location, or a refresh, takes the viewer's own frame to
 * the next file without the view its address carried, and the server frames
 * that file with only the bridge a frame inside a page gets, which reports up.
 * Heard from the viewer's own frame, that file loads again with the view,
 * once per document, and its report counts as its hello so the frame is not
 * taken for one that speaks no bridge. The page bridge is stubbed at its hook.
 */
import { act, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';
import type { ViewerToPage } from '../pageProtocol'; // DALTON-HOOK(mobile-web)
import type { PageBridgeListener } from '../usePageBridge';

const page = vi.hoisted(() => ({
  listener: null as PageBridgeListener | null,
  sent: [] as string[],
  lastDocId: undefined as string | undefined,
}));
vi.mock('../usePageBridge', () => {
  const bridge = {
    ready: false,
    session: 0,
    path: null,
    // DALTON-HOOK(mobile-web): echo page:ping with page:pong carrying the active docId
    post: (message: ViewerToPage) => {
      if (message.type === 'page:ping') {
        page.listener?.(
          page.lastDocId !== undefined
            ? { type: 'page:pong', seq: message.seq, docId: page.lastDocId }
            : { type: 'page:pong', seq: message.seq },
          bridge as never,
        );
      }
    },
    subscribe: (listener: PageBridgeListener) => {
      page.listener = listener;
      return () => {};
    },
  };
  return { usePageBridge: () => bridge };
});
vi.mock('../../frameHost', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../frameHost')>()),
  sendFrameTo: (_frame: HTMLIFrameElement, url: string) => { page.sent.push(url); },
}));

import { BridgedFrame } from '../BridgedFrame';

const PREFIX = `${window.location.origin}/api/v1/wsfiles/g/v1.ws.1.sig/`;
const NEXT = `${PREFIX}r/next.html`;

function say(message: Parameters<PageBridgeListener>[0]) {
  if (message.type === 'page:ready') page.lastDocId = message.docId;
  act(() => page.listener?.(message, null as never));
}

function renderFrame(onUnbridged = vi.fn()) {
  const view = renderWithProviders(
    <BridgedFrame
      source={{ key: 'k', src: `${PREFIX}r/a.html?inject=comments`, sandbox: 'allow-scripts' }}
      title="report"
      groundKey="g"
      onUnbridged={onUnbridged}
    />,
  );
  return { frame: view.container.querySelector('iframe')!, onUnbridged };
}

beforeEach(() => {
  page.listener = null;
  page.sent = [];
  page.lastDocId = undefined;
});
afterEach(() => vi.useRealTimers());

describe('BridgedFrame after a scripted navigation', () => {
  it('loads the next file again with the view, once per document', () => {
    renderFrame();

    say({ type: 'page:frame-loads', doc: NEXT });
    expect(page.sent).toEqual([`${NEXT}?inject=comments`]);
    // What that document sent before it went still arrives.
    say({ type: 'page:frame-loads', doc: NEXT });
    expect(page.sent).toHaveLength(1);

    // Viewed, its own script takes it there again: once more.
    // DALTON-HOOK(mobile-web): include pathState and docId on parsed page:ready message
    say({ type: 'page:ready', path: 'r/next.html', pathState: 'path', docId: 'doc-next-1' });
    say({ type: 'page:frame-loads', doc: NEXT });
    expect(page.sent).toHaveLength(2);
  });

  it('leaves a frame alone that already has the view, or went elsewhere', () => {
    renderFrame();
    say({ type: 'page:frame-loads', doc: `${NEXT}?inject=comments` });
    say({ type: 'page:frame-loads', doc: 'https://elsewhere.example/api/v1/wsfiles/g/v1.ws.1.sig/r/x.html' });
    expect(page.sent).toEqual([]);
  });

  it('is not taken for a frame that speaks no bridge', () => {
    vi.useFakeTimers();
    const { frame, onUnbridged } = renderFrame();
    fireEvent.load(frame);
    say({ type: 'page:frame-loads', doc: NEXT });
    // DALTON-HOOK(mobile-web): include pathState and docId on parsed page:ready message
    say({ type: 'page:ready', path: 'r/next.html', pathState: 'path', docId: 'doc-next-2' });
    fireEvent.load(frame);
    act(() => { vi.advanceTimersByTime(5000); });
    expect(onUnbridged).not.toHaveBeenCalled();

    // A load nothing answers still is.
    page.lastDocId = 'doc-stale';
    fireEvent.load(frame);
    act(() => { vi.advanceTimersByTime(5000); });
    expect(onUnbridged).toHaveBeenCalledTimes(1);
  });
});
