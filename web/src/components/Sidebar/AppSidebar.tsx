import React, { lazy, useCallback, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
<<<<<<< HEAD
import { ArrowLeft, ArrowRight, LayoutGrid, PanelLeftClose, PanelLeftOpen, Plus, Search, SquarePen } from 'lucide-react'; // DALTON-HOOK(global-search)
import logoLight from '../../assets/img/logo.svg';
import logoDark from '../../assets/img/logo-dark.svg';
import { BrandMark } from '@/components/brand/BrandMark'; // DALTON-HOOK(brand-name)
import { BRAND_MARK, BRAND_NAME } from '@/config/brand'; // DALTON-HOOK(brand-name)
import { useTheme } from '../../contexts/ThemeContext';
=======
import { LayoutGrid, Plus, Search } from 'lucide-react';
>>>>>>> db284da5a
import { useNavActive } from '../nav/useNavActive';
import { useDesktopNavItems } from '../nav/useNavItems';
import { cn } from '@/lib/utils';
import NavigationPanel from '@/pages/ChatAgent/components/NavigationPanel';
import NavDisplayOptions from '@/pages/ChatAgent/components/NavDisplayOptions';
import { useNavTreeProps } from '@/pages/ChatAgent/hooks/useNavTreeProps';
import type { NavWorkspace } from '@/pages/ChatAgent/hooks/useNavigationData';
import AccountMenu from './AccountMenu';
import { MountOnceOpen } from '@/components/MountOnceOpen';
import { canShowUpdates } from '@/lib/desktop';
import { withCopy } from '@/i18n';
import { draftKeys } from '@/lib/composerDrafts';
import { DraftPencil } from '@/components/ui/draft-mark';
import { useChatRoute } from './useChatRoute';
import { paletteKeyShortcuts, paletteShortcut, useCommandPalette } from '../CommandPalette/CommandPaletteProvider';
import { useOpenCreateWorkspace } from '../CreateWorkspaceHost';
import { useSidebarAgents } from '@/pages/ChatAgent/components/sidebarAgentsBridge';
import { useWorkspaceLabel } from '@/hooks/useWorkspaceLabel';
import { GettingStartedGate } from '@/pages/Onboarding';
import { SIDEBAR_DEFAULT_WIDTH, clampSidebarWidth } from './sidebarWidth';
<<<<<<< HEAD
// DALTON-HOOK(team-sharing): collapsible Team workspaces section
import { TeamWorkspacesSection } from '@/pages/ChatAgent/components/dalton/TeamSharing';
// DALTON-HOOK(coverage-tree): sidebar coverage-structure tree (sectors → members)
import { CoverageTreeSection } from '@/pages/ChatAgent/components/dalton/CoverageTreeSection';
import { isDaltonAuth } from '@/config/daltonAuth';
=======
import type { SidebarPeek } from './useSidebarPeek';
>>>>>>> db284da5a
import './Sidebar.css';

// Off first load: only the desktop app has an update to offer, so a browser
// never fetches the button or its copy.
const AppUpdate = lazy(withCopy(() => import('./AppUpdate'), ['appUpdate']));

/** The pages whose own composer keeps a draft, which their item points at
 *  while you are elsewhere. */
const PAGE_DRAFTS: Record<string, string> = {
  '/dashboard': draftKeys.dashboard,
  '/market': draftKeys.market,
};

interface AppSidebarProps {
  /** Out of the layout, Arc-style; the shell owns it and the content column. */
  hidden: boolean;
  /** Current expanded-panel width (px). */
  width: number;
  /** Live width update during a resize drag; commit=true persists (drag end / reset). */
  onWidthChange: (width: number, commit?: boolean) => void;
  /** Hidden, the panel peeks in over the page from the top bar's toggle. */
  peek: SidebarPeek;
  panelRef: React.Ref<HTMLElement>;
}

/**
 * App-shell sidebar: primary nav up top, the workspace/thread tree below,
 * account row at the bottom. Hides out of the layout entirely (state owned by
 * AuthenticatedShell, which also drives --sidebar-width so the content column
 * follows) and peeks back in over the page from the top bar's toggle.
 */
function AppSidebar({ hidden, width, onWidthChange, peek, panelRef }: AppSidebarProps) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const isActive = useNavActive();
  const navItems = useDesktopNavItems();

  const widthRef = useRef(width);
  widthRef.current = width;

  // Armed for the duration of a resize drag; unmount mid-drag (hiding,
  // route swap) would otherwise strand the app-wide sidebar-resizing body
  // class (col-resize cursor + no text selection everywhere).
  const dragCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => dragCleanupRef.current?.(), []);
  // Hiding mid-drag takes the handle away before its pointerup.
  useEffect(() => {
    if (hidden) dragCleanupRef.current?.();
  }, [hidden]);

  // Edge-drag resize. Pointer capture keeps move/up events routed to the handle
  // even when the cursor outruns it; a body class pins the col-resize cursor,
  // blocks text selection, and drops the width transitions for 1:1 tracking.
  const handleResizeStart = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const handle = e.currentTarget;
    try {
      handle.setPointerCapture(e.pointerId);
    } catch {
      // Pointer already released (or synthetic) — listeners below still work
      // for moves over the handle; the drag just loses off-element tracking.
    }
    const startX = e.clientX;
    const startWidth = widthRef.current;
    document.body.classList.add('sidebar-resizing');
    const onMove = (ev: PointerEvent) => {
      onWidthChange(clampSidebarWidth(startWidth + (ev.clientX - startX)));
    };
    // Hoisted declarations: teardown and onEnd reference each other.
    function teardown() {
      dragCleanupRef.current = null;
      document.body.classList.remove('sidebar-resizing');
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', onEnd);
      handle.removeEventListener('pointercancel', onEnd);
    }
    function onEnd(ev: PointerEvent) {
      teardown();
      onWidthChange(clampSidebarWidth(startWidth + (ev.clientX - startX)), true);
    }
    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', onEnd);
    handle.addEventListener('pointercancel', onEnd);
    dragCleanupRef.current = teardown;
  }, [onWidthChange]);

  const handleResizeReset = useCallback(() => {
    onWidthChange(SIDEBAR_DEFAULT_WIDTH, true);
  }, [onWidthChange]);

  const { currentWorkspaceId, currentThreadId } = useChatRoute();
  // Subagent registry published by the visible ChatView (sidebarAgentsBridge).
  // Only surfaced when the slice belongs to the thread this sidebar shows as
  // current — a stale slice from another thread must never render a tree.
  const sidebarAgents = useSidebarAgents();
  const agentsSlice = sidebarAgents && sidebarAgents.threadId === currentThreadId ? sidebarAgents : null;

  // DESKTOP INTENT: the tree's ✎ opens the workspace HOME (thread gallery —
  // centered composer + recent tasks, Codex-style D14), not a blank thread.
  // The mobile drawer deliberately differs; see ChatView's handleNewThread.
  const workspaceLabel = useWorkspaceLabel();
  const openWorkspaceHome = useCallback((wsId: string, ws: NavWorkspace | undefined) => {
    navigate(`/chat/${wsId}`, {
      state: {
        workspaceName: workspaceLabel(ws) || 'Workspace',
        workspaceStatus: ws?.status || null,
      },
    });
  }, [navigate, workspaceLabel]);

  const navTreeProps = useNavTreeProps({
    currentWorkspaceId,
    currentThreadId,
    agents: agentsSlice,
    onNewThread: openWorkspaceHome,
  });

  const openGallery = useCallback(() => navigate('/chat'), [navigate]);
  const palette = useCommandPalette();

  // Same create flow as the gallery, without leaving the current page first.
  const openCreateWorkspace = useOpenCreateWorkspace();

