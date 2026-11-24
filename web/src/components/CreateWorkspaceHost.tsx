import { Suspense, createContext, lazy, useCallback, useContext, useState, useSyncExternalStore, type ReactNode } from 'react';
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

const OpenCreateWorkspace = createContext<(() => void) | null>(null);

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
  const openDialog = useCallback(() => setOpen(true), []);
  const openCreated = useCallback(
    (ws: Workspace) => navigate(`/chat/${ws.workspace_id}`, { state: { workspaceName: ws.name, workspaceStatus: null } }),
    [navigate],
  );

  return (
    <OpenCreateWorkspace.Provider value={openDialog}>
      {children}
      {open && (
        <Suspense fallback={null}>
          <CreateWorkspaceModal isOpen onClose={() => setOpen(false)} onCreate={createWorkspace} onComplete={openCreated} />
        </Suspense>
      )}
      {/* DALTON-HOOK(settings-reorg): render ComputersDialogHost in place on pages without their own host */}
      <FallbackComputersHost />
    </OpenCreateWorkspace.Provider>
  );
}

/** Opens the "New workspace" dialog; null outside the app shell. */
// eslint-disable-next-line react-refresh/only-export-components
export function useOpenCreateWorkspace(): (() => void) | null {
  return useContext(OpenCreateWorkspace);
}
