import { useTranslation } from 'react-i18next';
import { FolderPlus, AlertTriangle } from 'lucide-react';
import { AUTO_APPROVE, useApproveAlways } from '@/hooks/useAutoApprove';
import { ProposalCard } from './ProposalCard';

interface ProposalData {
  workspace_name: string;
  workspace_description?: string;
  status: 'pending' | 'approved' | 'rejected';
  // DALTON-HOOK(workspace-duplicate-guard): set when a live workspace already covers this name
  duplicate_workspace_id?: string;
  duplicate_workspace_name?: string;
  duplicate_check_unavailable?: boolean;
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

  return (
    <ProposalCard
      icon={FolderPlus}
      title={t('chat.createWorkspaceCard.title')}
      status={status}
      settledLabel={
        status === 'approved'
          ? t('chat.createWorkspaceCard.created', { name: workspace_name })
          : t('chat.createWorkspaceCard.declined')
      }
      approveLabel={t('chat.createWorkspaceCard.create')}
      rejectLabel={t('chat.createWorkspaceCard.decline')}
      onApprove={onApprove}
      onReject={onReject}
      // DALTON-HOOK(team-sharing): no handlers in read-only mode, so no buttons at all
      approveAlways={onApprove || onReject ? {
        label: t('chat.createWorkspaceCard.alwaysApprove'),
        hint: t('chat.createWorkspaceCard.alwaysApproveHint'),
        onClick: () => approveAlways(() => onApprove?.()),
      } : undefined}
      busy={saving}
    >
      <div className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
        {workspace_name}
      </div>
      {workspace_description && (
        <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
          {workspace_description}
        </div>
      )}
      {(duplicate || duplicateUnknown) && (
        <div className="text-sm mt-2 flex items-start gap-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" style={{ color: 'var(--color-warning, #d97706)' }} />
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
