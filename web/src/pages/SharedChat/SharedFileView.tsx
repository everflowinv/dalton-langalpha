import React, { useCallback, useEffect, useEffectEvent, useMemo, useState } from 'react';
import { Code, Download, Eye } from 'lucide-react'; // DALTON-HOOK(monitors)
import { useTranslation } from 'react-i18next';
import { toast } from '@/components/ui/use-toast';
import { fetchSharedImageObjectUrl, isMarkdownFile } from '@/lib/dalton/sharedMarkdown'; // DALTON-HOOK(monitors)
import { cn } from '@/lib/utils';
import { bodyMode } from '../ChatAgent/components/filePanel/fileBody';
import { getFileExtension } from '../ChatAgent/components/filePanel/fileMeta';
import { FileViewer } from '../ChatAgent/components/filePanel/FileViewer';
import { useFileBody, useFileBodyCache } from '../ChatAgent/components/filePanel/useFileBody';
import { buildServeUrl } from '../ChatAgent/components/viewers/html/wsfilesUrl';
import { WorkspaceProvider } from '../ChatAgent/contexts/WorkspaceContext';
import { ShareTopBar, ShareTopBarAction } from './ShareTopBar';
import { downloadServedFile, servedObjectUrl, servedReaders, type SharedFileMetadata } from './api';
import './SharePage.css';

interface SharedFileViewProps {
  code: string;
  metadata: SharedFileMetadata;
}

/** Every non-HTML file, read and rendered the way the owner's panel does. */
export function FileBody({ scope, path, frameBase, onDownload, downloading, onGone, rawSource = false }: {
  /** The body cache's scope: the same bytes, whatever prefix renews under it. */
  scope: string;
  path: string;
  frameBase: string;
  onDownload: () => void;
  downloading: boolean;
  /** The file answered 404: under a version, most likely one an Update just ended. */
  onGone?: () => void;
  // DALTON-HOOK(monitors): show raw Markdown source when toggled in shared viewer
  rawSource?: boolean;
}) {
  const readers = useMemo(() => servedReaders(frameBase), [frameBase]);
  const cache = useFileBodyCache({ scope, workspaceId: '', readers });
  const { body, loading, error, refetch } = useFileBody({ cache, path });
  const tellGone = useEffectEvent(() => onGone?.());
  useEffect(() => {
    if (error?.category === 'not_found') tellGone();
  }, [error]);
  // DALTON-HOOK(monitors): resolve document-relative image paths under the shared prefix
  const imageDownloader = useCallback(
    (imagePath: string) => fetchSharedImageObjectUrl(frameBase, path, imagePath, servedObjectUrl),
    [frameBase, path],
  );
  // With an anchor handler and no file opener, a link to another workspace
  // file is plain text: a visitor can open only the files listed.
  const scrollToAnchor = useCallback((id: string) => {
    document.getElementById(id)?.scrollIntoView({ block: 'start' });
  }, []);
  // A grid or a paged document uses the width; prose and code read in a column.
  const wide = bodyMode(path) === 'buffer' || getFileExtension(path) === 'csv';
  // A PDF scrolls inside its own viewer, which needs the page's height for it:
  // left to grow with its pages, it would draw every one of them at once.
  const fill = getFileExtension(path) === 'pdf';

  return (
    <div className="share-scroll">
      <div className={cn(!wide && 'share-document', fill && 'share-fill')}>
        <WorkspaceProvider workspaceId={null} downloadFile={imageDownloader} imageScope={scope}>
          <FileViewer
            path={path}
            body={body}
            loading={loading}
            error={error}
            onRetry={() => void refetch()}
            onDownload={onDownload}
            onDownloadInFallback={onDownload}
            downloadState={downloading ? 'preparing' : 'idle'}
            workspaceId=""
            onAnchorLink={scrollToAnchor}
            markdownSource={rawSource} // DALTON-HOOK(monitors)
          />
        </WorkspaceProvider>
      </div>
    </div>
  );
}

/**
 * A file behind its owner's `/a/` link that is not HTML (an HTML file opens in
 * `OwnerArtifactView`): one line of chrome, then the file, read under the
 * serve prefix the metadata named.
 */
export default function SharedFileView({ code, metadata }: SharedFileViewProps): React.ReactElement {
  const { t } = useTranslation();
  const { path, name, frame_base: frameBase } = metadata;
  const [busy, setBusy] = useState(false);
  // DALTON-HOOK(monitors): toggle raw Markdown source on shared file page
  const isMarkdown = isMarkdownFile(path);
  const [rawSource, setRawSource] = useState(false);

  const download = async () => {
    setBusy(true);
    try {
      await downloadServedFile(buildServeUrl(frameBase, path), name);
    } catch (err) {
      console.error('[SharedFileView] Download failed:', err);
      toast({ description: t('filePanel:downloadFailed'), variant: 'destructive' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="share-page">
      <ShareTopBar
        name={name}
        actions={(
          <>
            {/* DALTON-HOOK(monitors): View raw / View rendered toggle for Markdown files */}
            {isMarkdown && (
              <ShareTopBarAction icon={rawSource ? Eye : Code} label={rawSource ? t('filePanel:viewRendered') : t('filePanel:viewRaw')} onClick={() => setRawSource((v) => !v)} />
            )}
            <ShareTopBarAction icon={Download} label={t('filePanel:download')} onClick={() => void download()} disabled={busy} />
          </>
        )}
      />
      <div className="share-content">
        {/* DALTON-HOOK(monitors): pass rawSource toggle to FileBody */}
        <FileBody scope={`share:${code}`} path={path} frameBase={frameBase} onDownload={() => void download()} downloading={busy} rawSource={isMarkdown && rawSource} />
      </div>
    </div>
  );
}
