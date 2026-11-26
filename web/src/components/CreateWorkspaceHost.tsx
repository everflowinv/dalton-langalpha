import { Suspense, createContext, lazy, useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { useIsMutating } from '@tanstack/react-query';
// DALTON-HOOK(settings-reorg): mount ComputersDialogHost lazily in place when a Dalton page without its own host opens the Computers dialog.
import { isDaltonBuild } from '@/config/brand';
import { computerOutcomeWatch } from '@/pages/ChatAgent/hooks/computerOutcomes';
import { useComputersPanel, usePageComputersHostMounted } from '@/pages/ChatAgent/hooks/computerPanelStore';
import { CREATE_COMPUTER_MUTATION_KEY } from '@/pages/ChatAgent/hooks/useComputers';
import { useCreateWorkspace } from '@/pages/ChatAgent/hooks/useCreateWorkspace';
import type { Workspace } from '@/types/api';
import { withCopy } from '@/i18n';

const CreateWorkspaceModal = lazy(withCopy(() => import('@/pages/ChatAgent/components/CreateWorkspaceModal'), [
  'filePanel',
]));
// DALTON-HOOK(settings-reorg): fallback Computers host for pages without their own ComputersDialogHost.
const ComputersDialogHost = lazy(withCopy(() => import('@/pages/ChatAgent/components/ComputersDialogHost'), [
  'filePanel', 'settings', 'setup',
]));

type OpenCreateWorkspace = (opener?: HTMLElement | null) => void; // DALTON-HOOK(dialog-focus-return): the click handler names its control

const OpenCreateWorkspace = createContext<OpenCreateWorkspace | null>(null);

// DALTON-HOOK(settings-reorg): fallback Computers host for pages without their own ComputersDialogHost.
// Yields immediately when a page-level host mounts (e.g. browser back to /chat) so two dialogs never
// appear, and stays mounted after dialog close while background preparation or spec changes settle so
// completion toasts still fire on the current page.
export function FallbackComputersHost() {
  const hasPageHost = usePageComputersHostMounted();
  const { panel, specComputerId } = useComputersPanel();
  const outcomeWatch = useSyncExternalStore(
    computerOutcomeWatch.subscribe,
    computerOutcomeWatch.get,
  );
  const creatingComputer = useIsMutating({ mutationKey: CREATE_COMPUTER_MUTATION_KEY }) > 0;

  if (!isDaltonBuild || hasPageHost) return null;
  const open = Boolean(panel || specComputerId);
  const watchingBackground =
    creatingComputer || outcomeWatch.preparing.size > 0 || outcomeWatch.specChanges.size > 0;
  if (!open && !watchingBackground) return null;

  return (
    <Suspense fallback={null}>
      <ComputersDialogHost fallback />
    </Suspense>
  );
}

/**
 * The app shell's "New workspace" dialog, which opens the workspace once made.
 * One for every surface that offers it (the sidebar, the command palette), and
 * held above them, since the palette closes as the dialog opens.
 */
export function CreateWorkspaceHost({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const createWorkspace = useCreateWorkspace();
  const [open, setOpen] = useState(false);
  // DALTON-HOOK(mobile-web): Safari does not focus a button on click, and the dialog mounts only once open,
  // so the host remembers the last pressed control and hands it to the dialog as the place to return focus.
  // Only a press just before this open counts, so a shortcut never sends focus to an older, unrelated click.
  const lastPressedRef = useRef<{ el: HTMLElement; at: number } | null>(null);
  const [opener, setOpener] = useState<HTMLElement | null>(null);
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      const el = e.target instanceof Element
        ? e.target.closest<HTMLElement>('button, a[href], [role="button"]')
        : null;
      lastPressedRef.current = el ? { el, at: e.timeStamp } : null;
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, []);
  const openDialog = useCallback((opener?: HTMLElement | null) => {
    const focused = document.activeElement;
    const pressed = lastPressedRef.current;
    lastPressedRef.current = null;
    const recentPress = pressed && performance.now() - pressed.at < 1000 ? pressed.el : null;
    // DALTON-HOOK(dialog-focus-return): the control the click handler named comes first. The two guesses
    // stand in for an opener that has none to give (such as the desktop app menu).
    setOpener(opener ?? (focused instanceof HTMLElement && focused !== document.body ? focused : recentPress));
    setOpen(true);
  }, []);
  const openCreated = useCallback(
    (ws: Workspace) => navigate(`/chat/${ws.workspace_id}`, { state: { workspaceName: ws.name, workspaceStatus: null } }),
    [navigate],
  );

  return (
    <OpenCreateWorkspace.Provider value={openDialog}>
      {children}
      {open && (
        <Suspense fallback={null}>
          <CreateWorkspaceModal isOpen returnFocusTo={opener} onClose={() => setOpen(false)} onCreate={createWorkspace} onComplete={openCreated} />
        </Suspense>
      )}
      {/* DALTON-HOOK(settings-reorg): render ComputersDialogHost in place on pages without their own host */}
      <FallbackComputersHost />
    </OpenCreateWorkspace.Provider>
  );
}

/**
 * Opens the "New workspace" dialog; null outside the app shell. A click
 * handler passes the control it runs for, and focus comes back to it on close.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useOpenCreateWorkspace(): OpenCreateWorkspace | null {
  return useContext(OpenCreateWorkspace);
}
