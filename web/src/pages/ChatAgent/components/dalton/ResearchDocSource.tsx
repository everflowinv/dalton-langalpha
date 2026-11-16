import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { BookOpen, Check, ChevronRight, Copy } from 'lucide-react';
import { TagBadge } from '@/components/mcp/McpPrimitives';
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

const CARD_CHROME =
  'group relative flex items-center gap-2.5 rounded-[var(--cs-radius-box,var(--radius-lg))] border px-2.5 text-left transition-colors ' +
  'border-[color:var(--color-shell-edge,var(--color-border-muted))] bg-[color:var(--color-shell-float,var(--color-bg-card))] ' +
  'hover:border-[color:var(--color-shell-edge-hover,var(--color-border-default))] hover:bg-[color:var(--color-shell-float,var(--color-bg-elevated))]';

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

  const title =
    record.title ||
    record.identifier ||
    t('chat.sources.unknownSource', { defaultValue: 'Research Document' });
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
      className={CARD_CHROME}
      style={{
        height: 52,
        boxShadow: 'var(--cs-frame-shadow, none)',
      }}
    >
      <button
        type="button"
        onClick={() => onOpenRecord(record)}
        aria-label={`${title} — ${t('chat.sources.viewDetails', { defaultValue: 'View details' })}`}
        className="flex min-w-0 flex-1 items-center gap-2.5 text-left outline-hidden cursor-pointer focus-visible:ring-2 focus-visible:ring-ring rounded-md"
      >
        <span
          className="flex shrink-0 items-center justify-center rounded-[var(--cs-radius-mark,var(--radius-md))]"
          style={{
            width: 28,
            height: 28,
            background: 'var(--color-shell-fill, var(--color-bg-subtle))',
            boxShadow: 'inset 0 0 0 1px var(--color-shell-rule, transparent)',
          }}
        >
          <BookOpen
            width={14}
            height={14}
            className="shrink-0"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
        </span>

        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex items-center gap-1.5 min-w-0">
            <span
              className="truncate text-sm"
              style={{ color: 'var(--color-text-primary)' }}
              title={title}
            >
              {title}
            </span>
            {badgeLabel && <TagBadge soft>{badgeLabel}</TagBadge>}
          </span>
          {subtitle && (
            <span
              className="truncate text-xs"
              style={{ color: 'var(--color-text-tertiary)' }}
              title={subtitle}
            >
              {subtitle}
            </span>
          )}
        </span>
      </button>

      {record.identifier && (
        <button
          type="button"
          onClick={handleCopyId}
          title={t('dalton.sources.copyId', { id: record.identifier })}
          aria-label={t('dalton.sources.copyId', { id: record.identifier })}
          className="relative z-10 inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[11px] opacity-75 hover:opacity-100 hover:bg-(--color-bg-subtle) cursor-pointer"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          <span className="max-w-[96px] truncate font-mono">{record.identifier}</span>
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
        </button>
      )}

      {accessCount > 1 && <TagBadge soft>×{accessCount}</TagBadge>}

      {subagent && <TagBadge soft>{t('chat.sources.subagent')}</TagBadge>}

      <ChevronRight
        aria-hidden="true"
        className="h-4 w-4 shrink-0 opacity-0 transition-opacity group-hover:opacity-50 pointer-events-none"
        style={{ color: 'var(--color-text-tertiary)' }}
      />
    </div>
  );
}

export default ResearchDocSource;
