import { Plus, Server } from 'lucide-react';
import { useTranslation } from 'react-i18next';

interface GalleryActionsProps {
  onNewWorkspace: () => void;
  onOpenComputers: () => void;
  /** Fill the row in two equal halves (the narrow header). */
  stacked?: boolean;
}

/**
 * The gallery's two header actions. One component because the desktop header
 * and the mobile one show the same pair, and a copy of a button group is how
 * they drift apart.
 */
export function GalleryActions({ onNewWorkspace, onOpenComputers, stacked = false }: GalleryActionsProps) {
  const { t } = useTranslation();
  // Two equal columns, whatever each button's padding and border.
  const fill = stacked ? ' min-w-0 justify-center' : '';
  const pad = stacked ? 'px-3' : 'px-4';
  return (
    <div className={stacked ? 'grid w-full grid-cols-2 gap-2' : 'flex items-center gap-2'}>
      <button
        onClick={onOpenComputers}
        className={`flex items-center gap-1.5 px-3 py-2 h-9 rounded-lg border press hover:bg-foreground/5${fill}`}
        style={{
          borderColor: 'var(--color-border-muted)',
          color: 'var(--color-text-secondary)',
        }}
      >
        <Server className="h-4 w-4 shrink-0" />
        <span className="truncate whitespace-nowrap text-sm font-medium">{t('computer.computers', 'Computers')}</span>
      </button>
      <button
        onClick={onNewWorkspace}
        className={`flex items-center gap-1.5 ${pad} py-2 h-9 rounded-lg press hover:opacity-90${fill}`}
        style={{
          backgroundColor: 'var(--color-btn-primary-bg)',
          color: 'var(--color-btn-primary-text)',
        }}
      >
        <Plus className="h-4 w-4 shrink-0" />
        <span className="truncate whitespace-nowrap text-sm font-medium">{t('workspace.newWorkspace')}</span>
      </button>
    </div>
  );
}

export default GalleryActions;
