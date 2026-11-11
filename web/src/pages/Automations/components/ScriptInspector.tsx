// DALTON-HOOK(company-automations): a company script has schedule controls and no agent actions.
import React from 'react';
import { useTranslation } from 'react-i18next';
import { Pause, Pencil, Play } from 'lucide-react';
import { HeaderButton, ListSkeleton } from '@/components/mcp/McpPrimitives';
import { useLocale } from '@/hooks/useLocale';
import type { ScriptAutomationRead } from '@/types/automation';
import { useAutomationExecutionReads } from '../hooks/useAutomationReads';
import { useAutomationMutations } from '../hooks/useAutomationMutations';
import { formatDateTimeShort } from '../utils/time';
import { runStatusKey, scriptStatusLabelKey } from '../utils/taskKind';
import './AutomationInspector.css';

interface ScriptInspectorProps {
  script: ScriptAutomationRead;
  runId: string | null;
  /** Detail `config_version`. Pause, resume, and save stay off until it is known. */
  configVersion: number | null;
  onEdit: (script: ScriptAutomationRead) => void;
}

export default function ScriptInspector({ script, runId, configVersion, onEdit }: ScriptInspectorProps) {
  const { t } = useTranslation();
  const locale = useLocale();
  const { pause, resume, busy } = useAutomationMutations();
  const runs = useAutomationExecutionReads(script.automation_id, { limit: 20, offset: 0 });
  const statusKey = scriptStatusLabelKey(script);
  const versionReady = configVersion != null;
  const canPause = script.enabled && !script.paused_by_user;
  const canResume = !script.enabled || script.paused_by_user;
  const target = () => (configVersion == null ? null : { id: script.automation_id, expectedConfigVersion: configVersion });
  const executions = runs.data?.executions ?? [];
  const opened = runId ? executions.find((run) => run.automation_execution_id === runId) ?? null : null;

  return (
    <article className="automation-inspector">
      <div className="automations-kicker">
        {t('automation.taskScript')}
        {statusKey ? ` · ${t(statusKey)}` : script.status ? ` · ${script.status}` : ''}
      </div>
      <h2 className="title-font automation-inspector-title">{script.name}</h2>
      <dl className="automation-details">
        <dt>{t('automation.scriptStatus')}</dt>
        <dd>{statusKey ? t(statusKey) : script.status}</dd>
        <dt>{t('automation.scriptCron')}</dt>
        <dd className="automation-mono">{script.cron_expression}</dd>
        <dt>{t('automation.scriptTimezone')}</dt>
        <dd>{script.timezone}</dd>
        {script.next_run_at && (
          <>
            <dt>{t('automation.scriptNextRun')}</dt>
            <dd>{formatDateTimeShort(script.next_run_at, locale)}</dd>
          </>
        )}
        {script.last_run_at && (
          <>
            <dt>{t('automation.scriptLastRun')}</dt>
            <dd>{formatDateTimeShort(script.last_run_at, locale)}</dd>
          </>
        )}
        {configVersion != null && (
          <>
            <dt>{t('automation.versionLabel')}</dt>
            <dd className="automation-mono">{configVersion}</dd>
          </>
        )}
      </dl>

      <div className="automation-inspector-actions">
        {canPause && (
          <HeaderButton
            variant="secondary"
            icon={Pause}
            disabled={busy || !versionReady}
            onClick={() => {
              const id = target();
              if (id) pause.mutate(id);
            }}
          >
            {t('automation.pause')}
          </HeaderButton>
        )}
        {canResume && (
          <HeaderButton
            variant="secondary"
            icon={Play}
            disabled={busy || !versionReady}
            onClick={() => {
              const id = target();
              if (id) resume.mutate(id);
            }}
          >
            {t('automation.resume')}
          </HeaderButton>
        )}
        <HeaderButton variant="secondary" icon={Pencil} disabled={!versionReady} onClick={() => onEdit(script)}>
          {t('automation.edit')}
        </HeaderButton>
      </div>

      <section className="automation-inspector-section">
        <h3 className="automations-kicker">{t('automation.runHistory')}</h3>
        {runId && !opened && !runs.isLoading && <p className="automations-quiet-note">{t('automation.runNotFound')}</p>}
        {runs.isLoading ? (
          <ListSkeleton rows={2} />
        ) : executions.length === 0 && !script.last_execution ? (
          <p className="automations-quiet-note">{t('automation.noRunsForThis')}</p>
        ) : (
          <ul className="automation-details">
            {(executions.length > 0 ? executions : script.last_execution ? [script.last_execution] : []).map((run) => (
              <li key={run.automation_execution_id} className="automation-mono">
                {runStatusKey(run.status) ? t(runStatusKey(run.status)!) : run.status}
                {run.started_at ? ` · ${formatDateTimeShort(run.started_at, locale)}` : ''}
                {run.completed_at ? ` · ${formatDateTimeShort(run.completed_at, locale)}` : ''}
              </li>
            ))}
          </ul>
        )}
      </section>
    </article>
  );
}
