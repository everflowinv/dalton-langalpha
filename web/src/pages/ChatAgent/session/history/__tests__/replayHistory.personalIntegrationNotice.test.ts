/**
 * First-touch personal integration privacy notice:
 * - Replayed once on its owning turn even if the turn ended with a rejected approval or error
 * - Handled live via handlePersonalIntegrationNotice without duplicating
 * - Retained visibly even when working rows are collapsed (NOTICES.personalIntegrationNotice.retain)
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import type { AssistantMessage, ChatMessage } from '@/types/chat';

const api = vi.hoisted(() => ({ replayThreadHistory: vi.fn() }));

vi.mock('../../../utils/api', () => ({
  replayThreadHistory: api.replayThreadHistory,
}));

import { loadConversationHistory } from '../replayHistory';
import { handlePersonalIntegrationNotice } from '../../stream/mainEventHandlers';
import { NOTICES } from '../../../components/messageList/notices';
import { buildRuntime, makeDeps, replayOf } from './replayHarness';

const NOTICE_TEXT = '本对话已读取你的个人日历，不会用于团队共享或长期记忆提取。';

beforeEach(() => vi.clearAllMocks());

describe('personal integration first-touch privacy notice (live & history replay)', () => {
  it('replays personal_integration_notice once on its owning turn even when approval was cancelled', async () => {
    const { rt, read } = buildRuntime();
    api.replayThreadHistory.mockImplementation(
      replayOf([
        {
          event: 'user_message',
          data: { thread_id: 'thread-1', turn_index: 0, content: '帮我改一下明天的会议', run_id: 'run-0' },
        },
        {
          event: 'personal_integration_notice',
          data: {
            thread_id: 'thread-1',
            turn_index: 0,
            provider: 'google',
            service: 'calendar',
            message: NOTICE_TEXT,
          },
        },
        {
          event: 'message_chunk',
          data: {
            thread_id: 'thread-1',
            turn_index: 0,
            role: 'assistant',
            id: 'lc_run--0',
            agent: 'model:abc',
            content_type: 'text',
            content: '好的，已取消修改日程。',
          },
        },
        {
          event: 'user_message',
          data: { thread_id: 'thread-1', turn_index: 1, content: '谢谢', run_id: 'run-1' },
        },
        {
          event: 'message_chunk',
          data: {
            thread_id: 'thread-1',
            turn_index: 1,
            role: 'assistant',
            id: 'lc_run--1',
            agent: 'model:abc',
            content_type: 'text',
            content: '不客气。',
          },
        },
      ]),
    );

    await loadConversationHistory(rt, makeDeps());

    const msgs = read();
    const assistant0 = msgs.find((m): m is AssistantMessage => m.id === 'history-assistant-0' && m.role === 'assistant');
    const assistant1 = msgs.find((m): m is AssistantMessage => m.id === 'history-assistant-1' && m.role === 'assistant');

    expect(assistant0?.contentSegments).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: 'notification',
          notice: expect.objectContaining({
            kind: 'personalIntegrationNotice',
            message: NOTICE_TEXT,
            provider: 'google',
            service: 'calendar',
          }),
        }),
      ]),
    );
    const noticeCount0 = (assistant0?.contentSegments ?? []).filter(
      (s) => s.type === 'notification' && s.notice?.kind === 'personalIntegrationNotice',
    ).length;
    expect(noticeCount0).toBe(1);

    const noticeCount1 = (assistant1?.contentSegments ?? []).filter(
      (s) => s.type === 'notification' && s.notice?.kind === 'personalIntegrationNotice',
    ).length;
    expect(noticeCount1).toBe(0);
  });

  it('deduplicates live personal_integration_notice and retains it when working rows fold', () => {
    let messages: AssistantMessage[] = [
      {
        id: 'assistant-live-1',
        role: 'assistant',
        content: '',
        contentSegments: [],
        isStreaming: true,
      } as unknown as AssistantMessage,
    ];
    const setMessages = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      messages = updater(messages) as AssistantMessage[];
    };

    handlePersonalIntegrationNotice({
      assistantMessageId: 'assistant-live-1',
      event: { message: NOTICE_TEXT, provider: 'feishu', service: 'calendar' },
      order: 1,
      setMessages: setMessages as never,
    });
    handlePersonalIntegrationNotice({
      assistantMessageId: 'assistant-live-1',
      event: { message: NOTICE_TEXT, provider: 'feishu', service: 'calendar' },
      order: 2,
      setMessages: setMessages as never,
    });

    const notices = (messages[0].contentSegments ?? []).filter(
      (s) => s.type === 'notification' && s.notice?.kind === 'personalIntegrationNotice',
    );
    expect(notices).toHaveLength(1);
    expect(NOTICES.personalIntegrationNotice.retain).toBe(true);
  });
});
