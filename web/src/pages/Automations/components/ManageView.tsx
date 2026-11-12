// DALTON-HOOK(company-automations): manage shows three scope groups and one task, or the form that edits it.
import React, { useLayoutEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { AnimatePresence, motion } from '@/lib/framer';
import { SwapCell } from '@/components/ui/crossfade';
import { MobileBottomSheet } from '@/components/ui/mobile-bottom-sheet';
import { ListSkeleton } from '@/components/mcp/McpPrimitives';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useScrollMemory } from '@/lib/scrollMemory';
import { useScrollReveal } from '../hooks/useScrollReveal';
import type { WatchedReading } from '../hooks/useWatchedReadings';
import { PANE_CROSSFADE } from '../utils/motion';
import { configVersionOf, isCompanyTask, isDeletedTask, isScriptTask, type ManageTask } from '../utils/taskKind';
import AutomationInlineForm, { type AutomationInlineFormProps } from './AutomationInlineForm';
import AutomationInspector from './AutomationInspector';
import { ManageScopes, type ScopePage } from './ManageScopes';
import ScriptInspector from './ScriptInspector';
import ScriptScheduleForm, { type ScriptScheduleFormProps } from './ScriptScheduleForm';
import './ManageView.css';

// DALTON-HOOK(company-automations): the form key is the task id, so a version refresh does not reset the draft.
/** The open form: the props it mounts with, and the key that remounts it
 *  for another task or a fresh start. The key is the task id, not its version. */
export type FormHost =
  | { kind: 'agent'; key: string; props: AutomationInlineFormProps }
  | { kind: 'script'; key: string; props: ScriptScheduleFormProps };

/**
 * What the list marks and the pane shows. A choice the lists do not hold is
 * `missing` until its own read returns. A forbidden read shows no task and no
 * actions. With no choice a row stands in. Only a choice, `chosen` or
 * `missing` or `forbidden`, opens the phone sheet.
 */
export type ManageSelection =
  | { kind: 'chosen'; task: ManageTask; runId: string | null }
  | { kind: 'first'; task: ManageTask }
  | { kind: 'missing'; runId: string | null }
  | { kind: 'forbidden' }
  | { kind: 'loading' }
  | { kind: 'none' };

// DALTON-HOOK(company-automations): scope groups, the current detail version, and permission callbacks for the open task.
interface ManageViewProps {
  pages: ScopePage[];
  readings: Map<string, WatchedReading>;
  selection: ManageSelection;
  /** Company writes use the open detail's version, never a list row's. */
  writeVersion: number | null;
  onSelect: (id: string | null) => void;
  onOpenRun: (automationId: string, runId: string | null) => void;
  form: FormHost | null;
  onEdit: (task: ManageTask) => void;
  onDelete: (task: ManageTask) => void;
  /** A company run list returned 403. The page drops that task's cached reads. */
  onRunsForbidden: (automationId: string) => void;
  /** Closes the recycle bin of the restored task's group. */
  onRestored: (automationId: string) => void;
  /** The group named by `?scope=`, after an unauthorized company link falls back. */
  activeScope?: ScopePage['scope'] | null;
}

/**
 * The list and the thing it lists, side by side: three scope groups on the
 * left, one task (or the form that makes or changes one) on the right. On a
 * phone the list is the page and the task opens as a sheet over it.
 */
