// DALTON-HOOK(company-automations): three scope lists. Grouping follows the request scope.
import React, { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { ListSkeleton } from '@/components/mcp/McpPrimitives';
import { useHomeTimezone } from '@/hooks/useHomeTimezone';
import { useLocale } from '@/hooks/useLocale';
import type { AutomationRead } from '@/types/automation';
import type { WatchedReading } from '../hooks/useWatchedReadings';
import { useOrderedGroups } from '../hooks/useOrderedGroups';
import { scheduleLine } from '../utils/schedule';
import { asAutomations, isScriptTask, scriptStatusLabelKey } from '../utils/taskKind';
import { AutomationList } from './AutomationList';

export const MANAGE_PAGE_SIZE = 50;

export interface ScopePage {
  scope: 'company' | 'personal' | 'research_tracking';
  titleKey: string;
  tasks: AutomationRead[];
  total: number;
  offset: number;
  isLoading: boolean;
  isForbidden: boolean;
  isError: boolean;
  onOffset: (offset: number) => void;
}

interface ManageScopesProps {
  pages: ScopePage[];
  readings: Map<string, WatchedReading>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  keyboardNav?: boolean;
}

export function ManageScopes({ pages, readings, selectedId, onSelect, keyboardNav = false }: ManageScopesProps) {
  const { t } = useTranslation();
  const navRef = useRef<HTMLDivElement>(null);
  const personal = pages.find((page) => page.scope === 'personal');
  const research = pages.find((page) => page.scope === 'research_tracking');
  const personalGroups = useOrderedGroups(asAutomations(personal?.tasks ?? []));
  const researchGroups = useOrderedGroups(asAutomations(research?.tasks ?? []));
  const groupsFor = (scope: ScopePage['scope']) => (scope === 'personal' ? personalGroups : researchGroups);

  useEffect(() => {
    if (!keyboardNav) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.target !== document.body) return;
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      const rows = [...(navRef.current?.querySelectorAll<HTMLElement>('[data-id]') ?? [])];
      const ids = rows.map((row) => row.dataset.id).filter((id): id is string => !!id);
      if (ids.length === 0) return;
      event.preventDefault();
      const index = selectedId ? ids.indexOf(selectedId) : -1;
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      const next = index < 0 ? (delta > 0 ? 0 : ids.length - 1) : Math.min(Math.max(index + delta, 0), ids.length - 1);
      const id = ids[next];
      if (!id || id === selectedId) return;
      onSelect(id);
      rows.find((row) => row.dataset.id === id)?.focus({ preventScroll: true });
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [keyboardNav, onSelect, selectedId]);

  return (
    <div ref={navRef} className="automations-scopes" aria-label={t('automation.automations')}>
      {pages.map((page) => (
        <section key={page.scope} className="automations-scope" aria-label={t(page.titleKey)}>
          <h2 className="automations-kicker automations-list-heading">
            {t(page.titleKey)}
            {!page.isForbidden && !page.isLoading && page.total > 0 && (
              <span className="automations-list-count">{page.total}</span>
            )}
          </h2>
          {page.isForbidden ? (
            <p className="automations-quiet-note">
              {t(page.scope === 'company' ? 'automation.companyUnavailable' : 'automation.detailForbidden')}
            </p>
          ) : page.isLoading ? (
            <ListSkeleton rows={3} />
          ) : page.isError ? (
            <p className="automations-quiet-note">{t('automation.scopeLoadFailed')}</p>
          ) : page.tasks.length === 0 ? (
            <p className="automations-quiet-note">{t('automation.scopeEmpty')}</p>
          ) : page.scope === 'company' ? (
            <CompanyRows tasks={page.tasks} selectedId={selectedId} onSelect={onSelect} />
          ) : (
            <>
              <AutomationList
                groups={groupsFor(page.scope)}
                readings={readings}
                selectedId={selectedId}
                onSelect={onSelect}
              />
              <CompanyRows tasks={page.tasks.filter(isScriptTask)} selectedId={selectedId} onSelect={onSelect} />
            </>
          )}
          {!page.isForbidden && !page.isLoading && page.total > MANAGE_PAGE_SIZE && (
            <ScopePager page={page} />
          )}
        </section>
      ))}
    </div>
  );
}

function CompanyRows({
  tasks,
  selectedId,
  onSelect,
}: {
  tasks: AutomationRead[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const homeZone = useHomeTimezone();
  if (tasks.length === 0) return null;
  return (
    <>
      {tasks.map((task) => {
        const selected = task.automation_id === selectedId;
        if (isScriptTask(task)) {
          const statusKey = scriptStatusLabelKey(task);
          return (
            <button
              key={task.automation_id}
              type="button"
              className="automations-list-row"
              data-id={task.automation_id}
              aria-current={selected ? 'true' : undefined}
              onClick={() => onSelect(task.automation_id)}
            >
              <span className="min-w-0 flex-1">
                <span className="automations-list-name">{task.name}</span>
                <span className="automation-mono automations-list-sub">
                  {t('automation.taskScript')} · {task.cron_expression}
                </span>
              </span>
              <span className="automation-mono automations-list-trailing">{statusKey ? t(statusKey) : task.status}</span>
            </button>
          );
        }
        return (
          <button
            key={task.automation_id}
            type="button"
            className="automations-list-row"
            data-id={task.automation_id}
            aria-current={selected ? 'true' : undefined}
            onClick={() => onSelect(task.automation_id)}
          >
            <span className="min-w-0 flex-1">
              <span className="automations-list-name">{task.name}</span>
              <span className="automation-mono automations-list-sub">
                {t('automation.taskAgent')} · {scheduleLine(task, t, homeZone, locale)}
              </span>
            </span>
          </button>
        );
      })}
    </>
  );
}

function ScopePager({ page }: { page: ScopePage }) {
  const { t } = useTranslation();
  const pages = Math.max(1, Math.ceil(page.total / MANAGE_PAGE_SIZE));
  const current = Math.floor(page.offset / MANAGE_PAGE_SIZE) + 1;
  const group = t(page.titleKey);
  return (
    <div className="automations-scope-pager">
      <button
        type="button"
        className="automation-link"
        disabled={page.offset <= 0}
        aria-label={`${t('automation.prevPage')} ${group}`}
        onClick={() => page.onOffset(Math.max(0, page.offset - MANAGE_PAGE_SIZE))}
      >
        {t('automation.prevPage')}
      </button>
      <span className="automation-mono automations-scope-page">{t('automation.pageOf', { page: current, pages })}</span>
      <button
        type="button"
        className="automation-link"
        disabled={page.offset + MANAGE_PAGE_SIZE >= page.total}
        aria-label={`${t('automation.nextPage')} ${group}`}
        onClick={() => page.onOffset(page.offset + MANAGE_PAGE_SIZE)}
      >
        {t('automation.nextPage')}
      </button>
    </div>
  );
}
