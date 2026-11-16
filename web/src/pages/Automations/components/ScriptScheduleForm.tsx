// DALTON-HOOK(company-automations): company script edits cron and timezone only.
// DALTON-HOOK(ui-unify): reuse visual CronScheduleBuilder and TimezonePicker instead of raw text inputs.
import React, { useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import TimezonePicker from '@/components/TimezonePicker';
import { Button } from '@/components/ui/button';
import { useHomeTimezone } from '@/hooks/useHomeTimezone';
import CronScheduleBuilder from './CronScheduleBuilder';
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
  const homeZone = useHomeTimezone();
  const [opened] = useState({ cron: cronExpression, timezone });
  const [cron, setCron] = useState(cronExpression);
  const [zone, setZone] = useState(timezone);
  const labelId = useId();
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

  const zoneSelect = (
    <TimezonePicker
      value={zone}
      onChange={setZone}
      home={{ zone: homeZone, label: t('timezone.yours') }}
      className="min-w-40 max-w-64"
    />
  );

  return (
    <form onSubmit={handleSubmit} className="automation-form">
      {notice}
      <div className="automation-form-rows">
        <FormRow label={t('automation.scriptCron')} labelId={labelId}>
          <div className="automation-form-trigger">
            <CronScheduleBuilder
              value={cron}
              onChange={setCron}
              zone={zoneSelect}
              labelledBy={labelId}
            />
          </div>
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
