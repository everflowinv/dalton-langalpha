/**
 * Opening the Computers dialog from a surface that may sit on a page without
 * its host: a refusal's way out, in a banner or in the New workspace dialog.
 */
import { useNavigate } from 'react-router';
// DALTON-HOOK(settings-reorg): Dalton hides Settings' Computers section, so open the dialog in place instead of navigating there.
import { isDaltonBuild } from '@/config/brand';

import { COMPUTERS_SETTINGS_URL, computersHostMounted, openComputersPanel, type ComputersPanelView } from './computerPanelStore';

/**
 * Opens the Computers dialog onto `panel` now. A page without the dialogs'
 * host goes to Settings' Computers section, which mounts one and lists the
 * machines, so the dialog closes onto that list; otherwise the request would
 * show nothing here and open unasked on whichever page mounts one next. The
 * list itself is that section, so asking for it opens nothing over it.
 */
export function useOpenComputers(): (panel: ComputersPanelView) => void {
  const navigate = useNavigate();
  return (panel) => {
    if (computersHostMounted() || isDaltonBuild) {
      openComputersPanel(panel);
      return;
    }
    navigate(COMPUTERS_SETTINGS_URL);
    if (panel.view !== 'list') openComputersPanel({ ...panel, returnTo: 'settings' });
  };
}
