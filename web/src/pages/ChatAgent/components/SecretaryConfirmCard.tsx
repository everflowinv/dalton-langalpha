import { useState } from 'react';
import { Archive, Trash2, Square, MessageSquareX, type LucideIcon } from 'lucide-react'; // DALTON-HOOK(cos-settings-tools)
import { useTranslation } from 'react-i18next';
import type { ArchiveWorkspaceSnapshotItem } from '@/types/chat'; // DALTON-HOOK(cos-settings-tools)
import { ProposalCard } from './ProposalCard';

// DALTON-HOOK(cos-settings-tools): support archive_workspace with 4-state lifecycle and whole-computer list
type SecretaryActionType = 'delete_workspace' | 'stop_workspace' | 'delete_thread' | 'archive_workspace';

const ARCHIVE_VISIBLE_LIMIT = 5;

interface ProposalData {
  actionType: SecretaryActionType;
  workspace_id?: string;
  /** Sent with a workspace delete; older interrupts carry only the id. */
  workspace_name?: string | null;
  thread_id?: string;
  computer_id?: string | null;
  computer_name?: string | null;
  expected_workspaces?: ArchiveWorkspaceSnapshotItem[];
  archived_workspace_count?: number;
  tool_call_id?: string;
  failure_code?: string;
  failure_error?: string;
  status: 'pending' | 'submitting' | 'approved' | 'rejected' | 'failed';
}

interface SecretaryConfirmCardProps {
  proposalData: ProposalData | null;
  onApprove?: () => void;
  onReject?: () => void;
}

const ACTION_CONFIG: Record<SecretaryActionType, {
  icon: LucideIcon;
  titleKey: string;
  approvedKey: string;
  rejectedKey: string;
  idLabelKey: string;
  idField: 'workspace_id' | 'thread_id';
}> = {
  delete_workspace: {
    icon: Trash2,
    titleKey: 'chat.secretaryConfirm.deleteWorkspace.title',
    approvedKey: 'chat.secretaryConfirm.deleteWorkspace.approved',
    rejectedKey: 'chat.secretaryConfirm.deleteWorkspace.rejected',
    idLabelKey: 'chat.secretaryConfirm.workspaceLabel',
    idField: 'workspace_id',
  },
  stop_workspace: {
    icon: Square,
    titleKey: 'chat.secretaryConfirm.stopWorkspace.title',
    approvedKey: 'chat.secretaryConfirm.stopWorkspace.approved',
    rejectedKey: 'chat.secretaryConfirm.stopWorkspace.rejected',
    idLabelKey: 'chat.secretaryConfirm.workspaceLabel',
    idField: 'workspace_id',
  },
  delete_thread: {
    icon: MessageSquareX,
    titleKey: 'chat.secretaryConfirm.deleteThread.title',
    approvedKey: 'chat.secretaryConfirm.deleteThread.approved',
    rejectedKey: 'chat.secretaryConfirm.deleteThread.rejected',
    idLabelKey: 'chat.secretaryConfirm.threadLabel',
    idField: 'thread_id',
  },
  archive_workspace: {
    icon: Archive,
    titleKey: 'chat.secretaryConfirm.archiveWorkspace.title',
    approvedKey: 'chat.secretaryConfirm.archiveWorkspace.approved',
    rejectedKey: 'chat.secretaryConfirm.archiveWorkspace.rejected',
    idLabelKey: 'chat.secretaryConfirm.workspaceLabel',
    idField: 'workspace_id',
  },
};

/**
 * SecretaryConfirmCard - Generic HITL confirmation card for secretary actions.
 *
 * Handles delete_workspace, stop_workspace, delete_thread, and archive_workspace.
 *
 * DALTON-HOOK(team-sharing): read-only mode hides the buttons in ProposalCard
 * (no action handlers are passed from MessageContentSegments).
 */
