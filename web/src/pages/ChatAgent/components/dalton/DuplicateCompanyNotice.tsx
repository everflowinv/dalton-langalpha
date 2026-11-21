import React from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { buttonSlot } from '@/components/ui/button-class';
import type { DuplicateWorkspaceMatch } from '@/types/api';
import './DuplicateCompanyNotice.css';

interface DuplicateCompanyNoticeProps {
  requestedName: string;
  matches: DuplicateWorkspaceMatch[];
  disabled?: boolean;
  onCloseModal: () => void;
  onCreateAnyway: (requestedName: string) => void;
}

export function DuplicateCompanyNotice({
  requestedName,
  matches,
  disabled = false,
  onCloseModal,
  onCreateAnyway,
}: DuplicateCompanyNoticeProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const primary = matches[0];
  let summaryText: string;
  if (primary) {
    if (primary.can_read) {
      summaryText = primary.lead_analyst_name
        ? t(
          'workspace.duplicateCompany.summaryReadableWithLead',
          '{{company}} already has a workspace "{{workspace}}" (owner: {{owner}}, lead: {{lead}}). Continue there, or confirm to create another.',
          {
            company: requestedName,
            workspace: primary.workspace_name,
            owner: primary.owner_name,
            lead: primary.lead_analyst_name,
          },
        )
        : t(
          'workspace.duplicateCompany.summaryReadable',
          '{{company}} already has a workspace "{{workspace}}" (owner: {{owner}}). Continue there, or confirm to create another.',
          {
            company: requestedName,
            workspace: primary.workspace_name,
            owner: primary.owner_name,
          },
        );
    } else {
      summaryText = t(
        'workspace.duplicateCompany.summaryUnreadable',
        '{{company}} already has a workspace "{{workspace}}" (owner: {{owner}}). You do not have access yet — ask {{owner}} to share it, or confirm to create another.',
        {
          company: requestedName,
          workspace: primary.workspace_name,
          owner: primary.owner_name,
        },
      );
    }
  } else {
    summaryText = t(
      'workspace.duplicateCompany.summaryFallback',
      'A workspace for "{{company}}" already exists. Confirm to create another.',
      { company: requestedName },
    );
  }

  const handleOpenExistingWorkspace = (workspaceId: string, workspaceName: string) => {
    onCloseModal();
    navigate(`/chat/${workspaceId}`, {
      state: { workspaceName, workspaceStatus: null },
    });
  };

  return (
    <div className="cwm-dup-card" role="alert">
      <div className="cwm-dup-message">{summaryText}</div>
      {matches.length > 0 && (
        <ul className="cwm-dup-list">
          {matches.map((match, idx) => (
            <li
              key={match.workspace_id || `${match.owner_name}-${match.workspace_name}-${idx}`}
              className="cwm-dup-item"
            >
              <div className="cwm-dup-item-info">
                <div className="cwm-dup-item-title">
                  <span className="cwm-dup-ws-name">{match.workspace_name}</span>
                  {match.can_read && match.ticker && (
                    <span className="cwm-dup-ticker">{match.ticker}</span>
                  )}
                </div>
                <div className="cwm-dup-item-meta">
                  <span>
                    {t('workspace.duplicateCompany.ownerLabel', 'Owner: {{owner}}', {
                      owner: match.owner_name,
                    })}
                  </span>
                  {match.can_read && match.lead_analyst_name && (
                    <span>
                      {' · '}
                      {t('workspace.duplicateCompany.leadLabel', 'Lead: {{lead}}', {
                        lead: match.lead_analyst_name,
                      })}
                    </span>
                  )}
                </div>
              </div>
              <div className="cwm-dup-item-action">
                {match.can_read && match.workspace_id ? (
                  <button
                    type="button"
                    {...buttonSlot('outline')}
                    className="cwm-dup-open-btn"
                    onClick={() =>
                      handleOpenExistingWorkspace(match.workspace_id!, match.workspace_name)
                    }
                  >
                    {t('workspace.duplicateCompany.openWorkspace', 'Open this workspace')}
                  </button>
                ) : (
                  <span className="cwm-dup-unreadable">
                    {t('workspace.duplicateCompany.askOwnerShare', 'Ask the owner to share access')}
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="cwm-dup-footer">
        <button
          type="button"
          {...buttonSlot('outline')}
          className="cwm-dup-anyway-btn"
          disabled={disabled}
          onClick={() => onCreateAnyway(requestedName)}
        >
          {t('workspace.duplicateCompany.createAnyway', 'Create anyway')}
        </button>
      </div>
    </div>
  );
}
