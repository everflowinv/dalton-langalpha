/**
 * Mobile Web P2: ChatView bottom floating composer safe-area padding.
 * Mobile uses `pb-[max(0.75rem,env(safe-area-inset-bottom))]`; desktop keeps `p-4`.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import { waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  getThread: vi.fn(),
  getWorkspace: vi.fn(),
  toast: vi.fn(),
  mobile: false,
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => mocks.mobile,
  getIsMobileSnapshot: () => mocks.mobile,
}));
vi.mock('@/lib/framer', async () => (await import('./chatViewHarness')).framerMock());
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: 'en-US', resolvedLanguage: 'en-US' },
  }),
}));
vi.mock('@/lib/supabase', () => ({ supabase: null }));
vi.mock('@/hooks/useUser', () => ({ useUser: () => ({ user: null }) }));
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}));
vi.mock('@/components/ui/use-toast', () => ({
  toast: mocks.toast,
  useToast: () => ({ toast: mocks.toast }),
}));
vi.mock('../Markdown', async () => (await import('./chatViewHarness')).markdownMock());
vi.mock('../../../../components/ui/chat-input', async () => (await import('./chatViewHarness')).chatInputMock());
vi.mock('../../hooks/useChatMessages', async (importOriginal) =>
  (await import('./chatViewHarness')).chatMessagesMock(await importOriginal()));
vi.mock('../../hooks/useWorkspaceFiles', async () => (await import('./chatViewHarness')).workspaceFilesMock());
vi.mock('../../hooks/useNavigationData', async (importOriginal) =>
  (await import('./chatViewHarness')).navigationDataMock(await importOriginal()));
vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await import('./chatViewHarness')).apiMock(await importOriginal()),
  getThread: mocks.getThread,
  getWorkspace: mocks.getWorkspace,
}));

import { THREAD_ID, WORKSPACE_ID, mountChatView, resetChatState } from './chatViewHarness';
import ChatView from '../ChatView';

beforeAll(() => {
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = (() => {}) as typeof Element.prototype.scrollTo;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = (() => {}) as typeof Element.prototype.scrollIntoView;
  }
});

beforeEach(() => {
  resetChatState();
  mocks.getThread.mockReset();
  mocks.getWorkspace.mockReset();
  mocks.toast.mockReset();
  mocks.getWorkspace.mockResolvedValue({ workspace_id: WORKSPACE_ID, name: 'Harness WS', status: 'active' });
  mocks.getThread.mockResolvedValue({ thread_id: THREAD_ID, subagents_allowed: false });
});

describe('ChatView floating composer safe-area classes (Mobile P2)', () => {
  it('applies env(safe-area-inset-bottom) bottom padding on mobile', async () => {
    mocks.mobile = true;
    const { container } = mountChatView(ChatView);

    await waitFor(() => {
      const stub = container.querySelector('[data-testid="chat-input-stub"]');
      expect(stub).not.toBeNull();
    });

    const composerWrapper = container.querySelector('.absolute.inset-x-0.bottom-0.pointer-events-none');
    expect(composerWrapper).not.toBeNull();
    expect(composerWrapper!.className).toContain('pb-[max(0.75rem,env(safe-area-inset-bottom))]');
    expect(composerWrapper!.className).toContain('px-3');
    expect(composerWrapper!.className).toContain('pt-3');
  });

  it('keeps standard p-4 padding on desktop without safe-area class', async () => {
    mocks.mobile = false;
    const { container } = mountChatView(ChatView);

    await waitFor(() => {
      const stub = container.querySelector('[data-testid="chat-input-stub"]');
      expect(stub).not.toBeNull();
    });

    const composerWrapper = container.querySelector('.absolute.inset-x-0.bottom-0.pointer-events-none');
    expect(composerWrapper).not.toBeNull();
    expect(composerWrapper!.className).toContain('p-4');
    expect(composerWrapper!.className).not.toContain('safe-area-inset-bottom');
  });
});
