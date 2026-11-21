/**
 * Create a workspace and refresh the lists that show it. The gallery and the
 * sidebar both open the create modal, and both hand it this.
 */
import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { apiErrorCoded } from '@/api/errors';
import { queryKeys } from '@/lib/queryKeys';
import type {
  CoverageUnitKindChoice,
  DedupCoverageSuggestion,
  DuplicateWorkspaceMatch,
  Workspace,
} from '@/types/api';

import { createWorkspace } from '../utils/api';
import { invalidateNewWorkspace } from './workspaceRowActions';

export interface NewWorkspace {
  name: string;
  description: string;
  /** The computer to put it on; omitted means the primary. */
  computerId?: string;
  // DALTON-HOOK(workspace-dedup): override duplicate company workspace check and register coverage unit
  allowDuplicateCompany?: boolean;
  coverageKind?: CoverageUnitKindChoice;
  ticker?: string;
  displayName?: string;
}

// DALTON-HOOK(workspace-dedup): structured payload extracted from 409 company_workspace_exists
export interface CompanyWorkspaceExistsInfo {
  message: string;
  matches: DuplicateWorkspaceMatch[];
  suggestion?: DedupCoverageSuggestion;
}

function parseSuggestion(raw: unknown): DedupCoverageSuggestion | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const rec = raw as Record<string, unknown>;
  const rawKind = typeof rec.coverage_kind === 'string' ? rec.coverage_kind.trim().toLowerCase() : '';
  if (rawKind !== 'company' && rawKind !== 'sector') return undefined;
  const ticker = typeof rec.ticker === 'string' && rec.ticker.trim() ? rec.ticker.trim() : null;
  const displayName = typeof rec.display_name === 'string' && rec.display_name.trim() ? rec.display_name.trim() : null;
  return {
    coverage_kind: rawKind,
    ticker,
    display_name: displayName,
  };
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
  const suggestion = parseSuggestion(detail.suggestion);
  return suggestion ? { message, matches, suggestion } : { message, matches };
}

export function useCreateWorkspace(): (data: NewWorkspace) => Promise<Workspace> {
  const queryClient = useQueryClient();
  return useCallback(
    async ({ name, description, computerId, allowDuplicateCompany, coverageKind, ticker, displayName }: NewWorkspace) => {
      let created: Workspace;
      try {
        const hasCoverage = coverageKind === 'company' || coverageKind === 'sector';
        created = hasCoverage
          ? await createWorkspace(name, description, {}, computerId, allowDuplicateCompany, {
              kind: coverageKind,
              ticker,
              displayName,
            })
          : allowDuplicateCompany
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
      // DALTON-HOOK(workspace-dedup): refresh coverage units & unclassified lists after creating a workspace.
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.coverageUnits() });
      return created;
    },
    [queryClient],
  );
}
