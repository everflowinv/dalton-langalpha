import { useTranslation } from 'react-i18next';
import { Maximize2, Minimize2, ExternalLink, Download, FileDown, MoreHorizontal } from 'lucide-react';
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger, DropdownMenuItem } from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';
import './HtmlActionBar.css';

interface HtmlActionBarProps {
  /** Omit where a second surface cannot be opened — see HtmlActions.openInNewTab. */
  onOpenInNewTab?: () => void;
  /** Download/PDF live in a secondary "more" menu. Omit both on surfaces where
   *  they're owned elsewhere (the file toolbar defers to the panel header menu). */
  onDownload?: () => void;
  onExportPdf?: () => void | Promise<void>;
  /** Toggle fullscreen. Omit to hide the expand/exit button. */
  onFullscreen?: () => void;
  /** Shown but inert, e.g. until the served URL the fullscreen frame loads exists. */
  fullscreenDisabled?: boolean;
  isFullscreen?: boolean;
  /** Visual context — 'overlay' for the inline-widget hover bar. */
  variant?: 'toolbar' | 'overlay';
  className?: string;
}

/** Presentational icon-button row for HTML surfaces (widget, file viewer, fullscreen). */
export default function HtmlActionBar({
  onOpenInNewTab,
  onDownload,
  onExportPdf,
  onFullscreen,
  fullscreenDisabled = false,
  isFullscreen = false,
  variant = 'toolbar',
  className,
}: HtmlActionBarProps) {
  const { t } = useTranslation();

  return (
    <div className={cn('html-action-bar', variant === 'overlay' && 'html-action-bar-overlay', className)}>
      {/* DALTON-HOOK(mobile-web): expand touch hit area via .tap-target on coarse-pointer devices */}
      {onFullscreen && (
        <button
          type="button"
          className="html-action-btn tap-target"
          onClick={onFullscreen}
          disabled={fullscreenDisabled}
          title={isFullscreen ? t('filePanel:exitFullscreen') : t('filePanel:fullscreen')}
          aria-label={isFullscreen ? t('filePanel:exitFullscreen') : t('filePanel:fullscreen')}
        >
          {isFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
        </button>
      )}
      {onOpenInNewTab && (
        <button
          type="button"
          className="html-action-btn tap-target"
          onClick={onOpenInNewTab}
          title={t('filePanel:openInNewTab')}
          aria-label={t('filePanel:openInNewTab')}
        >
          <ExternalLink className="h-4 w-4" />
        </button>
      )}
      {(onDownload || onExportPdf) && (
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            {/* DALTON-HOOK(mobile-web): expand touch hit area via .tap-target on coarse-pointer devices */}
            <button
              type="button"
              className="html-action-btn tap-target"
              title={t('filePanel:moreActions')}
              aria-label={t('filePanel:moreActions')}
            >
              <MoreHorizontal className="h-4 w-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" sideOffset={4}>
            {onDownload && (
              <DropdownMenuItem onSelect={() => onDownload()}>
                <Download className="h-3.5 w-3.5" />
                {t('filePanel:downloadAsHtml')}
              </DropdownMenuItem>
            )}
            {onExportPdf && (
              <DropdownMenuItem onSelect={() => void onExportPdf()}>
                <FileDown className="h-3.5 w-3.5" />
                {t('filePanel:saveAsPdf')}
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
