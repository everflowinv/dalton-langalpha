// DALTON-HOOK(company-automations): company writes send the detail config_version and refresh only that company access on 403.
import { useIsMutating, useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from '@/components/ui/use-toast';
import { FAIL_FAST_OFFLINE } from '@/lib/network';
import { queryKeys } from '@/lib/queryKeys';
import type { AutomationPayload, AutomationUpdatePayload, CompanyScriptUpdatePayload } from '@/types/automation';
import { apiErrorStatus } from '@/api/errors';
import * as automationApi from '../utils/api';
import { mutationErrorMessage } from '../utils/errors';

/** A personal id, or a company id together with the detail's `config_version`. */
export type AutomationWriteTarget = string | { id: string; expectedConfigVersion: number };

function writeTarget(target: AutomationWriteTarget): { id: string; expectedConfigVersion?: number } {
  return typeof target === 'string' ? { id: target } : target;
}

/** Every write carries it, so `busy` counts them all: two overlapping writes
 *  keep the buttons off until the second one settles too. */
const WRITE_KEY = ['automation-write'] as const;

/** A company save, pause, or resume carries the detail version. A personal id does not. */
function companyWriteId(variables: unknown): string | null {
  if (!variables || typeof variables !== 'object') return null;
  const record = variables as { id?: unknown; expectedConfigVersion?: unknown; data?: unknown };
  if (typeof record.expectedConfigVersion === 'number' && typeof record.id === 'string') return record.id;
  if (!record.data || typeof record.data !== 'object' || typeof record.id !== 'string') return null;
  const version = (record.data as { expected_config_version?: unknown }).expected_config_version;
  return typeof version === 'number' ? record.id : null;
}

/** Company list pages, plus this task's detail. Personal and research reads stay cached. */
function refreshCompanyAccess(queryClient: QueryClient, automationId: string) {
  return queryClient.invalidateQueries({
    queryKey: [...queryKeys.automations.all, 'read'],
    predicate: (query) => {
      const key = query.queryKey;
      if (key[2] === 'list') {
        const params = key[4];
        return !!params && typeof params === 'object' && (params as { scope?: unknown }).scope === 'company';
      }
      return key[2] === 'detail' && key[4] === automationId;
    },
  });
}

/**
 * Every successful write invalidates the whole automations family: a pause
 * moves a row between groups, a trigger adds a run to the feed and to that
 * automation's history. Success is announced only where nothing on screen
 * changes at once: a triggered run takes a moment to appear, everything else
 * is its own proof. A failure is announced here, so a button fires a verb
 * with `mutate` and forgets it, while `mutateAsync` still rejects for a
 * caller that branches. A 409 re-reads the family and still rejects, so the
 * caller can keep its draft. A 403 from a company save, pause, or resume
 * re-reads that company list and that task only.
 */
export function useAutomationMutations() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const shared = {
    // The form and the delete dialog wait on these, and a write paused
    // offline would hold them with no way out.
    ...FAIL_FAST_OFFLINE,
    mutationKey: WRITE_KEY,
    // Returned, so the write stays pending until the lists it changed are
    // read again and no button comes back on over stale rows.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.automations.all }),
    onError: (err: unknown, variables: unknown) => {
      // A 409 keeps the caller's draft. Re-read the task; do not submit again here.
      if (apiErrorStatus(err) === 409) {
        const detail = mutationErrorMessage(err, '');
        toast({
          description: detail ? t('automation.configUpdatedDetail', { detail }) : t('automation.configUpdated'),
        });
        return queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
      }
      toast({ variant: 'destructive', description: mutationErrorMessage(err, t('automation.actionFailed')) });
      // DALTON-HOOK(company-automations): a company 403 re-reads that company list and that task. A 409 re-reads the family.
      const companyId = apiErrorStatus(err) === 403 ? companyWriteId(variables) : null;
      if (companyId) return refreshCompanyAccess(queryClient, companyId);
    },
  };

  const create = useMutation({
    ...shared,
    mutationFn: async (data: AutomationPayload) => (await automationApi.createAutomation(data)).data,
  });
  const update = useMutation({
    ...shared,
    mutationFn: async ({
      id,
      data,
    }: {
      id: string;
      data: AutomationUpdatePayload & { expected_config_version?: number };
    }) => (await automationApi.updateAutomation(id, data)).data,
  });
  const updateScript = useMutation({
    ...shared,
    mutationFn: async ({ id, data }: { id: string; data: CompanyScriptUpdatePayload }) =>
      (await automationApi.updateCompanyScript(id, data)).data,
  });
  // Delete, restore, and rollback say the row changed under the reader. Other writes keep the shared 409 copy.
  const versionConflict = (err: unknown, variables: unknown) => {
    if (apiErrorStatus(err) === 409) {
      toast({ description: t('automation.versionConflict') });
      return queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
    }
    return shared.onError(err, variables);
  };
  const remove = useMutation({
    ...shared,
    mutationFn: ({ id, expectedConfigVersion }: { id: string; expectedConfigVersion: number }) =>
      automationApi.deleteAutomation(id, expectedConfigVersion),
    onError: versionConflict,
  });
  const restore = useMutation({
    ...shared,
    mutationFn: ({ id, expectedConfigVersion }: { id: string; expectedConfigVersion: number }) =>
      automationApi.restoreAutomation(id, expectedConfigVersion),
    onError: versionConflict,
  });
  const rollback = useMutation({
    ...shared,
    mutationFn: ({
      id,
      targetVersion,
      expectedConfigVersion,
    }: {
      id: string;
      targetVersion: number;
      expectedConfigVersion: number;
    }) => automationApi.rollbackAutomation(id, targetVersion, expectedConfigVersion),
    onError: versionConflict,
  });
  // DALTON-HOOK(company-automations): company pause and resume send the detail version. Personal calls stay one argument.
  const pause = useMutation({
    ...shared,
    mutationFn: (target: AutomationWriteTarget) => {
      const { id, expectedConfigVersion } = writeTarget(target);
      return expectedConfigVersion == null
        ? automationApi.pauseAutomation(id)
        : automationApi.pauseAutomation(id, expectedConfigVersion);
    },
  });
  const resume = useMutation({
    ...shared,
    mutationFn: (target: AutomationWriteTarget) => {
      const { id, expectedConfigVersion } = writeTarget(target);
      return expectedConfigVersion == null
        ? automationApi.resumeAutomation(id)
        : automationApi.resumeAutomation(id, expectedConfigVersion);
    },
  });
  const trigger = useMutation({
    ...shared,
    mutationFn: (id: string) => automationApi.triggerAutomation(id),
    onSuccess: () => {
      toast({ description: t('automation.runStarted') });
      return shared.onSuccess();
    },
  });
  // A 409 means the run started or settled before the click: there is
  // nothing left to skip, and the refetch every outcome gets shows where it went.
  const skip = useMutation({
    ...shared,
    mutationFn: async ({ automationId, executionId }: { automationId: string; executionId: string }) => {
      try {
        return await automationApi.skipRun(automationId, executionId);
      } catch (err: unknown) {
        if (apiErrorStatus(err) === 409) return null;
        throw err;
      }
    },
  });

  const dismiss = useMutation({
    ...shared,
    mutationFn: ({ automationId, executionId }: { automationId: string; executionId: string }) =>
      automationApi.dismissRun(automationId, executionId),
  });

  // DALTON-HOOK(company-automations): every company and personal write shares this pending flag.
  const busy = useIsMutating({ mutationKey: WRITE_KEY }) > 0;
  return { create, update, updateScript, remove, restore, rollback, pause, resume, trigger, skip, dismiss, busy };
}

export type AutomationMutations = ReturnType<typeof useAutomationMutations>;
