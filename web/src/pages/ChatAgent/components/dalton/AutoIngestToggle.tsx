import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Inbox } from 'lucide-react';
// Direct module imports: the primitives barrel also re-exports the
// confirm dialog, which would pull its animation library onto first load.
import { TOOLBAR_CHIP_CLASS, toolbarChipStyle } from '@/components/dalton/primitives/DaltonToolbarPill';
import { useCapabilities } from '@/hooks/useCapabilities';
import { cn } from '@/lib/utils';
import { fetchAutoIngest, updateAutoIngest } from '../../utils/api/workspaces';

/**
 * Workspace-level auto-ingest switch (D1), stored in `workspaces.config`.
 * On: material the workspace receives is filed into sales notes or the wiki
 * by "who is speaking", answering with a one-line receipt. Off: the ingest
 * tools keep their two-step preview/confirm flow.
 */
export function AutoIngestToggle({
  workspaceId,
  readOnly = false,
}: {
  workspaceId: string;
  readOnly?: boolean;
}): React.ReactElement | null {
  const { t } = useTranslation();
  const { hasCapability } = useCapabilities();
  const canAutoIngest = hasCapability('researchdata_bridge');
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');

  React.useEffect(() => {
    let cancelled = false;
    if (!workspaceId || !canAutoIngest) return;
    fetchAutoIngest(workspaceId)
      .then((value) => {
        if (!cancelled) {
          setEnabled(value);
          setLoaded(true);
        }
      })
      .catch(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, canAutoIngest]);

  if (!canAutoIngest || !workspaceId || !loaded || enabled === null) {
    return null;
  }

  const toggle = async () => {
    if (readOnly || busy || enabled === null) return;
    setBusy(true);
    setError('');
    try {
      const next = await updateAutoIngest(workspaceId, !enabled);
      setEnabled(next);
    } catch {
      setError(t('dalton.autoIngest.error'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={readOnly || busy}
      data-testid="dalton-auto-ingest-toggle"
      className={cn(
        TOOLBAR_CHIP_CLASS,
        'tap-target',
        readOnly ? 'cursor-not-allowed opacity-60' : 'press cursor-pointer hover:bg-(--color-bg-subtle)',
      )}
      style={toolbarChipStyle(enabled ? 'success' : 'neutral')}
      title={enabled ? t('dalton.autoIngest.enabledHint') : t('dalton.autoIngest.disabledHint')}
    >
      <Inbox className="h-3.5 w-3.5 shrink-0" />
      <span>{enabled ? t('dalton.autoIngest.on') : t('dalton.autoIngest.off')}</span>
      {error ? <span style={{ color: 'var(--color-loss)' }}>{error}</span> : null}
    </button>
  );
}