<<<<<<< HEAD
  // DALTON-HOOK(global-search): Command Palette shortcut label for sidebar search buttons
  const searchLabel = t('sidebar.search', { shortcut: paletteShortcut() });

  // One persistent <aside> so the width transition runs across mode flips;
  // the keyed fragments force each mode's content to cleanly remount (React
  // would otherwise morph rail nodes into panel nodes in place), which lets
  // the sidebar-content-in mount animation choreograph the swap.
  if (collapsed) {
    return (
      <aside className="sidebar" data-chrome>
        <React.Fragment key="rail">
        <div className="sidebar-window-drag" aria-hidden="true" />
        <button
          type="button"
          className="sidebar-logo"
          onClick={() => navigate('/dashboard')}
          aria-label={t('sidebar.dashboard')}
        >
          {BRAND_MARK === 'everflow' ? <BrandMark className="h-7 w-7 shrink-0" /> : <img src={logo} alt="" style={{ width: '28px', height: '28px', objectFit: 'contain' }} />} {/* DALTON-HOOK(brand-name) */}
        </button>
        <button
          className="sidebar-rail-toggle"
          onClick={onToggleCollapse}
          aria-label={t('sidebar.expand')}
          title={t('sidebar.expand')}
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
        {/* DALTON-HOOK(global-search): search button with tooltip below expand button in collapsed rail */}
        <TooltipProvider delayDuration={300}>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="sidebar-rail-toggle"
                onClick={(e) => palette?.open(undefined, { returnFocusTo: e.currentTarget })} // DALTON-HOOK(dialog-focus-return): remember this persistent trigger across palette/modal closes
                onPointerEnter={palette?.preload}
                aria-label={searchLabel}
                aria-keyshortcuts={paletteKeyShortcuts()}
                title={searchLabel}
              >
                <Search className="h-4 w-4" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="right" sideOffset={8}>
              {searchLabel}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <nav className="sidebar-nav">
          <TooltipProvider delayDuration={300}>
            {palette && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    className="sidebar-nav-item tap-target"
                    onClick={(e) => palette.open(undefined, { returnFocusTo: e.currentTarget })} // DALTON-HOOK(dialog-focus-return): remember this persistent trigger across palette/modal closes
                    onPointerEnter={palette.preload}
                    aria-label={t('palette.search')}
                    aria-keyshortcuts={paletteKeyShortcuts()}
                  >
                    <Search className="sidebar-nav-icon" />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="right" sideOffset={8}>
                  {t('palette.search')} <span className="sidebar-shortcut">{paletteShortcut()}</span>
                </TooltipContent>
              </Tooltip>
            )}
            {navItems.map((item) => {
              const Icon = item.icon;
              const label = t(item.labelKey);
              const active = isActive(item);
              return (
                <Tooltip key={item.key}>
                  <TooltipTrigger asChild>
                    <button
                      className={cn('sidebar-nav-item tap-target', active && 'active')}
                      onClick={() => navigate(item.key)}
                      aria-label={label}
                      aria-current={active ? 'page' : undefined}
                    >
                      <Icon className="sidebar-nav-icon" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="right" sideOffset={8}>
                    {label}
                  </TooltipContent>
                </Tooltip>
              );
            })}
          </TooltipProvider>
        </nav>
        <div className="sidebar-bottom">
          <GettingStartedGate variant="rail" onExpand={onToggleCollapse} />
          <AccountMenu />
        </div>
        </React.Fragment>
      </aside>
    );
  }

