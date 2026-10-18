import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { BookOpen, Check, ChevronRight, Copy } from 'lucide-react';
import type { ProvenanceRecord } from '@/types/chat';
import { isTaskAgentId } from '@/pages/ChatAgent/utils/agentId';

const IDENTITY_LABELS_ZH: Record<string, string> = {
  filing: '一手披露',
  company: '公司/管理层',
  broker_report: '券商研报',
  official_data: '官方数据',
  expert: '专家访谈',
  news: '新闻线索',
  internal_note: '内部笔记',
  derived: '二手摘要',
  street_estimate: '一致预期',
};

export interface ResearchDocSourceProps {
  record: ProvenanceRecord;
  records?: ProvenanceRecord[];
  onOpenRecord: (record: ProvenanceRecord) => void;
}

export function ResearchDocSource({
  record,
  records,
  onOpenRecord,
}: ResearchDocSourceProps): React.ReactElement {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const title = record.title || record.identifier || t('chat.sources.unknownSource', { defaultValue: 'Research Document' });
  const identityKey = record.detail || '';
  const badgeLabel = identityKey
    ? t(`dalton.sources.identity.${identityKey}`, {
        defaultValue: IDENTITY_LABELS_ZH[identityKey] || identityKey,
      })
    : '';
  const subtitle = record.provider || record.identifier || '';
  const subagent = isTaskAgentId(record.agent);
  const accessCount = records ? records.length : 1;

  const handleCopyId = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!record.identifier) return;
    void navigator.clipboard?.writeText(record.identifier);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpenRecord(record)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpenRecord(record);
        }
      }}
      aria-label={`${title} — ${t('chat.sources.viewDetails', { defaultValue: 'View details' })}`}
      className="group relative flex items-center gap-2.5 rounded-lg border border-(--color-border-muted) bg-(--color-bg-card) px-2.5 text-left outline-hidden cursor-pointer hover:border-(--color-border-default) hover:bg-(--color-bg-elevated) focus-visible:ring-2 focus-visible:ring-ring"
      style={{ height: 52, boxShadow: '0 1px 2px rgba(20, 20, 23, 0.05)' }}
    >
      <span
        className="flex shrink-0 items-center justify-center rounded-md"
        style={{ width: 28, height: 28, background: 'var(--color-bg-subtle)' }}
      >
        <BookOpen width={14} height={14} className="shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
      </span>

      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex items-center gap-1.5 min-w-0">
          <span className="truncate text-sm" style={{ color: 'var(--color-text-primary)' }} title={title}>
            {title}
          </span>
          {badgeLabel && (
            <span
              className="inline-flex shrink-0 items-center rounded px-1.5 py-0.2 text-[0.625rem] font-medium"
              style={{
                backgroundColor: 'var(--color-bg-subtle)',
                color: 'var(--color-text-secondary)',
              }}
            >
              {badgeLabel}
            </span>
          )}
        </span>
        <span className="flex items-center gap-1.5 min-w-0">
          {subtitle && (
            <span
              className="truncate text-xs"
              style={{ color: 'var(--color-text-tertiary)' }}
              title={subtitle}
            >
              {subtitle}
            </span>
          )}
          {record.identifier && (
            <button
              type="button"
              onClick={handleCopyId}
              title={`Copy ID: ${record.identifier}`}
              className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 py-0.2 text-[0.625rem] opacity-70 hover:opacity-100 hover:bg-(--color-bg-subtle)"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              <span className="max-w-[110px] truncate font-mono">{record.identifier}</span>
              {copied ? <Check className="h-2.5 w-2.5" /> : <Copy className="h-2.5 w-2.5" />}
            </button>
          )}
        </span>
      </span>

      {accessCount > 1 && (
        <span
          className="inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 text-[0.625rem] font-medium"
          style={{ backgroundColor: 'var(--color-border-muted)', color: 'var(--color-text-tertiary)' }}
        >
          ×{accessCount}
        </span>
      )}

      {subagent && (
        <span
          className="inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 text-[0.625rem] font-medium"
          style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}
        >
          {t('chat.sources.subagent')}
        </span>
      )}

      <ChevronRight
        className="h-4 w-4 shrink-0 opacity-0 transition-opacity group-hover:opacity-50"
        style={{ color: 'var(--color-text-tertiary)' }}
      />
    </div>
  );
}

export default ResearchDocSource;
