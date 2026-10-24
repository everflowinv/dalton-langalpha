import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { motion, AnimatePresence } from '@/lib/framer';
import { FolderPlus, Check, X, ChevronRight, AlertTriangle } from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import { AUTO_APPROVE, useApproveAlways } from '@/hooks/useAutoApprove';

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
  const [collapsed, setCollapsed] = useState(true);
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
  const isApproved = status === 'approved';
  const isRejected = status === 'rejected';

  // --- Resolved (approved / rejected) ---
  if (isApproved || isRejected) {
    return (
      <div>
        <button
          onClick={() => setCollapsed((v) => !v)}
          className="flex items-center gap-2 py-1 cursor-pointer w-full text-left"
        >
          <motion.div
            animate={{ rotate: collapsed ? 0 : 90 }}
            transition={{ duration: 0.2 }}
          >
            <ChevronRight
              className="h-3.5 w-3.5 shrink-0"
              style={{ color: 'var(--color-icon-muted)' }}
            />
          </motion.div>
          {isApproved ? (
            <Check className="h-4 w-4 shrink-0" style={{ color: 'var(--color-accent-light)' }} />
          ) : (
            <X className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
          )}
          <span
            className="text-sm"
            style={{ color: isApproved ? 'var(--color-text-tertiary)' : 'var(--color-text-tertiary)' }}
          >
            {isApproved
              ? t('chat.createWorkspaceCard.created', { name: workspace_name })
              : t('chat.createWorkspaceCard.declined')}
          </span>
        </button>

        <AnimatePresence initial={false}>
          {!collapsed && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
              className="overflow-hidden"
            >
              <div className="pt-2 pb-1 pl-6">
                <div
                  className="rounded-lg px-4 py-3"
                  style={{
                    border: '1px solid var(--color-border-muted)',
                    opacity: isRejected ? 0.6 : 0.8,
                  }}
                >
                  <div className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {workspace_name}
                  </div>
                  {workspace_description && (
                    <div className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                      {workspace_description}
                    </div>
                  )}
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }

  // --- Pending: interactive ---
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
    >
      {/* Header */}
      <div className="flex items-center gap-2 pb-3">
        <FolderPlus className="h-4 w-4 shrink-0" style={{ color: 'var(--color-accent-light)' }} />
        <span className="text-[0.9375rem] font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('chat.createWorkspaceCard.title')}
        </span>
        <Loader
         
          size={14}
         
          className="ml-auto shrink-0 text-(--color-icon-muted)"
        />
      </div>

      {/* Workspace preview */}
      <div
        className="rounded-lg px-4 py-3"
        style={{ border: '1px solid var(--color-border-muted)' }}
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
      </div>

      {/* DALTON-HOOK(team-sharing): hide interactive approval actions in read-only mode */}
      {(onApprove || onReject) && (
      <div className="pt-3 flex items-center gap-2">
        <motion.button
          disabled={saving}
          onClick={(e: React.MouseEvent) => { e.stopPropagation(); onApprove?.(); }}
          className="flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium transition-colors hover:brightness-110"
          style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          <Check className="h-3.5 w-3.5 stroke-[2.5]" />
          {t('chat.createWorkspaceCard.create')}
        </motion.button>
        <motion.button
          disabled={saving}
          title={t('chat.createWorkspaceCard.alwaysApproveHint')}
          onClick={(e: React.MouseEvent) => { e.stopPropagation(); approveAlways(() => onApprove?.()); }}
          className="text-sm px-4 py-2 rounded-md font-medium transition-colors hover:bg-(--color-bg-hover)"
          style={{ border: '1px solid var(--color-border-default)', color: 'var(--color-text-secondary)' }}
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          {t('chat.createWorkspaceCard.alwaysApprove')}
        </motion.button>
        <motion.button
          disabled={saving}
          onClick={(e: React.MouseEvent) => { e.stopPropagation(); onReject?.(); }}
          className="flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium transition-colors"
          style={{
            backgroundColor: 'var(--color-border-muted)',
            color: 'var(--color-text-tertiary)',
          }}
          onMouseEnter={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.currentTarget.style.backgroundColor = 'var(--color-border-muted)';
            e.currentTarget.style.color = 'var(--color-text-tertiary)';
          }}
          onMouseLeave={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.currentTarget.style.backgroundColor = 'var(--color-border-muted)';
            e.currentTarget.style.color = 'var(--color-text-tertiary)';
          }}
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          <X className="h-3.5 w-3.5" />
          {t('chat.createWorkspaceCard.decline')}
        </motion.button>
        {/* DALTON-HOOK(team-sharing): read-only for a teammate's workspace */}
      </div>
      )}
    </motion.div>
  );
}

export default CreateWorkspaceCard;