=======
>>>>>>> db284da5a
  return (
    <aside
      ref={panelRef}
      className={cn('sidebar sidebar--panel', hidden && 'sidebar--hidden')}
      data-chrome
      data-peek={peek.open ? '' : undefined}
      inert={hidden && !peek.open}
      style={{ '--sidebar-panel-width': `${width}px` } as React.CSSProperties}
      onPointerEnter={peek.panelHover?.onPointerEnter}
      onPointerLeave={peek.panelHover?.onPointerLeave}
    >
      <div className="sidebar-window-drag" aria-hidden="true" />
<<<<<<< HEAD
      {/* The brand row doubles as titlebar: the buttons inside it declare
          no-drag for themselves, so only the gaps move the window. */}
      <div className="sidebar-panel-header" data-chrome="drag">
        <button
          type="button"
          className="sidebar-brand"
          onClick={() => navigate('/dashboard')}
          aria-label={t('sidebar.dashboard')}
        >
          {BRAND_MARK === 'everflow' ? <BrandMark className="h-7 w-7 shrink-0" /> : <img src={logo} alt="" style={{ width: '28px', height: '28px', objectFit: 'contain' }} />} {/* DALTON-HOOK(brand-name) */}
          <span className="sidebar-wordmark title-font">{BRAND_NAME}</span> {/* DALTON-HOOK(brand-name) */}
        </button>
        {/* DALTON-HOOK(global-search): search icon button between brand and collapse button */}
        <button
          type="button"
          className="sidebar-collapse-btn"
          onClick={(e) => palette?.open(undefined, { returnFocusTo: e.currentTarget })} // DALTON-HOOK(dialog-focus-return): remember this persistent trigger across palette/modal closes
          onPointerEnter={palette?.preload}
          aria-label={searchLabel}
          aria-keyshortcuts={paletteKeyShortcuts()}
          title={searchLabel}
        >
          <Search className="h-4 w-4" />
        </button>
        <button
          className="sidebar-collapse-btn"
          onClick={onToggleCollapse}
          aria-label={t('sidebar.collapse')}
          title={t('sidebar.collapse')}
        >
          <PanelLeftClose className="h-4 w-4" />
        </button>
        <HistoryButtons />
      </div>
=======
      {/* The panel's share of the top bar, under the bar's corner. */}
      <div className="sidebar-panel-header" data-chrome="drag" />
>>>>>>> db284da5a

      <nav className="sidebar-panel-nav">
        {palette && (
          <button
            className="sidebar-panel-item"
            onClick={(e) => palette.open(undefined, { returnFocusTo: e.currentTarget })} // DALTON-HOOK(dialog-focus-return): remember this persistent trigger across palette/modal closes
            onPointerEnter={palette.preload}
            aria-keyshortcuts={paletteKeyShortcuts()}
          >
            <Search className="sidebar-panel-item-icon" />
            <span>{t('palette.search')}</span>
            <kbd className="sidebar-shortcut" aria-hidden>{paletteShortcut()}</kbd>
          </button>
        )}
        {/* New chat opens the workspace gallery: picking (or creating) the
            workspace is part of starting a conversation. The tree's
            per-workspace ✎ skips the chooser for that workspace's home. */}
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = isActive(item);
          return (
            <button
              key={item.labelKey}
              className={cn('sidebar-panel-item', active && 'active')}
              onClick={() => navigate(item.key)}
              aria-current={active ? 'page' : undefined}
            >
              <Icon className="sidebar-panel-item-icon" />
              <span>{t(item.labelKey)}</span>
              {PAGE_DRAFTS[item.key] && (
                <span className="ml-auto flex items-center">
                  <DraftPencil draftKey={PAGE_DRAFTS[item.key]} />
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div className="sidebar-section-header">
        <button className="sidebar-section-title" onClick={openGallery}>
          {t('sidebar.workspaces')}
        </button>
        <div className="sidebar-section-actions">
          <button
            className="nav-panel-dismiss-btn sidebar-section-action-btn"
            onClick={(e) => openCreateWorkspace?.(e.currentTarget)} // DALTON-HOOK(dialog-focus-return): focus comes back to this button
            aria-label={t('workspace.newWorkspace')}
            title={t('workspace.newWorkspace')}
          >
            <Plus className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
          </button>
          <NavDisplayOptions />
          <button
            className="nav-panel-dismiss-btn sidebar-section-action-btn"
            onClick={openGallery}
            aria-label={t('sidebar.workspaceGallery')}
            title={t('sidebar.workspaceGallery')}
          >
            <LayoutGrid className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
          </button>
        </div>
      </div>

      <div className="sidebar-tree">
        <NavigationPanel {...navTreeProps} />
        {/* DALTON-HOOK(coverage-tree): coverage structure tree below My Workspaces */}
        <CoverageTreeSection currentWorkspaceId={currentWorkspaceId} />
        {/* DALTON-HOOK(team-sharing): Team workspaces section below My Workspaces */}
        {isDaltonAuth && <TeamWorkspacesSection currentWorkspaceId={currentWorkspaceId} />}
      </div>

      <GettingStartedGate />

      <div className="sidebar-panel-bottom">
        <MountOnceOpen open={canShowUpdates()}>
          <AppUpdate shown={!hidden} />
        </MountOnceOpen>
        <AccountMenu />
      </div>

      {/* Edge drag = resize (double-click resets to the default width). */}
      {!hidden && (
        <div
          className="sidebar-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label={t('sidebar.resize')}
          title={t('sidebar.resize')}
          onPointerDown={handleResizeStart}
          onDoubleClick={handleResizeReset}
        />
      )}
    </aside>
  );
}

export default AppSidebar;
