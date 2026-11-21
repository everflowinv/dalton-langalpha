/**
 * Create a workspace and refresh the lists that show it. The gallery and the
 * sidebar both open the create modal, and both hand it this.
 */
import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { apiErrorCoded } from '@/api/errors';
import { queryKeys } from '@/lib/queryKeys';
import type { DuplicateWorkspaceMatch, Workspace } from '@/types/api';

import { createWorkspace } from '../utils/api';
import { invalidateNewWorkspace } from './workspaceRowActions';

export interface NewWorkspace {
  name: string;
  description: string;
  /** The computer to put it on; omitted means the primary. */
  computerId?: string;
  // DALTON-HOOK(workspace-dedup): override duplicate company workspace check on explicit user confirmation
  allowDuplicateCompany?: boolean;
}

// DALTON-HOOK(workspace-dedup): structured payload extracted from 409 company_workspace_exists
export interface CompanyWorkspaceExistsInfo {
  message: string;
  matches: DuplicateWorkspaceMatch[];
}

export function companyWorkspaceExistsOf(err: unknown): CompanyWorkspaceExistsInfo | null {
  const detail = apiErrorCoded(err, 'company_workspace_exists');
  if (!detail) return null;
  const rawMatches = Array.isArray(detail.matches) ? detail.matches : [];
  const matches: DuplicateWorkspaceMatch[] = rawMatches.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const rec = item as Record<string, unknown>;
    const unboundUnit = Boolean(rec.unbound_unit);
    const workspaceName = typeof rec.workspace_name === 'string' ? rec.workspace_name : '';
    const ownerName = typeof rec.owner_name === 'string' ? rec.owner_name : '';
    const displayName = typeof rec.display_name === 'string' ? rec.display_name : '';
    const ticker = typeof rec.ticker === 'string' ? rec.ticker : '';
    if (unboundUnit) {
      if (!displayName && !ticker) return [];
    } else if (!workspaceName || !ownerName) {
      return [];
    }
    return [
      {
        workspace_id: typeof rec.workspace_id === 'string' ? rec.workspace_id : null,
        workspace_name: workspaceName || null,
        owner_name: ownerName || null,
        display_name: displayName || null,
        lead_analyst_name: typeof rec.lead_analyst_name === 'string' ? rec.lead_analyst_name : null,
        ticker: ticker || null,
        can_read: Boolean(rec.can_read),
        unbound_unit: unboundUnit,
      },
    ];
  });
  const message = typeof detail.message === 'string' && detail.message ? detail.message : '';
  return { message, matches };
}

export function useCreateWorkspace(): (data: NewWorkspace) => Promise<Workspace> {
  const queryClient = useQueryClient();
  return useCallback(
    async ({ name, description, computerId, allowDuplicateCompany }: NewWorkspace) => {
      let created: Workspace;
      try {
        created = allowDuplicateCompany
          ? await createWorkspace(name, description, {}, computerId, true)
          : await createWorkspace(name, description, {}, computerId);
      } catch (err) {
        // DALTON-HOOK(workspace-dedup): company_workspace_exists is a workspace dedup prompt, not a stale computer refusal.
        if (!companyWorkspaceExistsOf(err)) {
          // A refusal for a computer still being prepared means the list the
          // form chose from is behind, so the computers re-read.
          void queryClient.invalidateQueries({ queryKey: queryKeys.computers.lists() });
        }
        throw err;
      }
      // A new folder also lands on a machine, so the computer rows move too.
      invalidateNewWorkspace(queryClient);
      return created;
    },
    [queryClient],
  );
}
