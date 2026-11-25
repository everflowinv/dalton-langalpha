import React from 'react';
// DALTON-HOOK(mobile-web): MoreHorizontal icon for per-row touch menu button
import { CheckSquare, ChevronDown, ChevronRight, Folder, MoreHorizontal, ScrollText, Square } from 'lucide-react';
import type { MemoEntry } from '../../utils/api';
import type { ContextPayload } from '@/components/ui/chat-input.types';
import type { ContextMenuData, TreeNode } from './types';
import { fileGlyph } from './fileMeta';
import { collectTreeFiles } from './fileTree';

// --- Row geometry ---

/** What one level of nesting indents a row by. */
const INDENT_STEP = 12;
/** Where the first guide line sits, inside the row's left padding. */
const GUIDE_OFFSET = 16;
/** The left padding a top-level row keeps before its indent. */
const ROW_PAD = 6;

const rowIndent = (depth: number) => depth * INDENT_STEP + ROW_PAD;

// --- IndentGuides ---

interface IndentGuidesProps {
  depth: number;
}

/** Renders vertical indent guide lines for a given depth */
function IndentGuides({ depth }: IndentGuidesProps): React.ReactElement | null {
  if (depth <= 0) return null;
  const guides: React.ReactElement[] = [];
  for (let i = 0; i < depth; i++) {
    guides.push(
      <span
        key={i}
        className="file-tree-indent-guide"
        style={{ left: i * INDENT_STEP + GUIDE_OFFSET }}
      />
    );
  }
  return <>{guides}</>;
}

// --- DirectoryNode ---

interface DirectoryNodeProps {
  node: TreeNode;
  depth: number;
  showHeader: boolean;
  expandedDirs: Set<string>;
  toggleDir: (dir: string) => void;
  selectMode: boolean;
  selectedPaths: Set<string>;
  toggleDirSelect: (dirFiles: string[]) => void;
  /** The click carries its modifiers: shift extends a range, Cmd/Ctrl toggles one row. */
  onFileClick: (filePath: string, event: React.MouseEvent) => void;
  /** Double click keeps the file — it stops being the reused preview tab. */
  onFileDoubleClick: (filePath: string) => void;
  readOnly: boolean;
  backedUpSet: Set<string>;
  modifiedSet: Set<string>;
  memoedMap: Map<string, MemoEntry>;
  memoedTitle: string;
  /** Files with a tab open, tinted so the tree says where the reader is. */
  openPaths: Set<string>;
  activePath: string | null;
  onAddContext: ((ctx: ContextPayload) => void) | null;
  setContextMenu: (menu: ContextMenuData | null) => void;
  activeContextPath: string | null;
  // DALTON-HOOK(mobile-web): accessible label and menu id for per-row actions triggers
  moreActionsLabel?: string;
  contextMenuId?: string;
}

