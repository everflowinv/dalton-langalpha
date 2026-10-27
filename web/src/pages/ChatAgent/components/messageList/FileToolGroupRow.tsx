import React, { memo, useState } from 'react';
import { SPRING_FOLD } from './liveZoneTiming';
import { motion, AnimatePresence } from '@/lib/framer';
import { ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { categorizeTool, getToolIcon, getCompletedRowTitle, getCompletedSummary } from '../toolDisplayConfig';
import { classifyAgentPath } from '../../utils/agentPaths';
import { RunningLine } from './ToolCallRow';
import { RunningIcon, FailedIconBadge } from './ActivityRowParts';
import { announceAnchoredToggle } from '../../utils/anchoredToggle';
import { EditDiff } from './EditDiff';
import { editStrings, filePathOf } from './groupFileToolRuns';
import type { ActivityItem, ToolActivityItem } from './activityTypes';

interface FileToolGroupRowProps {
  /** A single file call, or compatible settled calls of one verb. */
  items: ToolActivityItem[];
  onToolCallClick?: (item: ActivityItem) => void;
  onOpenFile?: (path: string, workspaceId?: string) => void;
  /** The run's last call is still out: the title shimmers until it lands. */
  isStreaming?: boolean;
}

/**
 * One row for a run of Read, Edit or Write calls: the verb with a count,
 * then a pill per distinct file. A run that keeps hitting one file (a report
 * edited eight times) names the file once and counts the calls beside it.
 * An Edit run keeps the single chevron, which opens every diff in order.
 */
export const FileToolGroupRow = memo(function FileToolGroupRow({ items, onOpenFile, onToolCallClick, isStreaming = false }: FileToolGroupRowProps): React.ReactElement {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const { root } = items[0];
  const toolName = items[0].toolName || 'Read';
  const IconComponent = getToolIcon(toolName, items[0].toolCall?.args, root);
  const isFailed = items.some((item) => item.isFailed === true || item._liveState === 'failed');
  const single = items.length === 1;
  const item = items[0];
  const failedLabel = t('toolArtifact.a11y.toolCallFailed');

  const paths: string[] = [];
  for (const item of items) {
    const p = filePathOf(item);
    if (p && !paths.includes(p)) paths.push(p);
  }
  const verbKey = toolName === 'Edit' ? 'edit' : toolName === 'Write' ? 'write' : 'read';
  const title = single ? getCompletedRowTitle(toolName, item.toolCall, t, { root }) : paths.length > 1
    ? t(`toolArtifact.fileGroup.${verbKey}`, { count: paths.length })
    : t(`toolArtifact.fileGroup.${verbKey}One`);
  const repeat = !single && paths.length <= 1
    ? (toolName === 'Edit' ? t('toolArtifact.fileGroup.edits', { count: items.length }) : `${items.length}×`)
    : null;

  const diffs = toolName === 'Edit'
    ? items.map((item) => ({ item, path: filePathOf(item), ...editStrings(item) })).filter((d) => d.oldStr || d.newStr)
    : [];
  const hasDiff = diffs.length > 0;
  const category = categorizeTool(toolName, item.toolCall, root);
  const tab = category === 'memoryRead' || category === 'memoryWrite' ? 'memory'
    : category === 'memo' || category === 'memoWrite' ? 'memo' : 'files';
  const openLabel = t('toolArtifact.a11y.openInTab', { tab: t(`filePanel.tabs.${tab}`) });
  const singlePath = filePathOf(item);
  const singleLabel = getCompletedSummary(toolName, item.toolCall, t, root)
    || (singlePath && classifyAgentPath(singlePath, root).kind === 'file' ? singlePath.split('/').pop() : null);
  const openPath = (path: string) => {
    if (onOpenFile) onOpenFile(path);
    else {
      const call = items.find((entry) => filePathOf(entry) === path);
      if (call) onToolCallClick?.(call);
    }
  };
  const openSingle = () => singlePath ? openPath(singlePath) : onToolCallClick?.(item);
  const pills = single ? (singlePath && singleLabel ? [{ path: singlePath, label: singleLabel }] : [])
    : paths.map((path) => ({ path, label: path.split('/').pop() }));

  return (
    <div className={`titem${isFailed ? ' failed' : ''}${isStreaming ? ' running' : ''}`} data-file-group={single ? undefined : toolName}>
      <div className="titem-icon" title={isFailed ? failedLabel : undefined}>
        <RunningIcon running={isStreaming}><IconComponent className="h-4 w-4" /></RunningIcon>
        {isFailed && <FailedIconBadge label={failedLabel} />}
      </div>
      <div className="titem-body">
        <div className="titem-line" style={{ flexWrap: 'wrap', rowGap: '4px' }}>
          {isStreaming && single ? <RunningLine item={item} onOpenPill={openSingle} /> : <>
            {pills.length > 0 ? <span className="titem-title">{title}</span>
                : <button type="button" onClick={openSingle} className="titem-title titem-title-button" title={openLabel}>{title}</button>}
            {pills.map(({ path, label }) => <span key={path} className="titem-pill-wrap">
              <button type="button" onClick={() => openPath(path)} className="obj" title={openLabel}>{label}</button>
            </span>)}
          </>}
          {repeat && <span style={{ opacity: 0.55, fontSize: '0.8125rem' }}>{repeat}</span>}
          {hasDiff && !isStreaming && (
            <button
              type="button"
              onClick={(e) => { announceAnchoredToggle(e.currentTarget); setExpanded(!expanded); }}
              className="inline-flex items-center shrink-0 bg-transparent border-0 p-0 cursor-pointer"
              style={{ color: 'inherit', alignSelf: 'center' }}
              aria-expanded={expanded}
              aria-label={expanded ? t('toolArtifact.a11y.collapseDiff') : t('toolArtifact.a11y.expandDiff')}
            >
              <motion.div animate={{ rotate: expanded ? 90 : 0 }} transition={SPRING_FOLD}>
                <ChevronDown className="h-3 w-3 -rotate-90" style={{ opacity: 0.5 }} />
              </motion.div>
            </button>
          )}
        </div>

        <AnimatePresence initial={false}>
          {expanded && hasDiff && !isStreaming && (
            <motion.div
              key="diffs"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={SPRING_FOLD}
              style={{ overflow: 'hidden' }}
            >
              <div className="mt-2 flex flex-col gap-2">
                {diffs.map((d, i) => (
                  <div key={d.item.id || i}>
                    {paths.length > 1 && d.path && (
                      <div className="font-mono mb-1" style={{ fontSize: '0.6875rem', color: 'var(--Labels-Tertiary)' }}>
                        {d.path.split('/').pop()}
                      </div>
                    )}
                    <EditDiff oldStr={d.oldStr} newStr={d.newStr} />
                  </div>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
});
