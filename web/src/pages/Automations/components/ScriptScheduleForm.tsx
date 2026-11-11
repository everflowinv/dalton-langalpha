// DALTON-HOOK(company-automations): company script edits cron and timezone only.
import React, { useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import FormRow from './FormRow';

export interface ScriptSchedulePatch {
  cron_expression?: string;
  timezone?: string;
}

export interface ScriptScheduleFormProps {
  /** Values the form opened with. A later read must not replace them. */
  cronExpression: string;
  timezone: string;
  notice?: React.ReactNode;
  submitLabel?: string;
  submitDisabled?: boolean;
  loading: boolean;
  onSubmit: (patch: ScriptSchedulePatch) => Promise<void> | void;
  onCancel: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}

export default function ScriptScheduleForm({
  cronExpression,
  timezone,
  notice,
  submitLabel,
  submitDisabled = false,
  loading,
  onSubmit,
  onCancel,
  onDirtyChange,
}: ScriptScheduleFormProps) {
  const { t } = useTranslation();
  const [opened] = useState({ cron: cronExpression, timezone });
  const [cron, setCron] = useState(cronExpression);
  const [zone, setZone] = useState(timezone);
  const uid = useId();
  const dirty = cron !== opened.cron || zone !== opened.timezone;

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (submitDisabled || loading) return;
    const patch: ScriptSchedulePatch = {};
    const nextCron = cron.trim();
    const nextZone = zone.trim();
    if (nextCron !== opened.cron && nextCron) patch.cron_expression = nextCron;
    if (nextZone !== opened.timezone && nextZone) patch.timezone = nextZone;
    if (!patch.cron_expression && !patch.timezone) return;
    void Promise.resolve(onSubmit(patch)).catch(() => {
      // A refused save leaves these fields as they were typed.
    });
  };

  return (
    <form onSubmit={handleSubmit} className="automation-form">
      {notice}
      <div className="automation-form-rows">
        <FormRow label={t('automation.scriptCron')} htmlFor={`${uid}-cron`}>
          <Input id={`${uid}-cron`} value={cron} onChange={(event) => setCron(event.target.value)} autoComplete="off" />
        </FormRow>
        <FormRow label={t('automation.scriptTimezone')} htmlFor={`${uid}-zone`}>
          <Input id={`${uid}-zone`} value={zone} onChange={(event) => setZone(event.target.value)} autoComplete="off" />
        </FormRow>
      </div>
      <div className="automation-form-actions">
        <Button
          type="submit"
          disabled={loading || submitDisabled || !dirty}
          style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
        >
          {loading ? t('common.saving') : submitLabel ?? t('automation.saveChanges')}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          {t('common.cancel')}
        </Button>
      </div>
    </form>
  );
}
