/**
 * sync3 B11 r4 review fix: ChatView passes the composer's send whole
 * (`draft: send`), so a send the server refuses while the composer is docked
 * comes back with its attachments, mentions and commands, not only its text.
 *
 * Real hook internals; only the api module (the transport) is mocked — the
 * same harness the comment-refusal suite uses.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';
import { settleMountEffect } from './chatHookHarness';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/api', async () => (await import('./chatHookHarness')).apiMockModule());

import { replayThreadHistory, sendChatMessageStream } from '../../utils/api';
import { quoteToken } from '@/components/ui/chat-input.quoteTokens';
import { useChatMessages } from '../useChatMessages';

const mockReplay = replayThreadHistory as Mock;
const mockSend = sendChatMessageStream as Mock;

type Emit = (e: Record<string, unknown>) => void;

/** Refused at admission, inside the stream: no run was started. */
const refusedInStream = async (_m: string, _ws: string, _t: string, { onEvent }: { onEvent: Emit }) => {
  onEvent({ event: 'error', error: 'A reply is still running.', code: 'running', error_type: 'admission_conflict' });
  return {};
};

async function mountDocked() {
  mockReplay.mockImplementation(async (_tid: string, onEvent: Emit) => {
    onEvent({ event: 'history_page', first_turn_index: 0, has_more: false });
    onEvent({ event: 'replay_done', thread_id: 'th-1', latest_run_id: null });
  });
  const hook = renderHookWithProviders(() =>
    useChatMessages('ws', 'th-1', { platform: 'web', returnsUnsent: () => true }),
  );
  await settleMountEffect();
  await waitFor(() => expect(hook.result.current.isLoadingHistory).toBe(false));
  return hook;
}

describe('useChatMessages: a refused send goes back to the composer whole', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns the draft with its files, mentions and commands when the docked send is refused', async () => {
    mockSend.mockImplementation(refusedInStream);
    const { result } = await mountDocked();

    await act(async () => {
      // The send the composer hands the chat: a ComposerSend's modelOptions
      // ride the SendOptions, as ChatView forwards them.
      await result.current.handleSendMessage('see the attached page', null, null, {
        model: null,
        reasoningEffort: null,
        fastMode: null,
        widgetSnapshots: [{ kind: 'widget', id: 'w1' } as never],
        draft: {
          text: 'see the attached page',
          files: [
            {
              file: new File(['x'], 'page.png', { type: 'image/png' }),
              dataUrl: 'data:image/png;base64,eA==',
              type: 'image/png',
              preview: null,
            },
          ],
          mentions: [{ kind: 'file', path: 'reports/q3.html' }],
          commands: [{ type: 'skill', name: 'chart-annotation', skillName: 'chart-annotation' }],
        },
      });
    });

    expect(result.current.returnedDraft).not.toBeNull();
    expect(result.current.returnedDraft?.text).toBe('see the attached page');
    expect(result.current.returnedDraft?.files).toHaveLength(1);
    expect(result.current.returnedDraft?.files[0].file.name).toBe('page.png');
    // #676 (sync3 B15) made a mention a file, a quote or a chip; this one is a file.
    const mention = result.current.returnedDraft?.mentions[0];
    expect(mention && 'path' in mention ? mention.path : undefined).toBe('reports/q3.html');
    expect(result.current.returnedDraft?.commands[0].skillName).toBe('chart-annotation');
  });

  // sync3 B15: a quote chip rides the draft as a mention with its token — a
  // refused send gives it back with the attachment it went out with, so the
  // composer's giveBack can seat both again.
  it('returns a quote mention with its token beside the attachment when the docked send is refused', async () => {
    mockSend.mockImplementation(refusedInStream);
    const { result } = await mountDocked();

    const token = quoteToken('Revenue grew', []);
    await act(async () => {
      await result.current.handleSendMessage('about this quote', null, null, {
        model: null,
        reasoningEffort: null,
        fastMode: null,
        draft: {
          text: `${token} and the exhibit`,
          files: [
            {
              file: new File(['x'], 'exhibit.txt', { type: 'text/plain' }),
              dataUrl: 'data:text/plain;base64,eA==',
              type: 'text/plain',
              preview: null,
            },
          ],
          mentions: [
            {
              kind: 'quote',
              token,
              snippet: 'Revenue grew',
              from: { kind: 'message', speaker: 'Analyst' },
            },
          ],
          commands: [],
        },
      });
    });

    const back = result.current.returnedDraft;
    expect(back).not.toBeNull();
    expect(back?.text).toBe(`${token} and the exhibit`);
    expect(back?.files).toHaveLength(1);
    expect(back?.files[0].file.name).toBe('exhibit.txt');
    expect(back?.mentions).toHaveLength(1);
    expect(back?.mentions[0]).toMatchObject({
      kind: 'quote',
      token,
      snippet: 'Revenue grew',
      from: { kind: 'message', speaker: 'Analyst' },
    });
  });
});