export default function ManageView({
  pages,
  readings,
  selection,
  writeVersion,
  onSelect,
  onOpenRun,
  form,
  onEdit,
  onDelete,
  onRunsForbidden,
  onRestored,
  activeScope = null,
}: ManageViewProps) {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const revealListScroll = useScrollReveal();
  const revealPaneScroll = useScrollReveal();
  const shown = selection.kind === 'chosen' || selection.kind === 'first' ? selection.task : null;
  const company = shown ? isCompanyTask(shown) : false;
  const deleted = shown ? isDeletedTask(shown) : false;
  const shownVersion = configVersionOf(shown);
  const runId = selection.kind === 'chosen' ? selection.runId : null;

  // DALTON-HOOK(company-automations): the open form stays mounted while another group's page is loading.
  const formPane = form && (
    <div className="automation-form-pane">
      <h2 className="title-font automation-form-title">
        {t(
          form.kind === 'script'
            ? 'automation.editAutomation'
            : form.props.original
              ? 'automation.editAutomation'
              : 'automation.newAutomation',
        )}
      </h2>
      <SwapCell as="div" className="items-start" swapKey={String(form.key)}>
        {form.kind === 'script' ? <ScriptScheduleForm {...form.props} /> : <AutomationInlineForm {...form.props} />}
      </SwapCell>
    </div>
  );

  const inspector = shown ? (
    isScriptTask(shown) ? (
      <ScriptInspector
        key={shown.automation_id}
        script={shown}
        runId={runId}
        configVersion={writeVersion}
        onEdit={onEdit}
        onDelete={onDelete}
        onRunsForbidden={onRunsForbidden}
        onRestored={() => onRestored(shown.automation_id)}
      />
    ) : (
      <AutomationInspector
        key={shown.automation_id}
        automation={shown}
        reading={readings.get(shown.automation_id)}
        runId={runId}
        onOpenRun={(run) => onOpenRun(shown.automation_id, run)}
        onEdit={onEdit}
        onDelete={onDelete}
        // DALTON-HOOK(company-automations): company agents can edit, pause, and move to the recycle bin. Run, dismiss, and skip stay off.
        allowRun={!company && !deleted}
        allowDelete={!deleted}
        allowDismiss={!company && !deleted}
        allowSkip={!company && !deleted}
        requireVersion={company}
        expectedConfigVersion={deleted ? shownVersion : writeVersion}
        onRunsForbidden={onRunsForbidden}
        onRestored={() => onRestored(shown.automation_id)}
      />
    )
  ) : selection.kind === 'forbidden' ? (
    <p className="automations-quiet-note">{t('automation.detailForbidden')}</p>
  ) : selection.kind === 'missing' ? (
    <p className="automations-quiet-note">{t(selection.runId ? 'automation.runLinkNotFound' : 'automation.notFound')}</p>
  ) : selection.kind === 'loading' ? (
    <ListSkeleton rows={4} />
  ) : null;

  const paneKey = form
    ? `form:${form.key}`
    : isMobile
      ? undefined
      : shown?.automation_id ?? (selection.kind === 'missing' || selection.kind === 'forbidden' ? selection.kind : undefined);

  const listRef = useRef<HTMLDivElement>(null);
  const paneRef = useRef<HTMLDivElement>(null);
  const layout = isMobile ? 'mobile' : 'desktop';
  useScrollMemory(listRef, `page:automations:manage:${layout}:list`);
  useScrollMemory(paneRef, `page:automations:manage:${layout}:pane:${paneKey ?? 'list'}`);

  const placedKey = useRef(paneKey);
  useLayoutEffect(() => {
    if (placedKey.current === paneKey) return;
    placedKey.current = paneKey;
    if (paneKey !== undefined && paneRef.current) paneRef.current.scrollTop = 0;
  }, [paneKey]);

  const list = (
    <ManageScopes
      pages={pages}
      readings={readings}
      selectedId={form ? null : selection.kind === 'chosen' || (!isMobile && selection.kind === 'first') ? selection.task.automation_id : null}
      onSelect={(id) => onSelect(id)}
      keyboardNav={!isMobile && !form}
      activeScope={activeScope}
    />
  );

  // DALTON-HOOK(company-automations): a phone opens the chosen task in a sheet. The stand-in stays on the list.
  if (isMobile) {
    const sheetOpen = !form && (selection.kind === 'chosen' || selection.kind === 'missing' || selection.kind === 'forbidden');
    return (
      <div ref={paneRef} className="automations-manage-mobile">
        {formPane}
        {list}
        <MobileBottomSheet open={sheetOpen} onClose={() => onSelect(null)} height="88vh">
          <div className="px-1 pb-6">{inspector}</div>
        </MobileBottomSheet>
      </div>
    );
  }

  return (
    <div className="automations-manage">
      <motion.div
        ref={listRef}
        layoutScroll
        className="automations-manage-list automations-scroller"
        onScroll={revealListScroll}
      >
        {list}
      </motion.div>
      <div ref={paneRef} className="automations-manage-pane automations-scroller" onScroll={revealPaneScroll}>
        {/* DALTON-HOOK(company-automations): the form and the inspector crossfade, so the previous heading stays until its exit ends. */}
        <AnimatePresence initial={false} mode="popLayout">
          {formPane ? (
            <motion.div key="form" {...PANE_CROSSFADE}>
              {formPane}
            </motion.div>
          ) : (
            inspector && (
              <motion.div key={shown?.automation_id ?? selection.kind} {...PANE_CROSSFADE}>
                {inspector}
              </motion.div>
            )
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
