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
    onOpenThread: noop,
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
    // DALTON-HOOK(external-share-guard): follow-up is part of the action sources; this gate still only checks Publish
    onThumbDownFollowUp: async () => ({ status: 'ok' as const }),
    widgetPrompts: {},
    onEditMessage: noop,
    onRegenerate: noop,
    onRetry: noop,
    onForkFromTurn: async () => {},
    editDrafts: createEditDraftStore(() => 'th-publish-gate'),
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

  // DALTON-HOOK(automation-effort): retry reads getRetryModelOptions so saved composer effort is not auto-sent
  it('uses getRetryModelOptions for onRetry so saved composer effort is omitted unless explicitly picked', () => {
    const onRetry = vi.fn();
    const getModelOptions = vi.fn(() => ({
      model: 'claude-opus-5-5-oauth',
      reasoningEffort: 'low',
      fastMode: false,
    }));
    const getRetryModelOptions = vi.fn(() => ({
      model: 'claude-opus-5-5-oauth',
      reasoningEffort: null,
      fastMode: false,
    }));
    const { result } = renderHook(() =>
      useMessageActionBundles(
        sources({
          onRetry,
          chatInputRef: {
            current: {
              getModelOptions,
              getRetryModelOptions,
              addContext: noop,
              addWidgetSnapshot: () => false,
              setValue: noop,
              giveBack: noop,
              send: () => false,
              holdsSend: () => false,
              getAttachmentCount: () => 0,
              focus: noop,
            },
          },
        }),
      ),
    );

    // DALTON-HOOK(automation-effort): verify retry forwards getRetryModelOptions result without saved effort
    result.current.messageActions.onRetry?.();
    expect(getRetryModelOptions).toHaveBeenCalledOnce();
    expect(getModelOptions).not.toHaveBeenCalled();
    expect(onRetry).toHaveBeenCalledWith({
      model: 'claude-opus-5-5-oauth',
      reasoningEffort: null,
      fastMode: false,
    });
  });
});
