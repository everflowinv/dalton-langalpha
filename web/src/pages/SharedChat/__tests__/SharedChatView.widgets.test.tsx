/**
 * DALTON-HOOK(shared-widgets): an inline widget (ShowWidget) on a shared link
 * shows as it does in the owner's view. The public replay sends its artifact
 * with the data_ref already resolved; the shared view used to drop it.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

const capturedMessages: Record<string, unknown>[][] = [];

vi.mock('react-router', () => ({
  Link: ({ children }: { children?: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock('../../../contexts/AuthContext', () => ({ useAuth: () => ({ isLoggedIn: false }) }));
vi.mock('../../../contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light' }),
}));

// MessageList is mocked, so nothing registers with the dispatch-status
// provider the view hosts above it.
vi.mock('../../ChatAgent/hooks/usePTCDispatchStatus', () => ({
  DispatchStatusProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('../../ChatAgent/components/MessageList', () => ({
  default: ({ messages }: { messages: Record<string, unknown>[] }) => {
    capturedMessages.push(messages);
    return <div data-testid="message-list" />;
  },
}));

vi.mock('../../ChatAgent/components/FilePanel', () => ({ default: () => null }));

vi.mock('../../ChatAgent/contexts/WorkspaceContext', () => ({
  WorkspaceProvider: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}));

const replayEvents = [
  { event: 'user_message', turn_index: 0, role: 'user', content: 'chart it' },
  { event: 'message_chunk', turn_index: 0, role: 'assistant', content: 'First the revenue.', content_type: 'text', _eventId: 101 },
  {
    event: 'tool_calls',
    turn_index: 0,
    tool_calls: [{ id: 'tc1', name: 'ShowWidget', args: {} }],
    _eventId: 102,
  },
  {
    event: 'artifact',
    turn_index: 0,
    artifact_type: 'html_widget',
    artifact_id: 'w1',
    _eventId: 103,
    tool_call_id: 'tc1',
    payload: { html: '<div id="chart"></div>', title: 'Revenue', data: { 'rev.csv': 'q,v\n1,2' } },
  },
  { event: 'message_chunk', turn_index: 0, role: 'assistant', content: 'Revenue grew.', content_type: 'text', _eventId: 104 },
  {
    event: 'artifact',
    turn_index: 0,
    artifact_type: 'html_widget',
    artifact_id: 'w2',
    _eventId: 105,
    tool_call_id: 'tc2',
    payload: { html: '<p>second</p>', title: 'Margin' },
  },
  { event: 'replay_done' },
];

vi.mock('../api', () => ({
  replaySharedThread: vi.fn(async (_token: string, onEvent: (e: unknown) => void) => {
    replayEvents.forEach(onEvent);
  }),
  getSharedFiles: vi.fn(async () => []),
  readSharedFile: vi.fn(async () => ''),
  downloadSharedFile: vi.fn(async () => undefined),
  servedObjectUrl: vi.fn(async () => ''),
  servedBytes: vi.fn(async () => new ArrayBuffer(0)),
  sharedServePrefix: (token: string) => `/api/v1/public/shared/${token}/files/serve/`,
}));

import SharedChatView from '../SharedChatView';
import type { SharedThreadMetadata } from '../api';

const metadata = { kind: 'thread', thread_id: 't1', title: 'Shared', workspace_name: '', msg_type: 'chat', created_at: '', updated_at: '', permissions: {} } as SharedThreadMetadata;

describe('SharedChatView inline widget replay', () => {
  beforeEach(() => {
    capturedMessages.length = 0;
  });

  it('replays each inline widget into the reply with its html, title and data', async () => {
    render(<SharedChatView shareToken="tok" metadata={metadata} />);
    await waitFor(() => {
      const last = capturedMessages[capturedMessages.length - 1] ?? [];
      const assistant = last.find((m) => m.role === 'assistant');
      expect(Object.keys((assistant?.htmlWidgetProcesses as object) ?? {})).toHaveLength(2);
    });
    const last = capturedMessages[capturedMessages.length - 1];
    const assistant = last.find((m) => m.role === 'assistant')!;
    const ordered = [...(assistant.contentSegments as Record<string, unknown>[])]
      .sort((a, b) => (a.order as number) - (b.order as number))
      .map((s) => (s.type === 'html_widget' ? s.widgetId : s.type));
    // The widget keeps its place among the reply's other segments.
    // Text, chart, text, chart: each widget stays between the text around it.
    expect(ordered.filter((k) => k === 'widget-w1' || k === 'widget-w2' || k === 'text')).toEqual([
      'text',
      'widget-w1',
      'text',
      'widget-w2',
    ]);
    expect(assistant.htmlWidgetProcesses).toEqual({
      'widget-w1': { html: '<div id="chart"></div>', title: 'Revenue', data: { 'rev.csv': 'q,v\n1,2' } },
      'widget-w2': { html: '<p>second</p>', title: 'Margin' },
    });
  });
});
