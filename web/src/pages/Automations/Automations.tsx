import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { SwapCell } from '@/components/ui/crossfade';
import { apiErrorStatus } from '@/api/errors';
import { useAuth } from '@/contexts/AuthContext';
import { useHomeTimezone } from '@/hooks/useHomeTimezone';
import { homeWorkspaceQuery } from '@/hooks/useHomeWorkspace';
import { ListError, ListSkeleton } from '@/components/mcp/McpPrimitives';
import { useScrollMemory } from '@/lib/scrollMemory';
import ConfirmDialog from '@/pages/Dashboard/components/ConfirmDialog';
import type { Automation, AutomationUpdatePayload, CompanyScriptUpdatePayload } from '@/types/automation';
import AutomationsHeader, { type AutomationsView } from './components/AutomationsHeader';
import AutomationInlineForm, { type FormSubmission } from './components/AutomationInlineForm';
import ConfirmDeleteDialog from './components/ConfirmDeleteDialog';
import FeedView, { FeedSkeleton } from './components/FeedView';
import { useLastShape } from '@/lib/lastShape';
import ManageView, { type FormHost, type ManageSelection } from './components/ManageView';
import { MANAGE_PAGE_SIZE, type ScopePage } from './components/ManageScopes';
import type { ScriptSchedulePatch } from './components/ScriptScheduleForm';
import Starters from './components/Starters';
import { useAutomations } from './hooks/useAutomations';
import { useAutomationMutations } from './hooks/useAutomationMutations';
import { useAutomationRead, useAutomationReadList } from './hooks/useAutomationReads';
import { useOrderedGroups } from './hooks/useOrderedGroups';
import { recentRunsQuery } from './hooks/useRecentRuns';
import { useWatchedReadings } from './hooks/useWatchedReadings';
import { mutationErrorMessage } from './utils/errors';
import { automationToFormState } from './utils/form';
import {
  asAutomations,
  configVersionOf,
  isCompanyTask,
  isForbiddenError,
  isScriptTask,
  type ManageTask,
} from './utils/taskKind';
import { type TemplateId, applyTemplate } from './utils/templates';
import './Automations.css';

const VIEW_STORAGE_KEY = 'automations:view';

function readStoredView(): AutomationsView | null {
  try {
    const v = localStorage.getItem(VIEW_STORAGE_KEY);
    return v === 'feed' || v === 'manage' ? v : null;
  } catch {
    return null;
  }
}

function storeView(view: AutomationsView): void {
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, view);
  } catch {
    // Private mode or blocked storage: the view just isn't remembered.
  }
}

type FormMode = { kind: 'create'; template: TemplateId; nonce: number } | { kind: 'edit'; automationId: string };

type ScopeName = 'company' | 'personal' | 'research_tracking';

type ConfigConflict = { id: string; message: string; current: number | null };

function companyAgentPatch(
  payload: AutomationUpdatePayload,
  version: number,
): AutomationUpdatePayload & { expected_config_version: number } {
  const next: AutomationUpdatePayload & { expected_config_version: number } = {
    ...payload,
    expected_config_version: version,
  };
  delete next.delivery_config;
  return next;
}

function ConfigConflictNotice({
  message,
  version,
  schedule,
}: {
  message: string;
  version: number | null;
  schedule?: { cron: string; timezone: string };
}) {
  const { t } = useTranslation();
  return (
    <div className="automations-quiet-note" role="status">
      <p>{message ? t('automation.configUpdatedDetail', { detail: message }) : t('automation.configUpdated')}</p>
      {version != null && <p>{t('automation.currentVersion', { version })}</p>}
      {schedule && <p>{t('automation.serverSchedule', { cron: schedule.cron, zone: schedule.timezone })}</p>}
    </div>
  );
}

/** The feed's scroller, which comes back where it was left. Its own
 *  component so the memory attaches to the element that actually scrolls;
 *  it mounts with the feed's skeleton and restores once the runs fill it. */
