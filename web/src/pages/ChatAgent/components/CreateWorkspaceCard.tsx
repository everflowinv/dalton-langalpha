import { useTranslation } from 'react-i18next';
import { FolderPlus, AlertTriangle } from 'lucide-react';
import { AUTO_APPROVE, useApproveAlways } from '@/hooks/useAutoApprove';
import { ProposalCard } from './ProposalCard';

interface ProposalData {
  workspace_name: string;
  workspace_description?: string;
  // DALTON-HOOK(cos-settings-tools): 4-state lifecycle + confirmation snapshot fields
  status: 'pending' | 'submitting' | 'approved' | 'rejected' | 'failed';
  // DALTON-HOOK(workspace-duplicate-guard): set when a live workspace already covers this name
  duplicate_workspace_id?: string;
  duplicate_workspace_name?: string;
  duplicate_check_unavailable?: boolean;
  // DALTON-HOOK(cos-settings-tools): target computer snapshot & failure details
  computer_id?: string | null;
  computer_name?: string | null;
  computer_is_own_machine?: boolean;
  bootstrap_cloud_primary?: boolean;
  tool_call_id?: string;
  confirm_token?: string;
  failure_code?: string;
  failure_error?: string;
}

interface CreateWorkspaceCardProps {
  proposalData: ProposalData | null;
  onApprove?: () => void;
  onReject?: () => void;
}

/**
 * CreateWorkspaceCard - Inline HITL card for workspace creation approval.
 *
 * Three states:
 *   pending  - workspace name + description, Approve/Reject buttons
 *   approved - collapsed "Workspace created: [name]", expandable
 *   rejected - collapsed "Workspace creation declined"
 */
function CreateWorkspaceCard({ proposalData, onApprove, onReject }: CreateWorkspaceCardProps) {
  const { t } = useTranslation();
  const { approveAlways, saving } = useApproveAlways(AUTO_APPROVE.workspaceCreation);

  if (!proposalData) return null;

  const { workspace_name, workspace_description, status } = proposalData;
  // DALTON-HOOK(workspace-duplicate-guard): warn the user a workspace covering
  // this name exists, or that the check could not run
  const duplicate = proposalData.duplicate_workspace_name
    ? `${proposalData.duplicate_workspace_name} (${proposalData.duplicate_workspace_id ?? ''})`
    : null;
  const duplicateUnknown =
    !duplicate && proposalData.duplicate_check_unavailable === true;
  // DALTON-HOOK(cos-settings-tools): resolve snapshot target computer label and 4-state settled copy
  const computerLabel = proposalData.bootstrap_cloud_primary
    ? t('chat.createWorkspaceCard.newCloudComputer')
    : proposalData.computer_name || null;
  const settledLabel =
    status === 'approved'
      ? computerLabel
        ? t('chat.createWorkspaceCard.createdOnComputer', { name: workspace_name, computer: computerLabel })
        : t('chat.createWorkspaceCard.created', { name: workspace_name })
      : status === 'failed'
        ? proposalData.failure_code === 'scope_changed'
          ? t('chat.createWorkspaceCard.scopeChanged')
          : proposalData.failure_error
            ? t('chat.createWorkspaceCard.failedWithReason', { reason: proposalData.failure_error })
            : t('chat.createWorkspaceCard.failed')
        : t('chat.createWorkspaceCard.declined');

  return (
    <ProposalCard
      icon={FolderPlus}
      title={t('chat.createWorkspaceCard.title')}
      status={status}
      settledLabel={settledLabel}
      approveLabel={
        status === 'submitting'
          ? t('chat.createWorkspaceCard.creating')
          : t('chat.createWorkspaceCard.create')
      }
      rejectLabel={t('chat.createWorkspaceCard.decline')}
      onApprove={onApprove}
      onReject={onReject}
      // DALTON-HOOK(team-sharing): no handlers in read-only mode, so no buttons at all
      // DALTON-HOOK(cos-settings-tools): hide "Always allow" for user's own machines
      approveAlways={(onApprove || onReject) && !proposalData.computer_is_own_machine ? {
        label: t('chat.createWorkspaceCard.alwaysApprove'),
        hint: t('chat.createWorkspaceCard.alwaysApproveHint'),
        onClick: () => approveAlways(() => onApprove?.()),
      } : undefined}
      busy={saving || status === 'submitting'}
    >
      <div className="text-sm font-medium break-words min-w-0" style={{ color: 'var(--color-text-primary)' }}>
        {workspace_name}
      </div>
      {workspace_description && (
        <div className="text-sm mt-1 break-words min-w-0" style={{ color: 'var(--color-text-tertiary)' }}>
          {workspace_description}
        </div>
      )}
      {computerLabel && (
        <div className="text-sm mt-1.5 break-words min-w-0" style={{ color: 'var(--color-text-secondary)' }}>
          {t('chat.createWorkspaceCard.targetComputer', { computer: computerLabel })}
        </div>
      )}
      {(duplicate || duplicateUnknown) && (
        <div className="text-sm mt-2 flex items-start gap-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          {/* DALTON-HOOK(ui-unify): use declared semantic warning token without hex fallback */}
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" style={{ color: 'var(--color-warning)' }} />
          <span>
            {duplicate
              ? t('chat.createWorkspaceCard.duplicateExists', { duplicate })
              : t('chat.createWorkspaceCard.duplicateUnknown')}
          </span>
        </div>
      )}
    </ProposalCard>
  );
}

export default CreateWorkspaceCard;
