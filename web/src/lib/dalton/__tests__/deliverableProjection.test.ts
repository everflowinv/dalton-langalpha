import { describe, expect, it } from 'vitest';
import {
  blockVisible,
  holdsAnswerText,
  projectMessageContent,
} from '@/pages/ChatAgent/components/messageList/contentProjection';
import { assistantText } from '@/pages/ChatAgent/components/messageList/messageText';
import type { MessageRecord } from '@/pages/ChatAgent/components/messageList/types';

describe('unified deliverableProjection for collapsed view and copy (R4 #6)', () => {
  it('excludes commentary before tool attribution so pre-tool preamble is hidden in both collapsed view and copy', () => {
    // Exact Codex P2 #5 repro: unphased transition text -> commentary -> business tool -> no final text
    const msg = {
      id: 'a-r4-codex-p2-5',
      role: 'assistant',
      content: '',
      contentType: 'text',
      contentSegments: [
        { type: 'text', content: '我先去拉取最新财报数据，请稍候。', order: 1, messageId: 'm-1' },
        { type: 'text', content: '正在执行脚本核对营收字段。', order: 2, phase: 'commentary', messageId: 'm-2' },
        { type: 'tool_call', toolCallId: 'tc-1', order: 3 },
      ],
      reasoningProcesses: {},
      toolCallProcesses: {
        'tc-1': {
          toolName: 'Bash',
          toolCall: { args: { command: 'python fetch.py' } },
          isInProgress: false,
          isComplete: true,
          isFailed: false,
        },
      },
    } as unknown as MessageRecord;

    const projection = projectMessageContent(msg);
    const collapsedTexts = projection.blocks
      .filter((b) => b.type === 'text' && blockVisible(projection, b, 'collapsed', true))
      .map((b) => (b.type === 'text' ? b.segment.content : ''));

    expect(collapsedTexts).toEqual([]);
    expect(holdsAnswerText(projection, true)).toBe(false);
    expect(assistantText(msg)).toBe('');
  });

  it('keeps identical post-tool continuation chain in both collapsed view and copy while dropping commentary and pre-tool text', () => {
    const msg = {
      id: 'a-r4-chain',
      role: 'assistant',
      content: '',
      contentType: 'text',
      contentSegments: [
        { type: 'text', content: '过渡话术：准备查数据', order: 1, messageId: 'm-pre' },
        { type: 'text', content: '思考过程说明', order: 2, phase: 'commentary', messageId: 'm-pre' },
        { type: 'tool_call', toolCallId: 'tc-1', order: 3 },
        { type: 'text', content: '二次过程解说', order: 4, phase: 'commentary', messageId: 'm-ans-1' },
        { type: 'text', content: '前半段结论', order: 5, phase: 'final_answer', messageId: 'm-ans-1' },
        {
          type: 'text',
          content: '后半段结论',
          order: 6,
          phase: 'final_answer',
          messageId: 'm-ans-2',
          continuedFromMessageId: 'm-ans-1',
        },
      ],
      reasoningProcesses: {},
      toolCallProcesses: {
        'tc-1': {
          toolName: 'WebSearch',
          toolCall: { args: {} },
          isInProgress: false,
          isComplete: true,
          isFailed: false,
        },
      },
    } as unknown as MessageRecord;

    const projection = projectMessageContent(msg);
    const collapsedText = projection.blocks
      .filter((b) => b.type === 'text' && blockVisible(projection, b, 'collapsed', true))
      .map((b) => (b.type === 'text' ? b.segment.content ?? '' : ''))
      .join('');

    expect(collapsedText).toBe('前半段结论后半段结论');
    expect(assistantText(msg)).toBe('前半段结论后半段结论');
  });
});
