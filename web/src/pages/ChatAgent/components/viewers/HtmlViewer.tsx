import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle } from 'lucide-react';
import { useTheme } from '@/contexts/ThemeContext';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import SyntaxHighlighter, { oneDark, oneLight } from '../SyntaxHighlighter';
import { useHtmlSandbox } from './html/useHtmlSandbox';
import { useHtmlActions } from './html/useHtmlActions';
import { useServedHtml } from './html/useServedHtml';
import { SERVED_HTML_SANDBOX } from './html/sandbox';
import HtmlActionBar from './html/HtmlActionBar';
import HtmlFullscreenModal from './html/HtmlFullscreenModal';
import './HtmlViewer.css';

interface HtmlViewerProps {
  /** Full source (read unlimited so Source isn't truncated). */
  content: string;
  fileName: string;
  workspaceId: string;
  /** Path within the workspace, e.g. "results/report.html". */
  filePath: string;
  /** Download the server's original bytes for this file. */
  /** Omitted where the viewer may not save the bytes, which drops the save
   *  affordances in the fullscreen toolbar along with it. */
  onTriggerDownload?: () => void;
  /** The serve prefix a share's page was handed. Omitted, the owner's grant
   *  serves the file. */
  servePrefix?: string;
  /** Element id a reference pointed at; the iframe scrolls to it. */
  anchor?: string | null;
  /** Bumped by the panel on every reference open, so the same anchor asked for
   *  twice is two requests rather than one unchanged prop. Read only alongside
   *  `anchor`. */
  anchorSeq?: number | null;
  // DALTON-HOOK(team-sharing): render mirror HTML in sandboxed srcDoc iframe when teamReadOnly
  teamReadOnly?: boolean;
}

export default function HtmlViewer({
  content,
  fileName,
  workspaceId,
  filePath,
  onTriggerDownload,
  servePrefix,
  anchor = null,
  anchorSeq = null,
  teamReadOnly = false,
}: HtmlViewerProps) {
  const { t } = useTranslation();
  const { theme } = useTheme();
  const [mode, setMode] = useState<'preview' | 'source'>('preview');
  const [fullscreen, setFullscreen] = useState(false);

  // An anchored open asks for a place in the rendered document, and Source
  // cannot show one: the tab survives both the file and the anchor changing, so
  // a reference clicked while reading markup left the reader on the same markup
  // with nothing to tell them the click had landed. The request is the seq, not
  // the anchor's text, so asking for one section twice is two requests, while
  // switching to Source with a request still in effect is left alone.
  const request = anchor ? `${anchorSeq ?? ''}\u0000${anchor}` : null;
  const [handled, setHandled] = useState(request);
  if (request !== handled) {
    setHandled(request);
    if (request) setMode('preview');
  }
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const { pushTheme, scrollToAnchor } = useHtmlSandbox({ iframeRef, autoHeight: false });

  // The fragment in `src` lands the first open, and any open naming a different
  // section. It cannot land the same section twice, because that URL is the one
  // already loaded, so ask the document itself on every request.
  useEffect(() => {
    if (anchor) scrollToAnchor(anchor);
  }, [request, anchor, scrollToAnchor]);

  // DALTON-HOOK(team-sharing): skip owner-only grant in colleague mirror mode and use mirror srcDoc preview
  const isReadOnlyMirror = teamReadOnly && !servePrefix;
  const served = useServedHtml(workspaceId, filePath, servePrefix, { teamReadOnly });
  const servedUrl = served.themedUrl;

  const actions = useHtmlActions({
    mode: 'file',
    filePath,
    triggerDownload: onTriggerDownload && (() => Promise.resolve(onTriggerDownload())),
    servedUrl: served.plainUrl,
    openUrl: served.openUrl,
  });

  const isLight = theme === 'light';

  const renderPreview = () => {
    if (servedUrl) {
      // src= loads: the served response's CSP `sandbox` header intersects
      // with this attribute, and serve.py owns the effective policy and the
      // link-click rationale (both must carry the popup tokens).
      return (
        <iframe
          ref={iframeRef}
          src={anchor ? `${servedUrl}#${encodeURIComponent(anchor)}` : servedUrl}
          sandbox={SERVED_HTML_SANDBOX}
          className="html-viewer-frame"
          title={fileName || 'HTML Preview'}
          onLoad={pushTheme}
        />
      );
    }
    if (isReadOnlyMirror) {
      return (
        <iframe
          ref={iframeRef}
          srcDoc={content}
          sandbox={SERVED_HTML_SANDBOX}
          className="html-viewer-frame"
          title={fileName || 'HTML Preview'}
          onLoad={pushTheme}
        />
      );
    }
    if (served.error) {
      return (
        <div className="flex flex-col items-center justify-center gap-3 py-12 text-sm" role="alert">
          <span className="flex items-center gap-2" style={{ color: 'var(--color-text-secondary)' }}>
            <AlertCircle className="h-4 w-4 shrink-0" style={{ color: 'var(--color-icon-danger)' }} />
            {t('filePanel.htmlPreviewFailed')}
          </span>
          <Button variant="outline" size="sm" onClick={served.retry}>
            {t('common.retry')}
          </Button>
        </div>
      );
    }
    return (
      <div className="flex items-center justify-center py-12">
        <Loader size={20} className="text-(--color-text-tertiary)" />
      </div>
    );
  };

  return (
    <div className="html-viewer">
      <div className="html-viewer-toolbar">
        <div className="html-viewer-tabs">
          <button
            className={`html-viewer-tab ${mode === 'preview' ? 'active' : ''}`}
            onClick={() => setMode('preview')}
          >
            {t('filePanel.htmlPreview')}
          </button>
          <button
            className={`html-viewer-tab ${mode === 'source' ? 'active' : ''}`}
            onClick={() => setMode('source')}
          >
            {t('filePanel.htmlSource')}
          </button>
        </div>
        {/* Download/Save-as-PDF and the share link live in the file panel header. */}
        <HtmlActionBar
          onFullscreen={() => setFullscreen(true)}
          fullscreenDisabled={!servedUrl && !isReadOnlyMirror}
          onOpenInNewTab={actions.openInNewTab}
        />
      </div>
      {mode === 'preview' ? renderPreview() : (
        <div className="html-viewer-source">
          <SyntaxHighlighter
            language="markup"
            style={isLight ? oneLight : oneDark}
            customStyle={{ margin: 0, padding: 0, backgroundColor: 'transparent', fontSize: '0.75rem', lineHeight: '1.6' }}
            codeTagProps={{ style: { backgroundColor: 'transparent' } }}
            wrapLongLines
          >
            {content}
          </SyntaxHighlighter>
        </div>
      )}
      {fullscreen && (servedUrl || isReadOnlyMirror) && (
        servedUrl ? (
          <HtmlFullscreenModal
            variant="file"
            open={fullscreen}
            onOpenChange={setFullscreen}
            title={fileName}
            servedUrl={servedUrl}
            actions={actions}
            canDownload={!!onTriggerDownload}
          />
        ) : (
          <HtmlFullscreenModal
            variant="widget"
            open={fullscreen}
            onOpenChange={setFullscreen}
            title={fileName}
            srcDoc={content}
            actions={actions}
            canDownload={!!onTriggerDownload}
          />
        )
      )}
    </div>
  );
}
