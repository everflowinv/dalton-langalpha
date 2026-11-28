import { describe, expect, it } from 'vitest';
import { groupSegments } from '../buildRenderBlocks';
import { blockVisible, projectMessageContent } from '../contentProjection';
import { assistantText } from '../messageText';
import type { ContentSegmentRecord, MessageRecord } from '../types';

function makeMessage(
  segments: ContentSegmentRecord[],
  toolCallProcesses: Record<string, Record<string, unknown>> = {},
): MessageRecord {
  return {
    id: `msg-${Math.random()}`,
    role: 'assistant',
    content: segments.filter((s) => s.type === 'text').map((s) => s.content ?? '').join(''),
    contentType: 'text',
    contentSegments: segments,
    reasoningProcesses: {},
    toolCallProcesses,
  } as unknown as MessageRecord;
}

describe('deliverable phase, tool boundary, and continuation folding', () => {
  it('keeps adjacent commentary and final_answer as separate blocks, hides commentary when collapsed, keeps it when expanded, and excludes it from copy', () => {
    const segs: ContentSegmentRecord[] = [
      { type: 'text', content: '先核对一下利润表数据。', order: 0, phase: 'commentary', messageId: 'm1' },
      { type: 'text', content: '正式结论：净利润同比增长 22%。', order: 1, phase: 'final_answer', messageId: 'm1' },
    ];
    const grouped = groupSegments(segs);
    expect(grouped).toHaveLength(2);
    expect(grouped[0].phase).toBe('commentary');
    expect(grouped[1].phase).toBe('final_answer');

    const msg = makeMessage(segs);
    const projection = projectMessageContent(msg);
    const collapsedTexts = projection.blocks
      .filter((b) => b.type === 'text' && blockVisible(projection, b, 'collapsed', true))
      .map((b) => (b.type === 'text' ? b.segment.content : ''));
    const expandedTexts = projection.blocks
      .filter((b) => b.type === 'text' && blockVisible(projection, b, 'expanded', true))
      .map((b) => (b.type === 'text' ? b.segment.content : ''));

    expect(collapsedTexts).toEqual(['正式结论：净利润同比增长 22%。']);
    expect(expandedTexts).toEqual(['先核对一下利润表数据。', '正式结论：净利润同比增长 22%。']);
    expect(assistantText(msg)).toBe('正式结论：净利润同比增长 22%。');
  });

  it('does not promote a long pre-tool table paragraph into collapsed view across a business tool boundary', () => {
    const longPreTool = '## 初步草稿\n\n## 指标\n\n| 指标 | 数值 |\n|---|---|\n| 收入 | 100 |\n- a\n- b\n- c';
    const msg = makeMessage(
      [
        { type: 'text', content: longPreTool, order: 0, messageId: 'm-pre' },
        { type: 'tool_call', toolCallId: 't-biz', order: 1 },
        { type: 'text', content: '最终核验结果：105。', order: 2, messageId: 'm-final' },
      ],
      {
        't-biz': { toolName: 'Bash', toolCall: { args: {} }, isInProgress: false, isComplete: true, isFailed: false },
      },
    );
    const projection = projectMessageContent(msg);
    const collapsedTexts = projection.blocks
      .filter((b) => b.type === 'text' && blockVisible(projection, b, 'collapsed', true))
      .map((b) => (b.type === 'text' ? b.segment.content : ''));
    const expandedTexts = projection.blocks
      .filter((b) => b.type === 'text' && blockVisible(projection, b, 'expanded', true))
      .map((b) => (b.type === 'text' ? b.segment.content : ''));

    expect(collapsedTexts).toEqual(['最终核验结果：105。']);
    expect(expandedTexts).toEqual([longPreTool, '最终核验结果：105。']);
    // Copy text reuses collapsed deliverable selection and must not include pre-tool prose.
    expect(assistantText(msg)).toBe('最终核验结果：105。');
  });

  it('keeps the full continuation chain in collapsed view while dropping unrelated earlier messages', () => {
    const msg = makeMessage([
      { type: 'text', content: '无关旧片段', order: 0, messageId: 'm-old' },
      { type: 'text', content: '前半段', order: 1, messageId: 'm-1' },
      { type: 'text', content: '后半段', order: 2, messageId: 'm-2', continuedFromMessageId: 'm-1' },
      { type: 'text', content: '截断提示', order: 3, messageId: 'dalton-notice-1', continuedFromMessageId: 'm-2' },
    ]);
    const projection = projectMessageContent(msg);
    const collapsedTexts = projection.blocks
      .filter((b) => b.type === 'text' && blockVisible(projection, b, 'collapsed', true))
      .map((b) => (b.type === 'text' ? b.segment.content : ''));

    expect(collapsedTexts).toEqual(['前半段', '后半段', '截断提示']);
    expect(assistantText(msg)).toBe('前半段后半段截断提示');
  });
});
