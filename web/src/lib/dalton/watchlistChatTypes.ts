/**
 * Personal Watchlist confirmation card types (Dalton-owned; re-exported thinly in `@/types/chat`).
 */

export type WatchlistProposalActionType =
  | 'watchlist_batch_add'
  | 'watchlist_set_tier'
  | 'watchlist_remove'
  | 'watchlist_tier_policy';

export interface WatchlistProposalSegment {
  type: WatchlistProposalActionType;
  proposalId: string;
  order: number;
}

export interface WatchlistProposalAffectedTicker {
  ticker: string;
  name?: string;
  old_tier?: string | null;
  new_tier?: string | null;
  has_workspace?: boolean;
  workspace_id?: string | null;
  is_private?: boolean;
}

export interface WatchlistProposalWorkspaceTaskChange {
  name: string;
  change: string;
  old_cron?: string | null;
  new_cron?: string | null;
  reason?: string;
}

export interface WatchlistProposalAffectedWorkspace {
  ticker: string;
  workspace_id: string;
  old_tier?: string | null;
  new_tier?: string | null;
  task_changes?: WatchlistProposalWorkspaceTaskChange[];
}

export interface WatchlistProposalState {
  status: 'pending' | 'submitting' | 'approved' | 'rejected' | 'failed';
  actionType: WatchlistProposalActionType;
  raw_action?: string;
  name?: string;
  description?: string;
  owner_user_id?: string;
  expected_roster_hash?: string;
  expected_policy_versions?: Record<string, number>;
  target_tickers?: string[];
  target_tier?: string | null;
  affected_tickers?: WatchlistProposalAffectedTicker[];
  affected_workspaces?: WatchlistProposalAffectedWorkspace[];
  task_summary?: {
    create_count?: number;
    update_count?: number;
    resume_count?: number;
    pause_count?: number;
    preserved_user_pause_count?: number;
    preserved_tracker_count?: number;
    preserved_handoff_count?: number;
  };
  preserve_workspace_notice?: string | null;
  group_id?: string | null;
  group_label?: string | null;
  tool_call_id?: string;
  failure_code?: string;
  failure_error?: string;
  reconcile_status?: string;
  job_id?: string | null;
  interruptId?: string;
}
