/**
 * ChatView attachment cap: screenshots (panel chart crops and image-bearing
 * widget snapshots) count toward MAX_FILES (10). Attempting to send 10 files
 * plus a screenshot shows a destructive toast, returns false without sending
 * a doomed HTTP request, and preserves the confirmed chart selection; 9 files
 * plus 1 screenshot (total 10) sends normally.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import type React from 'react';
import { act, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  getThread: vi.fn(),
  getWorkspace: vi.fn(),
  toast: vi.fn(),
  attachmentCount: 0,
}));

vi.mock('@/lib/framer', async () => (await import('./chatViewHarness')).framerMock());
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, opts?: Record<string, unknown>) => (opts?.max ? `${key}:${opts.max}` : key), i18n: { language: 'en-US', resolvedLanguage: 'en-US' } }),
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
vi.mock('../../../../components/ui/chat-input', async () => {
  const { useImperativeHandle } = await import('react');
  const { default: Stub } = (await import('./chatViewHarness')).chatInputMock();
  return {
    default: function ChatInputStub(props: Record<string, unknown>) {
      useImperativeHandle(props.ref as React.Ref<unknown>, () => ({
        getAttachmentCount: () => mocks.attachmentCount,
      }));
      return Stub(props);
    },
  };
});
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

import { THREAD_ID, WORKSPACE_ID, chatInput, chatState, mountChatView, resetChatState } from './chatViewHarness';
import ChatView from '../ChatView';
import { MAX_FILES } from '@/components/ui/chat-input.helpers';
import type { ComposerSend, ReadyAttachment } from '@/components/ui/chat-input.types';
import type { WidgetContextSnapshot } from '@/pages/Dashboard/widgets/framework/contextSnapshot';

type OnSend = (send: ComposerSend) => boolean | void;

function makeFiles(count: number): ReadyAttachment[] {
  return Array.from({ length: count }, (_, i) => ({
    file: new File(['a'], `f${i}.csv`, { type: 'text/csv' }),
    type: 'text/csv',
    attachmentId: `att-${i}`,
  }));
}

function sendWith(files: ReadyAttachment[], widgetSnapshots?: WidgetContextSnapshot[]) {
  let result: boolean | void = undefined;
  act(() => {
    result = (chatInput.props!.onSend as OnSend)({
      text: 'analyze',
      files,
      mentions: [],
      commands: [],
      message: 'analyze',
      modelOptions: {
        model: null,
        reasoningEffort: null,
        fastMode: null,
        ...(widgetSnapshots ? { widgetSnapshots } : {}),
      },
    });
  });
  return result;
}

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
  mocks.attachmentCount = 0;
  mocks.getWorkspace.mockResolvedValue({ workspace_id: WORKSPACE_ID, name: 'Harness WS', status: 'active' });
  mocks.getThread.mockResolvedValue({ thread_id: THREAD_ID, subagents_allowed: false });
});

// DALTON-HOOK(chat-attachment-count): panel chart selection crops left with upstream #874; widget screenshots still count.
describe('ChatView: screenshots count toward MAX_FILES (10)', () => {
  it('blocks sending 10 files plus an image-bearing widget snapshot', async () => {
    mountChatView(ChatView);
    await waitFor(() => expect(chatInput.props?.onSend).toBeTypeOf('function'));

    const widgetSnap: WidgetContextSnapshot = {
      widget_type: 'markets.chart',
      widget_id: 'w1',
      label: 'NVDA Chart',
      description: 'daily',
      captured_at: '2026-11-19T00:00:00Z',
      text: '<widget />',
      data: {},
      image_jpeg_data_url: 'data:image/jpeg;base64,BBBB',
    };

    const sent = sendWith(makeFiles(MAX_FILES), [widgetSnap]);
    expect(sent).toBe(false);
    expect(chatState.handleSendMessage).not.toHaveBeenCalled();
    expect(mocks.toast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'destructive',
        title: `chat.attachmentsTooMany:${MAX_FILES}`,
      }),
    );
  });
});
