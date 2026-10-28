/* eslint-disable react-refresh/only-export-components */
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Inbox } from 'lucide-react';
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
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');

  React.useEffect(() => {
    let cancelled = false;
    if (!workspaceId) return;
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
  }, [workspaceId]);

  if (!workspaceId || !loaded || enabled === null) {
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
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs transition-colors ${
        enabled
          ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
          : 'border-border bg-surface-hover text-muted-foreground'
      } ${readOnly ? 'cursor-not-allowed opacity-60' : 'hover:border-primary/40'}`}
      title={enabled ? t('dalton.autoIngest.enabledHint') : t('dalton.autoIngest.disabledHint')}
    >
      <Inbox className="size-3.5" />
      <span>{enabled ? t('dalton.autoIngest.on') : t('dalton.autoIngest.off')}</span>
      {error ? <span className="text-destructive">{error}</span> : null}
    </button>
  );
}
