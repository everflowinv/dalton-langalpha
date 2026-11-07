/**
 * sync3 B11 r4 review fix: the transcript's file cards offer Publish only
 * where the caller may publish — the workspace's own owner. A colleague (rw
 * or ro), an external member, or a read-only thread gets no entry.
 */
import { describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useMessageActionBundles, type MessageActionSources } from '../useMessageActionBundles';
import { createEditDraftStore } from '@/pages/ChatAgent/hooks/utils/editDrafts';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

const noop = () => {};

function sources(overrides: Partial<MessageActionSources> = {}): MessageActionSources {
  return {
    onOpenFile: noop,
    onDownloadFile: noop,
    onPublishFile: async () => {},
    downloadKeyFor: (p: string) => p,
    onRevealFiles: noop,
    onRevealMessage: () => false,
    onOpenSources: noop,
    onToolCallDetailClick: noop,
    onOpenChart: noop,
    onOpenArtifactComment: noop,
    onOpenSubagentTask: noop,
    onOpenDispatcher: noop,
    onAnswerQuestion: noop,
    onSkipQuestion: noop,
    onApproveCreateWorkspace: noop,
    onRejectCreateWorkspace: noop,
    onApproveStartQuestion: noop,
    onRejectStartQuestion: noop,
    onApprovePTCAgent: noop,
    onRejectPTCAgent: noop,
    onApproveSecretaryAction: noop,
    onRejectSecretaryAction: noop,
    onResumeCreditPause: noop,
    onApproveToolCall: noop,
    onRejectToolCall: noop,
    onThumbUp: async () => null,
    onThumbDown: async () => null,
    onWidgetSendPrompt: noop,
    onEditMessage: noop,
    onRegenerate: noop,
    onRetry: noop,
    editDrafts: createEditDraftStore(() => 'th-publish-gate'),
    onSendMessage: noop,
    chatInputRef: { current: null },
    ...overrides,
  };
}

describe('useMessageActionBundles: the Publish entry follows the caller', () => {
  it('offers Publish to the owner', () => {
    const { result } = renderHook(() => useMessageActionBundles(sources()));
    expect(typeof result.current.messageActions.onPublishFile).toBe('function');
  });

  it('hides Publish for a colleague, an external member and a read-only thread', () => {
    const colleague = renderHook(() => useMessageActionBundles(sources({ publishAllowed: false })));
    expect(colleague.result.current.messageActions.onPublishFile).toBeUndefined();
    expect(colleague.result.current.subagentMessageActions.onPublishFile).toBeUndefined();

    const readOnly = renderHook(() => useMessageActionBundles(sources({ readOnly: true })));
    expect(readOnly.result.current.messageActions.onPublishFile).toBeUndefined();
    expect(readOnly.result.current.subagentMessageActions.onPublishFile).toBeUndefined();
  });
});
