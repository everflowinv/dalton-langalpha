import { Suspense, createContext, lazy, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
// DALTON-HOOK(settings-reorg): mount ComputersDialogHost lazily in place when a Dalton page without its own host opens the Computers dialog.
import { isDaltonBuild } from '@/config/brand';
import { computersHostMounted, useComputersPanel } from '@/pages/ChatAgent/hooks/computerPanelStore';
import { useCreateWorkspace } from '@/pages/ChatAgent/hooks/useCreateWorkspace';
import type { Workspace } from '@/types/api';

const CreateWorkspaceModal = lazy(() => import('@/pages/ChatAgent/components/CreateWorkspaceModal'));
const ComputersDialogHost = lazy(() => import('@/pages/ChatAgent/components/ComputersDialogHost'));

const OpenCreateWorkspace = createContext<(() => void) | null>(null);

// DALTON-HOOK(settings-reorg): fallback Computers host for pages without their own ComputersDialogHost so closing leaves the user on the original page.
function FallbackComputersHost() {
  const { panel, specComputerId } = useComputersPanel();
  const [active, setActive] = useState(false);
  const open = Boolean(panel || specComputerId);

  useEffect(() => {
    if (!isDaltonBuild) return;
    if (open && !computersHostMounted()) {
      setActive(true);
    } else if (!open && active) {
      setActive(false);
    }
  }, [open, active]);

  if (!active) return null;
  return (
    <Suspense fallback={null}>
      <ComputersDialogHost />
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