function RememberedScroll({ memoryKey, children }: { memoryKey: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useScrollMemory(ref, memoryKey);
  return (
    <div ref={ref} className="automations-scroll">
      {children}
    </div>
  );
}

/**
 * /automations: the same set of automations read two ways. The feed is what
 * they found, newest first, beside what is coming and what needs a hand; the
 * manage view is the list itself with one automation open beside it.
 *
 * The view, the open automation and the run its report shows live in the URL
 * (`?view=`, `?id=`, `?run=`), so a link to one automation, or to one of its
 * runs, is a link somebody can send, and the dashboard's `?id=` deep link
 * lands on it. Without a `view` the last one used wins, which is a
 * per-browser convenience and so lives in local storage.
 */
export default function Automations() {
  const { t } = useTranslation();
  const { userId } = useAuth();
  const { automations, loading, loaded, error } = useAutomations();
  const lastCount = useLastShape('automations.count', loaded ? automations.length : null);
  const {
    create: { mutateAsync: createAutomation },
    update: { mutateAsync: updateAutomation },
    updateScript: { mutateAsync: updateScript },
    remove: { mutateAsync: removeAutomation },
    busy,
  } = useAutomationMutations();
  const homeZone = useHomeTimezone();
  const queryClient = useQueryClient();
  const { data: homeRow, isPending: homePending } = useQuery(homeWorkspaceQuery(queryClient));
  const readings = useWatchedReadings(automations);
  const [searchParams, setSearchParams] = useSearchParams();

  const [storedView, setStoredView] = useState(readStoredView);
  const selectedId = searchParams.get('id');
  const runId = searchParams.get('run');
  const listLoading = loading && (lastCount !== 0 || !!selectedId || !!runId);
  const urlView = searchParams.get('view');
  const view: AutomationsView =
    urlView === 'feed' || urlView === 'manage' ? urlView : selectedId || runId ? 'manage' : storedView ?? 'feed';
  const { isPending: runsPending } = useInfiniteQuery({
    ...recentRunsQuery(),
    enabled: view === 'feed',
    notifyOnChangeProps: ['isPending'],
  });

  const [form, setForm] = useState<FormMode | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Automation | null>(null);
  const [draftDirty, setDraftDirty] = useState(false);
  const [pendingLeave, setPendingLeave] = useState<(() => void) | null>(null);
  const [offsets, setOffsets] = useState<Record<ScopeName, number>>({ company: 0, personal: 0, research_tracking: 0 });
  const [standInId, setStandInId] = useState<string | null>(null);
  const [scopeUser, setScopeUser] = useState(userId);
  const [editVersion, setEditVersion] = useState<number | null>(null);
  const [conflict, setConflict] = useState<ConfigConflict | null>(null);

  if (userId !== scopeUser) {
    setScopeUser(userId);
    setOffsets({ company: 0, personal: 0, research_tracking: 0 });
    setStandInId(null);
    setEditVersion(null);
    setConflict(null);
    setForm(null);
  }

  const manageOn = view === 'manage';
  const companyQuery = useAutomationReadList({
    scope: 'company',
    limit: MANAGE_PAGE_SIZE,
    offset: offsets.company,
    enabled: manageOn,
  });
  const personalQuery = useAutomationReadList({
    scope: 'personal',
    limit: MANAGE_PAGE_SIZE,
    offset: offsets.personal,
    enabled: manageOn,
  });
  const researchQuery = useAutomationReadList({
    scope: 'research_tracking',
    limit: MANAGE_PAGE_SIZE,
    offset: offsets.research_tracking,
    enabled: manageOn,
  });

  const companyForbidden = isForbiddenError(companyQuery.error);
  const companyTasks =
    companyQuery.isSuccess && !companyForbidden && companyQuery.data ? companyQuery.data.automations : [];
  const personalTasks = personalQuery.isSuccess && personalQuery.data ? personalQuery.data.automations : [];
  const researchTasks = researchQuery.isSuccess && researchQuery.data ? researchQuery.data.automations : [];
  const listsReady =
    (companyQuery.isSuccess || companyQuery.isError) &&
    (personalQuery.isSuccess || personalQuery.isError) &&
    (researchQuery.isSuccess || researchQuery.isError);
  const personalGroups = useOrderedGroups(asAutomations(personalTasks));
  const researchGroups = useOrderedGroups(asAutomations(researchTasks));
  const pool = useMemo(
    () => [...companyTasks, ...personalTasks, ...researchTasks],
    [companyTasks, personalTasks, researchTasks],
  );
  const fallback = companyTasks[0] ?? personalGroups[0]?.items[0] ?? researchGroups[0]?.items[0];
  const showStandIn = manageOn && listsReady && !selectedId && !runId;
  const remembered = standInId ? pool.find((task) => task.automation_id === standInId) : undefined;
  const standIn = showStandIn ? remembered ?? fallback : undefined;
  const nextStandIn = standIn?.automation_id ?? null;
  if ((showStandIn || !manageOn) && nextStandIn !== standInId) setStandInId(nextStandIn);

  const runOwner =
    !selectedId && runId
      ? pool.find((task) => !isScriptTask(task) && task.last_execution?.automation_execution_id === runId) ??
        automations.find((task) => task.last_execution?.automation_execution_id === runId)
      : undefined;
  const paneId = selectedId ?? runOwner?.automation_id ?? (runId ? null : standIn?.automation_id ?? null);
  const detail = useAutomationRead(paneId, { enabled: manageOn && !!paneId });
  const openDetail = detail.isSuccess && detail.data?.automation_id === paneId ? detail.data : null;
  // The company list and this detail are separate queries. Once the list is
  // refused, the cached company detail and its draft are hidden as well.
  const detailForbidden =
    (!!paneId && isForbiddenError(detail.error)) ||
    (companyForbidden && !!openDetail && isCompanyTask(openDetail));
  const detailTask = !detailForbidden ? openDetail : null;
  const pageRow = paneId ? pool.find((task) => task.automation_id === paneId) : undefined;

  useEffect(() => {
    if (form?.kind !== 'edit') return;
    // `editVersion` is set only when this draft belongs to a company task.
    // A company-list refusal drops that draft even if the stand-in pane has
    // already moved off it. A personal or research draft leaves it null.
    const lostCompanyDraft = companyForbidden && editVersion != null;
    if (!(detailForbidden && form.automationId === paneId) && !lostCompanyDraft) return;
    setForm(null);
    setConflict(null);
    setDraftDirty(false);
    setEditVersion(null);
  }, [detailForbidden, companyForbidden, editVersion, form, paneId]);

  const leaveForm = useCallback(
    (proceed: () => void) => {
      if (form && draftDirty) setPendingLeave(() => proceed);
      else proceed();
    },
    [form, draftDirty],
  );

  const navigateTo = useCallback(
    (next: AutomationsView, id: string | null, run: string | null = null) => {
      const params = new URLSearchParams(searchParams);
      params.set('view', next);
      if (id) params.set('id', id);
      else params.delete('id');
      if (id && run) params.set('run', run);
      else params.delete('run');
      storeView(next);
      setStoredView(next);
      setSearchParams(params, { replace: next === view });
    },
    [searchParams, setSearchParams, view],
  );

  const openAutomation = useCallback(
    (id: string | null) =>
      leaveForm(() => {
        setForm(null);
        setConflict(null);
        navigateTo('manage', id);
      }),
    [leaveForm, navigateTo],
  );

  const openRun = useCallback(
    (id: string, run: string | null) =>
      leaveForm(() => {
        setForm(null);
        setConflict(null);
        navigateTo('manage', id, run);
      }),
    [leaveForm, navigateTo],
  );

  const changeView = useCallback(
    (next: AutomationsView) =>
      leaveForm(() => {
        setForm(null);
        setConflict(null);
        navigateTo(next, next === 'manage' ? selectedId : null);
      }),
    [leaveForm, navigateTo, selectedId],
  );

  const startCreate = useCallback(
    (template: TemplateId) =>
      leaveForm(() => {
        setConflict(null);
        setForm({ kind: 'create', template, nonce: Date.now() });
        if (view !== 'manage') navigateTo('manage', selectedId);
      }),
    [leaveForm, navigateTo, selectedId, view],
  );

  const writeVersion = detailTask && isCompanyTask(detailTask) ? configVersionOf(detailTask) : null;
  const taskById = useCallback(
    (id: string): ManageTask | null => {
      if (detailTask?.automation_id === id) return detailTask;
      return pool.find((task) => task.automation_id === id) ?? null;
    },
    [detailTask, pool],
  );

  const versionFor = useCallback(
    (id: string): number | null => {
      if (conflict?.id === id) return conflict.current;
      return editVersion;
    },
    [conflict, editVersion],
  );

  const rememberConflict = useCallback(
    async (id: string, err: unknown) => {
      const message = mutationErrorMessage(err, '');
      setConflict({ id, message, current: null });
      const read = await detail.refetch();
      setConflict((current) =>
        current && current.id === id ? { ...current, current: configVersionOf(read.data) } : current,
      );
    },
    [detail],
  );

  const startEdit = useCallback(
    (task: ManageTask) => {
      if (isCompanyTask(task) && (detailTask?.automation_id !== task.automation_id || writeVersion == null)) return;
      setEditVersion(isCompanyTask(task) ? writeVersion : null);
      setConflict(null);
      setForm({ kind: 'edit', automationId: task.automation_id });
    },
    [detailTask, writeVersion],
  );

  const handleSubmit = useCallback(
    async (submission: FormSubmission) => {
      if (submission.kind === 'edit' && form?.kind === 'edit') {
        const id = form.automationId;
        const task = taskById(id);
        const company = !!task && isCompanyTask(task);
        let data: AutomationUpdatePayload & { expected_config_version?: number } = submission.payload;
        if (company) {
          const version = versionFor(id);
          if (version == null) return;
          data = companyAgentPatch(submission.payload, version);
        }
        try {
          await updateAutomation({ id, data });
          setForm(null);
          setConflict(null);
        } catch (err) {
          if (apiErrorStatus(err) === 409) await rememberConflict(id, err);
          throw err;
        }
        return;
      }
      if (submission.kind === 'create') {
        const created = await createAutomation(submission.payload);
        setForm(null);
        if (created?.automation_id) navigateTo('manage', created.automation_id);
      }
    },
    [form, taskById, versionFor, updateAutomation, rememberConflict, createAutomation, navigateTo],
  );

  const submitScript = useCallback(
    async (id: string, patch: ScriptSchedulePatch) => {
      const version = versionFor(id);
      if (version == null) return;
      const data: CompanyScriptUpdatePayload = { expected_config_version: version };
      if (patch.cron_expression) data.cron_expression = patch.cron_expression;
      if (patch.timezone) data.timezone = patch.timezone;
      if (!data.cron_expression && !data.timezone) return;
      try {
        await updateScript({ id, data });
        setForm(null);
        setConflict(null);
      } catch (err) {
        if (apiErrorStatus(err) === 409) await rememberConflict(id, err);
        throw err;
      }
    },
    [versionFor, updateScript, rememberConflict],
  );

  const handleConfirmDelete = useCallback(async () => {
    if (!deleteTarget) return;
    try {
      await removeAutomation(deleteTarget.automation_id);
      if (deleteTarget.automation_id === selectedId) navigateTo('manage', null);
      setDeleteTarget(null);
    } catch {
      // Reported by the mutation hook.
    }
  }, [deleteTarget, removeAutomation, navigateTo, selectedId]);

  const editing = form?.kind === 'edit' ? taskById(form.automationId) : null;
  const held = conflict && editing && conflict.id === editing.automation_id ? conflict : null;
  const formBlocked = !!editing && detailForbidden && editing.automation_id === paneId;

  const formHost: FormHost | null = useMemo(() => {
    if (!form || formBlocked) return null;
    if (form.kind === 'edit') {
      if (!editing) return null;
      if (isScriptTask(editing)) {
        return {
          kind: 'script',
          key: `script:${editing.automation_id}`,
          props: {
            cronExpression: editing.cron_expression,
            timezone: editing.timezone,
            loading: busy,
            submitDisabled: !!held && held.current == null,
            submitLabel: held ? t('automation.retryWithCurrent') : undefined,
            notice: held ? (
              <ConfigConflictNotice
                message={held.message}
                version={held.current}
                schedule={{ cron: editing.cron_expression, timezone: editing.timezone }}
              />
            ) : undefined,
            onSubmit: (patch) => submitScript(editing.automation_id, patch),
            onCancel: () => {
              setForm(null);
              setConflict(null);
            },
            onDirtyChange: setDraftDirty,
          },
        };
      }
      if (homePending) return null;
      return {
        kind: 'agent',
        key: `edit:${editing.automation_id}`,
        props: {
          onSubmit: handleSubmit,
          onCancel: () => {
            setForm(null);
            setConflict(null);
          },
          onDirtyChange: setDraftDirty,
          loading: busy,
          submitDisabled: !!held && held.current == null,
          submitLabel: held ? t('automation.retryWithCurrent') : undefined,
          omitDelivery: isCompanyTask(editing),
          notice: held ? <ConfigConflictNotice message={held.message} version={held.current} /> : undefined,
          initialValues: automationToFormState(editing, homeZone, homeRow?.workspace_id),
          original: editing,
        },
      };
    }
    return {
      kind: 'agent',
      key: `create:${form.template}:${form.nonce}`,
      props: {
        onSubmit: handleSubmit,
        onCancel: () => setForm(null),
        onDirtyChange: setDraftDirty,
        loading: busy,
        initialValues: applyTemplate(form.template, t, homeZone),
        original: null,
      },
    };
  }, [form, formBlocked, editing, held, busy, submitScript, handleSubmit, homePending, homeZone, homeRow, t]);

  const selection = useMemo((): ManageSelection => {
    const linked = !!(selectedId || runId);
    if (paneId && detailForbidden) return { kind: 'forbidden' };
    const task = detailTask ?? (!detailForbidden ? pageRow : undefined);
    if (task) return linked ? { kind: 'chosen', task, runId } : { kind: 'first', task };
    if (paneId && (detail.isLoading || detail.isFetching || !listsReady)) return { kind: 'loading' };
    if (selectedId || runId) return { kind: 'missing', runId };
    if (!listsReady) return { kind: 'loading' };
    return { kind: 'none' };
  }, [paneId, detailForbidden, detailTask, pageRow, detail.isLoading, detail.isFetching, listsReady, selectedId, runId]);

  const scopePage = (
    scope: ScopeName,
    titleKey: string,
    tasks: ScopePage['tasks'],
    total: number,
    query: { isLoading: boolean; isFetching: boolean; isSuccess: boolean; isError: boolean },
    forbidden: boolean,
  ): ScopePage => ({
    scope,
    titleKey,
    tasks: forbidden ? [] : tasks,
    total: forbidden ? 0 : total,
    offset: offsets[scope],
    isLoading: !forbidden && (query.isLoading || (query.isFetching && !query.isSuccess)),
    isForbidden: forbidden,
    isError: query.isError,
    onOffset: (offset) => setOffsets((current) => ({ ...current, [scope]: offset })),
  });

  const pages: ScopePage[] = [
    scopePage('company', 'automation.groupCompany', companyTasks, companyQuery.data?.total ?? 0, companyQuery, companyForbidden),
    scopePage('personal', 'automation.groupMine', personalTasks, personalQuery.data?.total ?? 0, personalQuery, isForbiddenError(personalQuery.error)),
    scopePage(
      'research_tracking',
      'automation.groupResearch',
      researchTasks,
      researchQuery.data?.total ?? 0,
      researchQuery,
      isForbiddenError(researchQuery.error),
    ),
  ];

  const visibleCount = companyTasks.length + personalTasks.length + researchTasks.length;
  // Starters only when every group loaded and is empty. A 403 or a failed
  // read keeps the three groups, each with its own refusal or error.
  const successfulEmpty =
    companyQuery.isSuccess && personalQuery.isSuccess && researchQuery.isSuccess && visibleCount === 0;
  let body: React.ReactNode;
  if (view === 'feed') {
    if (error && automations.length === 0) {
      body = (
        <div className="automations-scroll">
          <div className="automations-frame">
            <ListError>{t('automation.loadFailed')}</ListError>
          </div>
        </div>
      );
    } else {
      const feedState = listLoading || (runsPending && automations.length > 0) ? 'loading' : automations.length === 0 ? 'empty' : 'feed';
      body = (
        <RememberedScroll memoryKey="page:automations:feed">
          <div className="automations-frame">
            <SwapCell as="div" swapKey={feedState}>
              {feedState === 'loading' ? (
                <FeedSkeleton />
              ) : feedState === 'empty' ? (
                <Starters onPick={startCreate} />
              ) : (
                <FeedView
                  automations={automations}
                  readings={readings}
                  onOpenAutomation={openAutomation}
                  onOpenRun={openRun}
                  onManage={() => changeView('manage')}
                  onNew={startCreate}
                />
              )}
            </SwapCell>
          </div>
        </RememberedScroll>
      );
    }
  } else if (!selectedId && !runId && !listsReady) {
    body = (
      <div className="automations-scroll">
        <div className="automations-frame">
          <ListSkeleton rows={5} />
        </div>
      </div>
    );
  } else if (!selectedId && !runId && successfulEmpty) {
    body = (
      <div className="automations-scroll">
        <div className="automations-frame">
          {formHost?.kind === 'agent' ? (
            <div className="automation-form-pane automations-zero-form">
              <h2 className="title-font automation-form-title">{t('automation.newAutomation')}</h2>
              <AutomationInlineForm key={formHost.key} {...formHost.props} />
            </div>
          ) : (
            <Starters onPick={startCreate} />
          )}
        </div>
      </div>
    );
  } else {
    body = (
      <div className="automations-frame automations-frame-fill">
        <ManageView
          pages={pages}
          readings={readings}
          selection={selection}
          writeVersion={writeVersion}
          onSelect={openAutomation}
          onOpenRun={openRun}
          form={formHost}
          onEdit={startEdit}
          onDelete={setDeleteTarget}
        />
      </div>
    );
  }

  return (
    <div className="automations-page">
      <div className="chrome-drag-strip" aria-hidden="true" />
      <div className="automations-frame">
        <AutomationsHeader automations={automations} loading={listLoading} view={view} onViewChange={changeView} onNew={startCreate} />
      </div>
      <div className="automations-body">{body}</div>

      <ConfirmDeleteDialog
        open={!!deleteTarget}
        onOpenChange={(open: boolean) => !open && setDeleteTarget(null)}
        onConfirm={handleConfirmDelete}
        automationName={deleteTarget?.name}
        loading={busy}
      />

      <ConfirmDialog
        open={!!pendingLeave}
        title={t('automation.discardDraftTitle')}
        message={t('automation.discardDraftMessage')}
        confirmLabel={t('automation.discardDraft')}
        onConfirm={() => {
          const proceed = pendingLeave;
          setPendingLeave(null);
          setDraftDirty(false);
          proceed?.();
        }}
        onOpenChange={(open) => {
          if (!open) setPendingLeave(null);
        }}
      />
    </div>
  );
}
