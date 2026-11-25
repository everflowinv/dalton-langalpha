import { useEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router';

/**
 * The `?file=<path>` link (a shared report, a copied link, a file picked in
 * the command palette): once the view is on screen and knows its workspace,
 * `open` gets the path and the address drops it, keeping the route state.
 * Read once per navigation rather than once per mount: the chat keeps hidden
 * views alive and the workspace page stays mounted from one workspace to the
 * next, so a later link to the same view has to open too.
 */
// DALTON-HOOK(channel-links): identify rooted or sibling ?file= paths that depend on workspace/computer folder metadata
export function deepLinkNeedsFolders(rawPath: string): boolean {
  const trimmed = rawPath.trim();
  if (!trimmed) return false;
  if (/^file:\/\//i.test(trimmed)) return true;
  if (/^\/?__wsref__\//i.test(trimmed)) return false;
  if (trimmed.startsWith('/')) return true;
  if (/^(?:home\/workspace|home\/daytona)(?:\/|$)/i.test(trimmed)) return true;
  if (trimmed.startsWith('../')) return true;
  return false;
}

export function useFileDeepLink(
  active: boolean,
  workspaceId: string | null | undefined,
  open: (path: string) => void,
  // DALTON-HOOK(channel-links): wait for folder/sibling queries to settle before consuming rooted or sibling ?file= links
  foldersReady = true,
) {
  const location = useLocation();
  const navigate = useNavigate();
  const consumedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!active || !workspaceId || consumedRef.current === location.key) return;
    const params = new URLSearchParams(location.search);
    const raw = params.get('file');
    if (!raw) return;
    // DALTON-HOOK(channel-links): keep ?file= intact until folder metadata settles so sibling/rooted routing resolves accurately
    if (!foldersReady && deepLinkNeedsFolders(raw)) return;
    consumedRef.current = location.key;
    // URLSearchParams.get already percent-decodes; a second decodeURIComponent
    // would throw on a literal '%' in the filename (e.g. 100%25_report.html).
    open(raw);
    params.delete('file');
    const search = params.toString();
    navigate({ pathname: location.pathname, search: search ? `?${search}` : '' }, { replace: true, state: location.state });
  }, [active, workspaceId, foldersReady, location.key, location.search, location.pathname, location.state, navigate, open]);
}
