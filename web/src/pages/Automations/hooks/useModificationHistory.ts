// Modification history for an open task, plus rollback and restore.
import { useCallback, useEffect, useState } from 'react';
import { apiErrorStatus } from '@/api/errors';
import type { AutomationHistory, AutomationHistoryEntry } from '@/types/automation';
import { useAutomationHistory } from './useAutomationReads';
import { useAutomationMutations } from './useAutomationMutations';
import { historyEntriesDesc } from '../utils/historyView';
import { configVersionOf, isDeletedTask, isForbiddenError } from '../utils/taskKind';

interface HistoryTask {
  automation_id: string;
  config_version?: unknown;
  deleted_at?: string | null;
}

/** Further history pages. The route accepts limit and offset, up to 200. */
const HISTORY_PROBE_LIMIT = 200;

/**
 * Loads history as soon as the inspector opens, so the action bar can offer
 * rollback before the history tab is selected. A company 403 is reported to
 * the page. A 404 on a deleted script stays on this task.
 *
 * The first page is the newest 50 entries. The previous version can sit on a
 * later page, so further pages load until that snapshot arrives. Rollback
 * stays off until then.
 */
export function useModificationHistory(
  task: HistoryTask,
  company: boolean,
  onForbidden?: (automationId: string) => void,
  onRestored?: () => void,
) {
  const [recordTab, setRecordTab] = useState<'runs' | 'history'>('runs');
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const historyQuery = useAutomationHistory(task.automation_id);
  const { restore, rollback, busy } = useAutomationMutations();
  const deleted = isDeletedTask(task);
  const version = configVersionOf(task);
  const historyForbidden = company && isForbiddenError(historyQuery.error);

  useEffect(() => {
    if (historyForbidden) onForbidden?.(task.automation_id);
  }, [historyForbidden, onForbidden, task.automation_id]);

  const entries = historyEntriesDesc(historyQuery.data?.entries);
  const activeVersion = historyQuery.data?.active_version;
  const current = activeVersion == null ? undefined : entries.find((entry) => entry.version === activeVersion);
  const prevVersion = !deleted && current?.prev_version != null ? current.prev_version : null;

  const [probeOffset, setProbeOffset] = useState<number | null>(null);
  const [extraEntries, setExtraEntries] = useState<AutomationHistoryEntry[]>([]);
  const [probeDone, setProbeDone] = useState(false);
  const probeKey = `${task.automation_id}:${prevVersion ?? ''}`;
  const [seenProbeKey, setSeenProbeKey] = useState(probeKey);
  if (seenProbeKey !== probeKey) {
    setSeenProbeKey(probeKey);
    setProbeOffset(null);
    setExtraEntries([]);
    setProbeDone(false);
  }

  const first = historyQuery.data;
  const firstEnd = first ? first.offset + first.entries.length : 0;
  const onFirstPage = prevVersion == null ? undefined : entries.find((entry) => entry.version === prevVersion);
  const locatedExtra = prevVersion == null ? undefined : extraEntries.find((entry) => entry.version === prevVersion);
  const more = !!first && first.entries.length > 0 && firstEnd < first.total;
  const shouldProbe =
    prevVersion != null && onFirstPage == null && locatedExtra == null && more && historyQuery.isSuccess && !probeDone;
  const requestOffset = probeOffset ?? firstEnd;
  const probe = useAutomationHistory(task.automation_id, {
    limit: HISTORY_PROBE_LIMIT,
    offset: requestOffset,
    enabled: shouldProbe,
  });

  useEffect(() => {
    if (!shouldProbe || !probe.isSuccess || !probe.data) return;
    // A stale page from the previous offset is not this request.
    if (probe.data.offset !== requestOffset) return;
    const page = probe.data.entries ?? [];
    setExtraEntries((current) => {
      const seen = new Set(current.map((entry) => entry.version));
      const added = page.filter((entry) => !seen.has(entry.version));
      return added.length === 0 ? current : [...current, ...added];
    });
    const found = page.some((entry) => entry.version === prevVersion);
    const next = probe.data.offset + page.length;
    if (found || page.length === 0 || next >= probe.data.total) setProbeDone(true);
    else setProbeOffset(next);
  }, [shouldProbe, probe.isSuccess, probe.data, requestOffset, prevVersion]);

  useEffect(() => {
    if (!shouldProbe || !probe.isError) return;
    // A later page can be refused for a company task. A 404 is not that refusal.
    if (company && isForbiddenError(probe.error)) {
      onForbidden?.(task.automation_id);
      return;
    }
    setProbeDone(true);
  }, [shouldProbe, probe.isError, probe.error, company, onForbidden, task.automation_id]);

  const located = onFirstPage ?? locatedExtra;
  // An empty snapshot object counts as loaded. A version the pages never returned does not.
  const snapshotReady = prevVersion != null && located != null;
  const snapshotPending =
    prevVersion != null && !snapshotReady && !probeDone && (historyQuery.isLoading || shouldProbe || probe.isFetching);
  const targetSnapshot = located?.snapshot ?? null;

  const restoreTask = useCallback(async () => {
    if (version == null) return;
    try {
      await restore.mutateAsync({ id: task.automation_id, expectedConfigVersion: version });
      onRestored?.();
    } catch {
      // The mutation reports the failure and refreshes the task on a version conflict.
    }
  }, [onRestored, restore, task.automation_id, version]);

  const confirmRollback = useCallback(async () => {
    if (prevVersion == null || version == null || !snapshotReady) return;
    try {
      await rollback.mutateAsync({
        id: task.automation_id,
        targetVersion: prevVersion,
        expectedConfigVersion: version,
      });
      setRollbackOpen(false);
    } catch (err) {
      if (apiErrorStatus(err) === 409) setRollbackOpen(false);
    }
  }, [prevVersion, rollback, snapshotReady, task.automation_id, version]);

  const history: AutomationHistory | undefined = historyQuery.data;
  return {
    recordTab,
    setRecordTab,
    rollbackOpen,
    setRollbackOpen,
    history,
    historyLoading: historyQuery.isLoading,
    historyFailed: historyQuery.isError && !historyForbidden,
    historyForbidden,
    prevVersion,
    targetSnapshot,
    snapshotReady,
    snapshotPending,
    version,
    busy,
    restoreTask,
    confirmRollback,
  };
}
