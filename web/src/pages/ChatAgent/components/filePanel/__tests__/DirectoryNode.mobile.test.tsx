import { useRef, useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import '@/i18n';
import { DirectoryNode } from '../DirectoryNode';
import { FileContextMenu } from '../FileContextMenu';
import { QuoteComposer } from '../../selectionQuote/SelectionQuote';
import type { ContextMenuData, TreeNode } from '../types';

const sampleNode: TreeNode = {
  name: 'reports',
  fullPath: 'reports',
  children: [],
  files: ['reports/q4.md'],
};

function renderNode(overrides: Partial<React.ComponentProps<typeof DirectoryNode>> = {}) {
  const onFileClick = vi.fn();
  const toggleDir = vi.fn();
  const setContextMenu = vi.fn();
  const view = render(
    <DirectoryNode
      node={sampleNode}
      depth={0}
      showHeader={true}
      expandedDirs={new Set(['reports'])}
      toggleDir={toggleDir}
      selectMode={false}
      selectedPaths={new Set()}
      toggleDirSelect={vi.fn()}
      onFileClick={onFileClick}
      onFileDoubleClick={vi.fn()}
      readOnly={false}
      backedUpSet={new Set()}
      modifiedSet={new Set()}
      memoedMap={new Map()}
      memoedTitle="In memo"
      openPaths={new Set()}
      activePath={null}
      onAddContext={null}
      setContextMenu={setContextMenu}
      activeContextPath={null}
      moreActionsLabel="More actions"
      contextMenuId="test-file-menu"
      {...overrides}
    />,
  );
  return { ...view, onFileClick, toggleDir, setContextMenu };
}

function InteractiveTreeHarness({ mobileOverlay = false }: { mobileOverlay?: boolean }) {
  const [menu, setMenu] = useState<ContextMenuData | null>(null);
  return (
    <>
      <DirectoryNode
        node={sampleNode}
        depth={0}
        showHeader={true}
        expandedDirs={new Set(['reports'])}
        toggleDir={vi.fn()}
        selectMode={false}
        selectedPaths={new Set()}
        toggleDirSelect={vi.fn()}
        onFileClick={vi.fn()}
        onFileDoubleClick={vi.fn()}
        readOnly={false}
        backedUpSet={new Set()}
        modifiedSet={new Set()}
        memoedMap={new Map()}
        memoedTitle="In memo"
        openPaths={new Set()}
        activePath={null}
        onAddContext={null}
        setContextMenu={setMenu}
        activeContextPath={menu?.filePath ?? null}
        moreActionsLabel="More actions"
        contextMenuId="test-file-menu"
      />
      {menu && (
        <FileContextMenu
          id="test-file-menu"
          menu={menu}
          onAction={vi.fn()}
          onClose={() => setMenu(null)}
          canAddContext={true}
          memoState={null}
          canDownload={true}
          canSelectDir={true}
          mobileOverlay={mobileOverlay}
          selectedCount={0}
        />
      )}
    </>
  );
}

describe('DirectoryNode accessible row menu buttons (files and directories)', () => {
  it('renders keyboard-focusable sibling ⋯ buttons outside role="treeitem" for both files and directories with filenames in accessible names', () => {
    renderNode();

    const fileBtn = screen.getByRole('button', { name: 'More actions: q4.md' });
    const dirBtn = screen.getByRole('button', { name: 'More actions: reports/' });

    // Must NOT be nested inside role="treeitem" (so VoiceOver/screen readers can activate them independently).
    expect(fileBtn.closest('[role="treeitem"]')).toBeNull();
    expect(dirBtn.closest('[role="treeitem"]')).toBeNull();

    // Must be naturally keyboard-focusable (not tabIndex=-1).
    expect(fileBtn.tabIndex).toBeGreaterThanOrEqual(0);
    expect(dirBtn.tabIndex).toBeGreaterThanOrEqual(0);
    fileBtn.focus();
    expect(document.activeElement).toBe(fileBtn);
    dirBtn.focus();
    expect(document.activeElement).toBe(dirBtn);

    expect(fileBtn).toHaveAttribute('aria-haspopup', 'menu');
    expect(dirBtn).toHaveAttribute('aria-haspopup', 'menu');
    expect(fileBtn).toHaveAttribute('aria-expanded', 'false');
    expect(dirBtn).toHaveAttribute('aria-expanded', 'false');
  });

  it('opens the file context menu from the file ⋯ button without triggering onFileClick', () => {
    const { onFileClick, setContextMenu } = renderNode();
    const menuBtn = screen.getByRole('button', { name: 'More actions: q4.md' });
    menuBtn.getBoundingClientRect = () => ({
      left: 300,
      top: 120,
      right: 332,
      bottom: 148,
      width: 32,
      height: 28,
      x: 300,
      y: 120,
      toJSON: () => ({}),
    });

    fireEvent.click(menuBtn);

    expect(onFileClick).not.toHaveBeenCalled();
    expect(setContextMenu).toHaveBeenCalledWith({
      x: 332,
      y: 148,
      filePath: 'reports/q4.md',
      kind: 'file',
      triggerEl: menuBtn,
    });
  });

  it('opens the directory context menu from the directory ⋯ button without collapsing the folder', () => {
    const { toggleDir, setContextMenu } = renderNode();
    const dirBtn = screen.getByRole('button', { name: 'More actions: reports/' });
    dirBtn.getBoundingClientRect = () => ({
      left: 300,
      top: 80,
      right: 332,
      bottom: 108,
      width: 32,
      height: 28,
      x: 300,
      y: 80,
      toJSON: () => ({}),
    });

    fireEvent.click(dirBtn);

    expect(toggleDir).not.toHaveBeenCalled();
    expect(setContextMenu).toHaveBeenCalledWith({
      x: 332,
      y: 108,
      filePath: 'reports',
      kind: 'dir',
      dirFiles: ['reports/q4.md'],
      isCollapsed: false,
      triggerEl: dirBtn,
    });
  });

  it('wires aria-controls to the active menu id and toggles closed on second click', () => {
    const { setContextMenu } = renderNode({ activeContextPath: 'reports/q4.md' });
    const menuBtn = screen.getByRole('button', { name: 'More actions: q4.md' });
    expect(menuBtn).toHaveAttribute('aria-expanded', 'true');
    expect(menuBtn).toHaveAttribute('aria-controls', 'test-file-menu');

    fireEvent.click(menuBtn);
    expect(setContextMenu).toHaveBeenCalledWith(null);
  });

  it('renders directory menu items in FileContextMenu with matching menu id', () => {
    const onAction = vi.fn();
    render(
      <FileContextMenu
        id="test-file-menu"
        menu={{
          x: 100,
          y: 100,
          filePath: 'reports',
          kind: 'dir',
          dirFiles: ['reports/q4.md', 'reports/q3.md'],
          isCollapsed: false,
        }}
        onAction={onAction}
        onClose={vi.fn()}
        canAddContext={true}
        memoState={null}
        canDownload={true}
        canSelectDir={true}
        selectedCount={0}
      />,
    );

    const menu = screen.getByRole('menu');
    expect(menu).toHaveAttribute('id', 'test-file-menu');
    fireEvent.click(screen.getByRole('menuitem', { name: /Collapse|收起/i }));
    expect(onAction).toHaveBeenCalledWith('toggle-dir', 'reports');
    fireEvent.click(screen.getByRole('menuitem', { name: /Select files|选择文件/i }));
    expect(onAction).toHaveBeenCalledWith('select-dir', 'reports');
    fireEvent.click(screen.getByRole('menuitem', { name: /Download 2 files|下载 2 个文件/i }));
    expect(onAction).toHaveBeenCalledWith('download-dir', 'reports');
  });

  it.each([
    ['Escape', { key: 'Escape' }],
    ['Tab', { key: 'Tab' }],
    ['Shift+Tab', { key: 'Tab', shiftKey: true }],
  ])('closes FileContextMenu and restores focus to the opening ⋯ button on %s', (_, keyInit) => {
    render(<InteractiveTreeHarness />);
    const fileBtn = screen.getByRole('button', { name: 'More actions: q4.md' });
    fileBtn.focus();
    fireEvent.click(fileBtn);

    const menu = screen.getByRole('menu');
    expect(menu.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document.activeElement!, keyInit);
    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(fileBtn);
  });

  it('marks portaled FileContextMenu and QuoteComposer with data-mobile-overlay-active only when owned by an active mobile FilePanel', () => {
    const { rerender } = render(<InteractiveTreeHarness mobileOverlay={false} />);
    fireEvent.click(screen.getByRole('button', { name: 'More actions: q4.md' }));
    expect(screen.getByRole('menu')).not.toHaveAttribute('data-mobile-overlay-active');

    rerender(<InteractiveTreeHarness mobileOverlay={true} />);
    expect(screen.getByRole('menu')).toHaveAttribute('data-mobile-overlay-active', '');

    function QuoteHarness({ mobileOverlay, hidden = false }: { mobileOverlay: boolean; hidden?: boolean }) {
      const boundsRef = useRef<HTMLDivElement>(null);
      return (
        <div ref={boundsRef}>
          <QuoteComposer
            anchor={{ id: 1, kind: 'rect', text: 'quoted snippet', rect: { left: 10, top: 10, bottom: 30 } }}
            boundsRef={boundsRef}
            label="Quote from q4.md"
            placeholder="Reply"
            hidden={hidden}
            mobileOverlay={mobileOverlay}
            onAdd={vi.fn()}
            onDismiss={vi.fn()}
          />
        </div>
      );
    }

    const quoteView = render(<QuoteHarness mobileOverlay={false} />);
    // The composer stays visibility:hidden until placed, which jsdom never does, so its accessible name is not computed.
    expect(screen.getByRole('dialog', { hidden: true })).not.toHaveAttribute('data-mobile-overlay-active');

    quoteView.rerender(<QuoteHarness mobileOverlay={true} />);
    expect(screen.getByRole('dialog', { hidden: true })).toHaveAttribute('data-mobile-overlay-active', '');

    quoteView.rerender(<QuoteHarness mobileOverlay={true} hidden={true} />);
    expect(screen.getByRole('dialog', { hidden: true })).not.toHaveAttribute('data-mobile-overlay-active');
  });

  it('hides file and directory ⋯ buttons while in selectMode', () => {
    renderNode({ selectMode: true });
    expect(screen.queryByRole('button', { name: /More actions/i })).toBeNull();
  });
});
