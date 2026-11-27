/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type { ComputerFolders } from '../utils/agentPaths';

interface WorkspaceContextValue {
  workspaceId: string | null;
  downloadFile: ((path: string) => void) | null;
  /** The workspace's folders and its siblings', for links that name a sibling by folder. */
  folders: ComputerFolders | null;
  /** Whose images these are on a page with no workspace, where the same path is another image under another link or version. */
  imageScope: string | null;
  // DALTON-HOOK(monitors): resolve document-relative image paths into canonical cache keys for shared subdocuments
  resolveImagePath: ((path: string) => string | null) | null;
}

const WorkspaceContext = createContext<WorkspaceContextValue>({ workspaceId: null, downloadFile: null, folders: null, imageScope: null, resolveImagePath: null });

interface WorkspaceProviderProps {
  workspaceId: string | null;
  downloadFile: ((path: string) => void) | null;
  folders?: ComputerFolders | null;
  imageScope?: string | null;
  resolveImagePath?: ((path: string) => string | null) | null; // DALTON-HOOK(monitors)
  children: ReactNode;
}

export const WorkspaceProvider = ({ workspaceId, downloadFile, folders = null, imageScope = null, resolveImagePath = null, children }: WorkspaceProviderProps) => {
  const value = useMemo(() => ({ workspaceId, downloadFile, folders, imageScope, resolveImagePath }), [workspaceId, downloadFile, folders, imageScope, resolveImagePath]);
  return <WorkspaceContext value={value}>{children}</WorkspaceContext>;
};

export const useWorkspaceId = () => useContext(WorkspaceContext).workspaceId;
export const useWorkspaceDownloadFile = () => useContext(WorkspaceContext).downloadFile;
export const useWorkspaceFolders = () => useContext(WorkspaceContext).folders;
export const useWorkspaceImageScope = () => useContext(WorkspaceContext).imageScope;
export const useWorkspaceResolveImagePath = () => useContext(WorkspaceContext).resolveImagePath; // DALTON-HOOK(monitors)