function SecretaryConfirmCard({ proposalData, onApprove, onReject }: SecretaryConfirmCardProps) {
  const { t } = useTranslation();
  const [expandedWorkspaces, setExpandedWorkspaces] = useState(false);

  if (!proposalData) return null;

  const { actionType, status } = proposalData;
  const config = ACTION_CONFIG[actionType];
  if (!config) return null;

  // --- DALTON-HOOK(cos-settings-tools): begin ---
  if (actionType === 'archive_workspace') {
    const visibleWorkspaces = (proposalData.expected_workspaces || []).filter(
      (ws) => !ws.is_stand_in,
    );
    const totalCount =
      typeof proposalData.archived_workspace_count === 'number'
        ? proposalData.archived_workspace_count
        : visibleWorkspaces.length;
    const computerLabel = proposalData.computer_name || proposalData.computer_id || t('chat.secretaryConfirm.unknownId');
    const shownWorkspaces = expandedWorkspaces
      ? visibleWorkspaces
      : visibleWorkspaces.slice(0, ARCHIVE_VISIBLE_LIMIT);
    const hasMore = visibleWorkspaces.length > ARCHIVE_VISIBLE_LIMIT;

    const settledLabel =
      status === 'approved'
        ? t('chat.secretaryConfirm.archiveWorkspace.approved', {
            computer: computerLabel,
            count: totalCount,
          })
        : status === 'failed'
          ? proposalData.failure_code === 'scope_changed'
            ? t('chat.secretaryConfirm.archiveWorkspace.scopeChanged')
            : proposalData.failure_error
              ? t('chat.secretaryConfirm.archiveWorkspace.failedWithReason', {
                  reason: proposalData.failure_error,
                })
              : t('chat.secretaryConfirm.archiveWorkspace.failed')
          : t('chat.secretaryConfirm.archiveWorkspace.rejected');

    const detail = (
      <div className="flex flex-col gap-2 text-sm min-w-0">
        <div className="break-words min-w-0" style={{ color: 'var(--color-text-primary)' }}>
          <span className="font-medium">{t('chat.secretaryConfirm.archiveWorkspace.computerLabel')}</span>{' '}
          <span>{computerLabel}</span>
        </div>
        <div className="text-xs break-words min-w-0" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('chat.secretaryConfirm.archiveWorkspace.affectedHint', { count: visibleWorkspaces.length })}
        </div>
        {visibleWorkspaces.length > 0 && (
          <ul className="flex flex-col gap-1 text-xs min-w-0" style={{ color: 'var(--color-text-secondary)' }}>
            {shownWorkspaces.map((ws) => (
              <li key={ws.workspace_id} className="flex items-center justify-between gap-2 min-w-0">
                <span className="break-words min-w-0 font-medium">{ws.name || ws.workspace_id}</span>
                {ws.status && (
                  <span className="shrink-0 opacity-75">{ws.status}</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {hasMore && (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setExpandedWorkspaces((v) => !v); }}
            className="self-start text-xs underline"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {expandedWorkspaces
              ? t('chat.secretaryConfirm.archiveWorkspace.collapseWorkspaces')
              : t('chat.secretaryConfirm.archiveWorkspace.expandWorkspaces', { count: visibleWorkspaces.length })}
          </button>
        )}
      </div>
    );

    return (
      <ProposalCard
        icon={config.icon}
        title={t(config.titleKey)}
        status={status}
        settledLabel={settledLabel}
        approveLabel={
          status === 'submitting'
            ? t('chat.secretaryConfirm.archiveWorkspace.archiving')
            : t('chat.secretaryConfirm.archiveWorkspace.confirm')
        }
        rejectLabel={t('chat.secretaryConfirm.archiveWorkspace.decline')}
        onApprove={onApprove}
        onReject={onReject}
        busy={status === 'submitting'}
        settledDetail={detail}
      >
        {detail}
      </ProposalCard>
    );
  }
  // --- DALTON-HOOK(cos-settings-tools): end ---

  const targetId = proposalData[config.idField] || t('chat.secretaryConfirm.unknownId');
  const shortId = targetId.length > 12 ? `${targetId.slice(0, 8)}...` : targetId;
  const targetName = config.idField === 'workspace_id' ? proposalData.workspace_name : null;
  // The card has room only for the start of a bare id; the settled row opens
  // onto all of it.
  const target = (id: string) => (
    <div className="text-sm break-words min-w-0" style={{ color: 'var(--color-text-tertiary)' }}>
      <span className="font-medium">{t(config.idLabelKey)}</span>{' '}
      {targetName
        ? <span>{targetName}</span>
        : <span className="font-mono text-xs">{id}</span>}
    </div>
  );

  return (
    <ProposalCard
      icon={config.icon}
      title={t(config.titleKey)}
      status={status}
      settledLabel={t(status === 'approved' ? config.approvedKey : config.rejectedKey)}
      approveLabel={t('chat.secretaryConfirm.confirm')}
      rejectLabel={t('chat.secretaryConfirm.decline')}
      onApprove={onApprove}
      onReject={onReject}
      settledDetail={target(targetId)}
    >
      {target(shortId)}
    </ProposalCard>
  );
}

export default SecretaryConfirmCard;
