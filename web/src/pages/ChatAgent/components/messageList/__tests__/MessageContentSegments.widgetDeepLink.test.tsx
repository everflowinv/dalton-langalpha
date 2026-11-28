import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { MessageContentSegments } from '../MessageContentSegments';

const capturedWidgetBlocks = vi.hoisted(() => [] as Array<{ widgetId: string }>);

vi.mock('../../viewers/WidgetBlock', () => ({
  default: ({ widgetId }: { widgetId: string }) => {
    capturedWidgetBlocks.push({ widgetId });
    return <div data-testid="widget-block" data-storage-widget-id={widgetId} />;
  },
}));

type SegmentsProps = React.ComponentProps<typeof MessageContentSegments>;

describe('MessageContentSegments: html_widget deep-link and storage identity', () => {
  it('uses persistent widget_id for both data-widget-id and WidgetBlock storage key, falling back to call id', () => {
    capturedWidgetBlocks.length = 0;
    const { container } = render(
      <MemoryRouter>
        <MessageContentSegments
          segments={[
            { type: 'html_widget', order: 0, widgetId: 'widget-call_1' },
            { type: 'html_widget', order: 1, widgetId: 'widget-call_2' },
          ] as SegmentsProps['segments']}
          reasoningProcesses={{}}
          toolCallProcesses={{}}
          subagentTasks={{}}
          htmlWidgetProcesses={{
            'widget-call_1': {
              widget_id: 'w_persistent_1',
              title: 'Persisted Widget',
              html: '<div>ok</div>',
            },
            'widget-call_2': {
              title: 'Fallback Widget',
              html: '<div>fallback</div>',
            },
          }}
          isStreaming={false}
        />
      </MemoryRouter>,
    );

    const wrappers = Array.from(container.querySelectorAll('[data-widget-id]'));
    expect(wrappers.map((el) => el.getAttribute('data-widget-id'))).toEqual([
      'w_persistent_1',
      'call_2',
    ]);
    expect(capturedWidgetBlocks.map((b) => b.widgetId)).toEqual([
      'widget-w_persistent_1',
      'widget-call_2',
    ]);
  });
});
