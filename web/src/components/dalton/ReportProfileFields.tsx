import React, { useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { ReportProfileConfig } from '@/types/automation';

interface ReportProfileFieldsProps {
  value: ReportProfileConfig | null | undefined;
  onChange: (next: ReportProfileConfig | null) => void;
}

type TextTargetMode = 'inherit' | 'custom' | 'none';

function parseTargetLines(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function formatTargetLines(targets: string[] | null | undefined): string {
  return (targets ?? []).join(', ');
}

function sameTargetList(a: string[], b: string[] | null | undefined): boolean {
  const right = b ?? [];
  return a.length === right.length && a.every((item, idx) => item === right[idx]);
}

function deriveTextMode(textTargets: string[] | null | undefined): TextTargetMode {
  if (textTargets === undefined || textTargets === null) return 'inherit';
  return textTargets.length === 0 ? 'none' : 'custom';
}

export default function ReportProfileFields({ value, onChange }: ReportProfileFieldsProps) {
  const { t } = useTranslation();
  const uid = useId();
  const [expanded, setExpanded] = useState(!!value);
  const [reportDraft, setReportDraft] = useState(() => formatTargetLines(value?.report_targets));
  const [textDraft, setTextDraft] = useState(() => formatTargetLines(value?.text_targets));
  const [failureDraft, setFailureDraft] = useState(() => formatTargetLines(value?.failure_targets));
  const [textMode, setTextMode] = useState<TextTargetMode>(() => deriveTextMode(value?.text_targets));

  // Resync drafts only when the controlled value diverges from what the user's raw draft parses to
  // (e.g. switching automations or clearing), preserving trailing commas/whitespace while typing.
  useEffect(() => {
    const nextReport = value?.report_targets ?? [];
    setReportDraft((prev) => (sameTargetList(parseTargetLines(prev), nextReport) ? prev : formatTargetLines(nextReport)));
  }, [value?.report_targets]);

  useEffect(() => {
    const nextFailure = value?.failure_targets ?? [];
    setFailureDraft((prev) => (sameTargetList(parseTargetLines(prev), nextFailure) ? prev : formatTargetLines(nextFailure)));
  }, [value?.failure_targets]);

  useEffect(() => {
    const incoming = value?.text_targets;
    if (incoming === undefined || incoming === null) {
      setTextMode('inherit');
      setTextDraft((prev) => (parseTargetLines(prev).length === 0 ? prev : ''));
      return;
    }
    if (incoming.length === 0) {
      setTextMode((prev) => (prev === 'custom' ? 'custom' : 'none'));
      setTextDraft((prev) => (parseTargetLines(prev).length === 0 ? prev : ''));
      return;
    }
    setTextMode('custom');
    setTextDraft((prev) => (sameTargetList(parseTargetLines(prev), incoming) ? prev : formatTargetLines(incoming)));
  }, [value?.text_targets]);

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
          setReportDraft('');
          setTextDraft('');
          setFailureDraft('');
          setTextMode('inherit');
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

  const handleTextModeChange = (mode: TextTargetMode) => {
    setTextMode(mode);
    if (mode === 'inherit') {
      const next: ReportProfileConfig = {
        profile: current.profile,
        report_targets: current.report_targets,
        failure_targets: current.failure_targets,
      };
      onChange(next);
      return;
    }
    if (mode === 'none') {
      setTextDraft('');
      onChange({ ...current, text_targets: [] });
      return;
    }
    onChange({ ...current, text_targets: parseTargetLines(textDraft) });
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
          value={reportDraft}
          onChange={(e) => {
            const raw = e.target.value;
            setReportDraft(raw);
            onChange({ ...current, report_targets: parseTargetLines(raw) });
          }}
          placeholder="feishu:chat:oc_... / feishu:user:ou_..."
        />
      </div>

      <div className="space-y-1.5">
        <label htmlFor={`${uid}-text-mode`} className="block text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {t('automation:reportDeliveryConfirmTextTargets', '纯文本目标')}
        </label>
        <select
          id={`${uid}-text-mode`}
          aria-label={t('automation:reportDeliveryTextMode', '纯文本发送方式')}
          value={textMode}
          onChange={(e) => handleTextModeChange(e.target.value as TextTargetMode)}
          className="h-9 w-full rounded-md border px-2.5 text-xs"
          style={{
            backgroundColor: 'var(--color-bg-input, var(--color-bg-elevated))',
            borderColor: 'var(--color-border-muted)',
            color: 'var(--color-text-primary)',
          }}
        >
          <option value="inherit">{t('automation:reportDeliveryTextModeInherit', '沿用报告卡片目标')}</option>
          <option value="custom">{t('automation:reportDeliveryTextModeCustom', '自定义纯文本目标')}</option>
          <option value="none">{t('automation:reportDeliveryTextModeNone', '不发送纯文本')}</option>
        </select>
        {textMode === 'custom' && (
          <Input
            id={`${uid}-text`}
            aria-label={t('automation:reportDeliveryConfirmTextTargets', '纯文本目标')}
            value={textDraft}
            onChange={(e) => {
              const raw = e.target.value;
              setTextDraft(raw);
              onChange({
                ...current,
                text_targets: parseTargetLines(raw),
              });
            }}
            placeholder="feishu:chat:oc_... / discord:channel:..."
          />
        )}
      </div>

      <div className="space-y-1">
        <label htmlFor={`${uid}-failure`} className="block text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {t('automation:reportDeliveryConfirmFailureTargets', '失败告警目标')}
        </label>
        <Input
          id={`${uid}-failure`}
          aria-label={t('automation:reportDeliveryConfirmFailureTargets', '失败告警目标')}
          value={failureDraft}
          onChange={(e) => {
            const raw = e.target.value;
            setFailureDraft(raw);
            onChange({ ...current, failure_targets: parseTargetLines(raw) });
          }}
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
            setReportDraft('');
            setTextDraft('');
            setFailureDraft('');
            setTextMode('inherit');
            onChange(null);
          }}
        >
          {t('automation:reportDeliveryClear', '清除报告外发配置')}
        </Button>
      </div>
    </div>
  );
}
