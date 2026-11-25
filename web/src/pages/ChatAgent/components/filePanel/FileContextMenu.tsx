import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
// DALTON-HOOK(mobile-web): CheckSquare icon for directory select-files action
import { CheckSquare, Download, FolderOpen, PanelRight, RefreshCw, ScrollText, TextSelect } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ContextMenuData } from './types';
import { downloadLabel, type DownloadState } from '../../utils/downloadNotice';

// DALTON-HOOK(mobile-web): add directory menu actions (expand/collapse, select folder files, download folder files)
export type FileMenuAction =
  | 'add-context'
  | 'add-to-memo'
  | 'open'
  | 'open-new-tab'
  | 'download'
  | 'download-many'
  | 'toggle-dir'
  | 'select-dir'
  | 'download-dir';

interface FileContextMenuProps {
  // DALTON-HOOK(mobile-web): stable DOM id referenced by row menu buttons via aria-controls
  id?: string;
  menu: ContextMenuData;
  onAction: (action: FileMenuAction, filePath: string) => void;
  /** Dismissed without choosing: Escape. An outside click is the owner's to notice. */
  onClose: () => void;
  canAddContext: boolean;
  /** Null where this file cannot go in the memo store at all. */
  memoState: 'absent' | 'present' | null;
  canDownload: boolean;
  // DALTON-HOOK(mobile-web): allow directory menu to enter multi-select mode when panel is writable, and mark overlays belonging to the active mobile full-screen FilePanel
  canSelectDir?: boolean;
  mobileOverlay?: boolean;
  /** This file's save is still being prepared, so Download would only join it. */
  downloadState?: DownloadState;
  /** How many files a bulk save would cover, counting only a selection this file is in. */
  selectedCount: number;
}

const ICON = { className: 'h-3.5 w-3.5', style: { color: 'var(--color-text-tertiary)' } } as const;

/** Keep the menu inside the window; a click near the right edge opens it leftward. */
const EDGE = 8;
function useClampedPosition(x: number, y: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: x, top: y });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    const left = x + width > window.innerWidth - EDGE ? Math.max(EDGE, x - width) : x;
    const top = y + height > window.innerHeight - EDGE ? Math.max(EDGE, y - height) : y;
    setPos({ left, top });
  }, [x, y]);
  return { ref, pos };
}

const items = (menu: HTMLElement) => Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]'));

/** The tree's right-click menu: what can be done with one file without opening it. */
// DALTON-HOOK(mobile-web): accept stable menu id (aria-controls), canSelectDir for folder-row menus, and mobileOverlay for per-panel z-index scoping
export function FileContextMenu({
  id = 'file-panel-context-menu',
  menu, onAction, onClose, canAddContext, memoState, canDownload, canSelectDir = false, mobileOverlay = false, downloadState = 'idle', selectedCount,
}: FileContextMenuProps): React.ReactElement {
  const { t } = useTranslation();
  const { ref, pos } = useClampedPosition(menu.x, menu.y);
  // DALTON-HOOK(mobile-web): remember the trigger that opened this menu so Escape and Tab return focus to it
  const triggerRef = useRef<HTMLElement | null>(null);

  // The menu takes focus as it opens, so the keyboard is already in it.
  useEffect(() => {
    const el = ref.current;
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    triggerRef.current = menu.triggerEl ?? (active && (!el || !el.contains(active)) ? active : triggerRef.current);
    if (el) items(el)[0]?.focus();
  }, [ref, menu.filePath, menu.triggerEl]);

  const restoreTriggerFocus = () => {
    const trigger = menu.triggerEl ?? triggerRef.current;
    if (trigger?.isConnected) trigger.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const el = ref.current;
    if (!el) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      restoreTriggerFocus();
      onClose();
      return;
    }
    // DALTON-HOOK(mobile-web): leaving the menu via Tab/Shift+Tab closes it and returns focus to the opening trigger
    if (e.key === 'Tab') {
      e.preventDefault();
      e.stopPropagation();
      restoreTriggerFocus();
      onClose();
      return;
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const all = items(el);
    const at = all.indexOf(document.activeElement as HTMLElement);
    const step = e.key === 'ArrowDown' ? 1 : -1;
    all[(at + step + all.length) % all.length]?.focus();
  };

  const item = (action: FileMenuAction, icon: React.ReactNode, label: string, disabled = false) => (
    <button
      type="button"
      role="menuitem"
      className="file-panel-context-menu-item"
      disabled={disabled}
      onClick={() => onAction(action, menu.filePath)}
    >
      {icon}
      {label}
    </button>
  );

  const isDir = menu.kind === 'dir';
  const dirFileCount = menu.dirFiles?.length ?? 0;

  // Portaled to the body: the tree column clips its overflow for the open and
  // close animation, and a menu that lives inside it is cut at the panel edge.
  return createPortal(
    <div
      id={id}
      ref={ref}
      className="file-panel-context-menu"
      // DALTON-HOOK(mobile-web): scope z-index elevation strictly to the active mobile FilePanel instance
      data-mobile-overlay-active={mobileOverlay ? '' : undefined}
      role="menu"
      style={pos}
      onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
      // DALTON-HOOK(mobile-web): keep touch taps inside the menu from tripping the document pointerdown closer
      onPointerDown={(e: React.PointerEvent) => e.stopPropagation()}
      onKeyDown={onKeyDown}
    >
      {/* DALTON-HOOK(mobile-web): directory-row menu actions when opened from a folder row */}
      {isDir ? (
        <>
          {item('toggle-dir', <FolderOpen {...ICON} />, menu.isCollapsed ? t('filePanel:expandFolder') : t('filePanel:collapseFolder'))}
          {canSelectDir && dirFileCount > 0 && item('select-dir', <CheckSquare {...ICON} />, t('filePanel:selectFiles'))}
          {canDownload && dirFileCount > 0 && item('download-dir', <Download {...ICON} />, t('filePanel:downloadCount', { count: dirFileCount }))}
        </>
      ) : (
        <>
          {canAddContext && item('add-context', <TextSelect {...ICON} />, t('context.addToContext'))}
          {memoState === 'present' && item('add-to-memo', <RefreshCw {...ICON} />, t('context.syncWithMemo'))}
          {memoState === 'absent' && item('add-to-memo', <ScrollText {...ICON} />, t('context.addToMemo'))}
          {item('open', <FolderOpen {...ICON} />, t('context.openFile'))}
          {item('open-new-tab', <PanelRight {...ICON} />, t('filePanel:openInNewTab'))}
          {canDownload && item(
            'download',
            <Download {...ICON} />,
            downloadLabel(downloadState, t('filePanel:download'), t),
            downloadState !== 'idle',
          )}
          {canDownload && selectedCount > 1
            && item('download-many', <Download {...ICON} />, t('filePanel:downloadCount', { count: selectedCount }))}
        </>
      )}
    </div>,
    document.body,
  );
}