/** Recursive directory node renderer for the file tree */
export function DirectoryNode({
  node, depth, showHeader,
  expandedDirs, toggleDir,
  selectMode, selectedPaths, toggleDirSelect,
  onFileClick, onFileDoubleClick, readOnly, backedUpSet, modifiedSet, memoedMap, memoedTitle,
  openPaths, activePath,
  onAddContext, setContextMenu, activeContextPath,
  moreActionsLabel = 'More actions',
  contextMenuId = 'file-panel-context-menu',
}: DirectoryNodeProps): React.ReactElement {
  const isRoot = node.name === '/';
  const isCollapsed = isRoot ? false : !expandedDirs.has(node.fullPath);
  const allFiles = collectTreeFiles(node);
  const totalCount = allFiles.length;
  const allSelected = allFiles.every((f) => selectedPaths.has(f));
  const indent = rowIndent(depth + 1);
  const dirLabel = isRoot ? '/' : `${node.name}/`;
  const isDirMenuOpen = activeContextPath === node.fullPath;

  return (
    <div key={node.fullPath}>
      {showHeader && (
        /* DALTON-HOOK(mobile-web): keep directory row ⋯ button adjacent to (outside) role="treeitem" so screen readers and keyboards can focus and activate it independently */
        <div className={`file-tree-row-wrap${isDirMenuOpen ? ' file-panel-item-context-active' : ''}`}>
          <div
            className="file-panel-dir-header file-tree-row"
            role="treeitem"
            tabIndex={-1}
            aria-expanded={!isCollapsed}
            aria-selected={selectMode ? allSelected : undefined}
            data-tree-row=""
            data-row-kind="dir"
            data-row-path={node.fullPath}
            data-row-depth={depth}
            style={depth > 0 ? { paddingLeft: rowIndent(depth) } : undefined}
            onClick={() => selectMode ? toggleDirSelect(allFiles) : toggleDir(node.fullPath)}
          >
            <IndentGuides depth={depth} />
            {selectMode ? (
              allSelected
                ? <CheckSquare className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
                : <Square className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
            ) : isCollapsed
              ? <ChevronRight className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
              : <ChevronDown className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
            }
            <Folder className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
            <span className="text-xs font-medium truncate" style={{ color: 'var(--color-text-tertiary)' }}>
              {dirLabel}
            </span>
            <span className="text-xs" style={{ color: 'var(--color-icon-muted)' }}>
              {totalCount}
            </span>
          </div>
          {!selectMode && (
            <button
              type="button"
              className="file-panel-row-menu-btn tap-target"
              aria-label={`${moreActionsLabel}: ${dirLabel}`}
              title={`${moreActionsLabel}: ${dirLabel}`}
              aria-haspopup="menu"
              aria-expanded={isDirMenuOpen}
              aria-controls={isDirMenuOpen ? contextMenuId : undefined}
              onMouseDown={(e) => e.stopPropagation()}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation();
                if (isDirMenuOpen) {
                  setContextMenu(null);
                  return;
                }
                const rect = e.currentTarget.getBoundingClientRect();
                setContextMenu({
                  x: rect.right,
                  y: rect.bottom,
                  filePath: node.fullPath,
                  kind: 'dir',
                  dirFiles: allFiles,
                  isCollapsed,
                  triggerEl: e.currentTarget,
                });
              }}
            >
              <MoreHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          )}
        </div>
      )}
      {(!isCollapsed || selectMode) && (
        <>
          {/* Subdirectories */}
          {node.children.map((child) => (
            <DirectoryNode
              key={child.fullPath}
              node={child}
              depth={showHeader ? depth + 1 : depth}
              showHeader={true}
              expandedDirs={expandedDirs}
              toggleDir={toggleDir}
              selectMode={selectMode}
              selectedPaths={selectedPaths}
              toggleDirSelect={toggleDirSelect}
              onFileClick={onFileClick}
              onFileDoubleClick={onFileDoubleClick}
              readOnly={readOnly}
              backedUpSet={backedUpSet}
              modifiedSet={modifiedSet}
              memoedMap={memoedMap}
              memoedTitle={memoedTitle}
              openPaths={openPaths}
              activePath={activePath}
              onAddContext={onAddContext}
              setContextMenu={setContextMenu}
              activeContextPath={activeContextPath}
              // DALTON-HOOK(mobile-web): forward localized row-menu label and menu id to nested directories
              moreActionsLabel={moreActionsLabel}
              contextMenuId={contextMenuId}
            />
          ))}
          {/* Files in this directory */}
          {node.files.map((filePath) => {
            const name = filePath.split('/').pop()!;
            const Icon = fileGlyph(filePath);
            const isSelected = selectedPaths.has(filePath);
            const fileDepth = showHeader ? depth + 1 : depth;
            const isFileMenuOpen = activeContextPath === filePath;
            return (
              /* DALTON-HOOK(mobile-web): keep file row ⋯ button adjacent to (outside) role="treeitem" so screen readers and keyboards can focus and activate it independently */
              <div
                key={filePath}
                className={`file-tree-row-wrap${isFileMenuOpen ? ' file-panel-item-context-active' : ''}`}
              >
                <div
                  role="treeitem"
                  tabIndex={-1}
                  aria-selected={selectMode ? isSelected : activePath === filePath}
                  data-tree-row=""
                  data-row-kind="file"
                  data-row-path={filePath}
                  data-row-depth={fileDepth}
                  className={[
                    'file-panel-item file-tree-row',
                    selectMode && isSelected ? 'file-panel-item-selected' : '',
                    isFileMenuOpen ? 'file-panel-item-context-active' : '',
                    openPaths.has(filePath) ? 'file-panel-item-open' : '',
                    activePath === filePath ? 'file-panel-item-active' : '',
                  ].filter(Boolean).join(' ')}
                  style={{ paddingLeft: showHeader ? indent : undefined }}
                  // Every click goes one way, modifiers and all: select mode is
                  // the panel's state, not the row's, and a row that decided for
                  // itself is what made shift-click unreachable while selecting.
                  onClick={(e) => onFileClick(filePath, e)}
                  onDoubleClick={() => { if (!selectMode) onFileDoubleClick(filePath); }}
                  onContextMenu={(e: React.MouseEvent<HTMLDivElement>) => {
                    e.preventDefault();
                    setContextMenu({ x: e.clientX, y: e.clientY, filePath, kind: 'file', triggerEl: e.currentTarget });
                  }}
                >
                  <IndentGuides depth={fileDepth} />
                  {selectMode ? (
                    isSelected
                      ? <CheckSquare className="h-4 w-4 shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
                      : <Square className="h-4 w-4 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                  ) : (
                    <Icon className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                  )}
                  <span className="text-xs truncate" style={{ color: 'var(--color-text-primary)' }}>{name}</span>
                  {!selectMode && (memoedMap.has(filePath) || (!readOnly && (backedUpSet.has(filePath) || modifiedSet.has(filePath)))) && (
                    <span className="file-panel-row-status">
                      {memoedMap.has(filePath) && (
                        <span className="file-panel-memo-badge" title={memoedTitle}>
                          <ScrollText
                            className="h-3.5 w-3.5"
                            style={{ color: 'var(--color-text-tertiary)', opacity: 0.85 }}
                          />
                        </span>
                      )}
                      {!readOnly && (backedUpSet.has(filePath) || modifiedSet.has(filePath)) && (
                        <span
                          className={`file-panel-backup-dot ${backedUpSet.has(filePath) ? 'backed-up' : 'modified'}`}
                          title={backedUpSet.has(filePath) ? 'Backed up' : 'Modified since last backup'}
                        />
                      )}
                    </span>
                  )}
                </div>
                {/* DALTON-HOOK(mobile-web): accessible sibling ⋯ button for files, outside role="treeitem" and keyboard-focusable */}
                {!selectMode && (
                  <button
                    type="button"
                    className="file-panel-row-menu-btn tap-target"
                    aria-label={`${moreActionsLabel}: ${name}`}
                    title={`${moreActionsLabel}: ${name}`}
                    aria-haspopup="menu"
                    aria-expanded={isFileMenuOpen}
                    aria-controls={isFileMenuOpen ? contextMenuId : undefined}
                    onMouseDown={(e) => e.stopPropagation()}
                    onPointerDown={(e) => e.stopPropagation()}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (isFileMenuOpen) {
                        setContextMenu(null);
                        return;
                      }
                      const rect = e.currentTarget.getBoundingClientRect();
                      setContextMenu({ x: rect.right, y: rect.bottom, filePath, kind: 'file', triggerEl: e.currentTarget });
                    }}
                  >
                    <MoreHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                )}
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
