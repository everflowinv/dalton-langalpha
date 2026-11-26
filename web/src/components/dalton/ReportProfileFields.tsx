import React, { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { ReportProfileConfig } from '@/types/automation';

interface ReportProfileFieldsProps {
  value: ReportProfileConfig | null | undefined;
  onChange: (next: ReportProfileConfig | null) => void;
}

function parseTargetLines(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function formatTargetLines(targets: string[] | null | undefined): string {
  return (targets ?? []).join(', ');
}

export default function ReportProfileFields({ value, onChange }: ReportProfileFieldsProps) {
  const { t } = useTranslation();
  const uid = useId();
  const [expanded, setExpanded] = useState(!!value);

  const active = expanded || !!value;

  if (!active) {
    return (
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 px-2 text-xs"
        onClick={() => {
          setExpanded(true);
          onChange({
            profile: '',
            report_targets: [],
            failure_targets: [],
          });
        }}
      >
        {t('automation:reportDeliveryConfigure', '配置报告外发对象')}
      </Button>
    );
  }

  const current: ReportProfileConfig = value ?? {
    profile: '',
    report_targets: [],
    failure_targets: [],
  };

  return (
    <div className="space-y-2.5 max-w-md">
      <div className="space-y-1">
        <label htmlFor={`${uid}-profile`} className="block text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {t('automation:reportDeliveryConfirmProfile', '报告类型')}
        </label>
        <Input
          id={`${uid}-profile`}
          aria-label={t('automation:reportDeliveryConfirmProfile', '报告类型')}
          value={current.profile}
          onChange={(e) => onChange({ ...current, profile: e.target.value })}
          placeholder="daily-filing-alert"
        />
      </div>

      <div className="space-y-1">
        <label htmlFor={`${uid}-report`} className="block text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {t('automation:reportDeliveryConfirmReportTargets', '报告卡片目标')}
        </label>
        <Input
          id={`${uid}-report`}
          aria-label={t('automation:reportDeliveryConfirmReportTargets', '报告卡片目标')}
          value={formatTargetLines(current.report_targets)}
          onChange={(e) => onChange({ ...current, report_targets: parseTargetLines(e.target.value) })}
          placeholder="feishu:chat:oc_... / feishu:user:ou_..."
        />
      </div>

      <div className="space-y-1">
        <label htmlFor={`${uid}-text`} className="block text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {t('automation:reportDeliveryConfirmTextTargets', '纯文本目标')}
        </label>
        <Input
          id={`${uid}-text`}
          aria-label={t('automation:reportDeliveryConfirmTextTargets', '纯文本目标')}
          value={formatTargetLines(current.text_targets)}
          onChange={(e) => {
            const parsed = parseTargetLines(e.target.value);
            onChange({
              ...current,
              text_targets: e.target.value.trim() ? parsed : undefined,
            });
          }}
          placeholder="feishu:chat:oc_... / discord:channel:..."
        />
      </div>

      <div className="space-y-1">
        <label htmlFor={`${uid}-failure`} className="block text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {t('automation:reportDeliveryConfirmFailureTargets', '失败告警目标')}
        </label>
        <Input
          id={`${uid}-failure`}
          aria-label={t('automation:reportDeliveryConfirmFailureTargets', '失败告警目标')}
          value={formatTargetLines(current.failure_targets)}
          onChange={(e) => onChange({ ...current, failure_targets: parseTargetLines(e.target.value) })}
          placeholder="discord:user:..."
        />
      </div>

      <div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => {
            setExpanded(false);
            onChange(null);
          }}
        >
          {t('automation:reportDeliveryClear', '清除报告外发配置')}
        </Button>
      </div>
    </div>
  );
}
