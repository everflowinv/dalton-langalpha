import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { bodyMode } from '@/pages/ChatAgent/components/filePanel/fileBody';
import { isHtmlFile } from '@/pages/ChatAgent/utils/filePaths';
import { servedObjectUrl } from '@/pages/SharedChat/api';
import {
  buildSharedTargetUrl,
  createSharedEntryReaders,
  fetchSharedImageObjectUrl,
  probeSharedFileAccess,
  resolveSharedRelativePath,
  splitSharedHref,
} from './sharedMarkdown';

export interface SharedMarkdownLinkRenderInfo {
  href: string;
  children: React.ReactNode;
}

type ProbeFn = (targetPath: string) => Promise<boolean>;

interface SharedMarkdownRelativeLinkProps {
  frameBase: string;
  docPath: string;
  rootRelative: boolean;
  anonymousEntry: boolean;
  href: string;
  probe: ProbeFn;
  onOpen: (targetPath: string, fragment: string | null) => void;
  children: React.ReactNode;
}

function SharedMarkdownRelativeLink({
  frameBase,
  docPath,
  rootRelative,
  anonymousEntry,
  href,
  probe,
  onOpen,
  children,
}: SharedMarkdownRelativeLinkProps): React.ReactElement {
  const { t } = useTranslation();
  const { fragment } = useMemo(() => splitSharedHref(href), [href]);
  const target = useMemo(
    () => resolveSharedRelativePath(docPath, href, { rootRelative, anonymousEntry, decode: true }),
    [docPath, href, rootRelative, anonymousEntry],
  );
  const probeKey = target ? `${frameBase}\0${anonymousEntry ? 'anon' : 'direct'}\0${target}` : '';
  const [result, setResult] = useState<{ key: string; allowed: boolean } | null>(null);

  useEffect(() => {
    if (!target || !probeKey) return;
    let cancelled = false;
    void probe(target).then((allowed) => {
      if (!cancelled) {
        setResult({ key: probeKey, allowed });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [probe, target, probeKey]);

  const status: 'checking' | 'allowed' | 'denied' = !target
    ? 'denied'
    : result?.key === probeKey
      ? (result.allowed ? 'allowed' : 'denied')
      : 'checking';

  if (status === 'allowed' && target) {
    const targetUrl = buildSharedTargetUrl(frameBase, target, anonymousEntry);
    return (
      <a
        href={targetUrl}
        className="underline hover:opacity-80 transition-opacity cursor-pointer"
        style={{ color: 'var(--color-accent-primary)' }}
        onClick={(e: React.MouseEvent) => {
          e.preventDefault();
          onOpen(target, fragment);
        }}
      >
        {children}
      </a>
    );
  }

  if (status === 'denied') {
    const label = t('filePanel:sharedLinkUnavailable');
    return (
      <span
        data-testid="shared-link-placeholder"
        className="inline-flex items-baseline gap-1 rounded px-1"
        style={{
          color: 'var(--color-text-tertiary)',
          backgroundColor: 'var(--color-bg-subtle, rgba(128, 128, 128, 0.08))',
        }}
        title={label}
      >
        <span>{children}</span>
        <span className="text-[0.75em] opacity-80">({label})</span>
      </span>
    );
  }

  return <span>{children}</span>;
}

export interface UseSharedMarkdownViewOptions {
  path: string;
  frameBase: string;
  entryUrl?: string;
  anonymousEntry?: boolean;
  onAnchorLink: (fragment: string) => void;
}

export function useSharedMarkdownView({
  path,
  frameBase,
  entryUrl,
  anonymousEntry = false,
  onAnchorLink,
}: UseSharedMarkdownViewOptions) {
  const { t } = useTranslation();
  const baseKey = `${frameBase}\0${path}`;
  const [navState, setNavState] = useState<{ baseKey: string; subPath: string | null }>({
    baseKey,
    subPath: null,
  });
  const activeSubPath = navState.baseKey === baseKey ? navState.subPath : null;
  const activePath = activeSubPath ?? path;
  const isRootRelative = Boolean(anonymousEntry && !activeSubPath);

  const probeCacheRef = useRef<{ base: string; map: Map<string, Promise<boolean>> }>({
    base: `${frameBase}\0${anonymousEntry ? 'anon' : 'direct'}`,
    map: new Map(),
  });
  const probe = useCallback<ProbeFn>(
    (targetPath: string) => {
      const cacheBase = `${frameBase}\0${anonymousEntry ? 'anon' : 'direct'}`;
      if (probeCacheRef.current.base !== cacheBase) {
        probeCacheRef.current = { base: cacheBase, map: new Map() };
      }
      const existing = probeCacheRef.current.map.get(targetPath);
      if (existing) return existing;
      const req = probeSharedFileAccess(frameBase, targetPath, { anonymousEntry });
      probeCacheRef.current.map.set(targetPath, req);
      return req;
    },
    [frameBase, anonymousEntry],
  );

  const readers = useMemo(
    () => createSharedEntryReaders(frameBase, path, entryUrl, anonymousEntry),
    [frameBase, path, entryUrl, anonymousEntry],
  );

  const resolveImagePath = useCallback(
    (imagePath: string) =>
      resolveSharedRelativePath(activePath, imagePath, {
        rootRelative: isRootRelative,
        anonymousEntry,
      }),
    [activePath, isRootRelative, anonymousEntry],
  );

  const imageDownloader = useCallback(
    (imagePath: string) =>
      fetchSharedImageObjectUrl(frameBase, activePath, imagePath, servedObjectUrl, {
        rootRelative: isRootRelative,
        anonymousEntry,
      }),
    [frameBase, activePath, isRootRelative, anonymousEntry],
  );

  const handleOpenTarget = useCallback(
    (targetPath: string, fragment: string | null) => {
      if (targetPath === activePath && fragment) {
        onAnchorLink(fragment);
        return;
      }
      if (isHtmlFile(targetPath) || bodyMode(targetPath) === 'none') {
        window.open(buildSharedTargetUrl(frameBase, targetPath, anonymousEntry), '_blank', 'noopener,noreferrer');
        return;
      }
      setNavState({ baseKey, subPath: targetPath === path ? null : targetPath });
    },
    [activePath, anonymousEntry, baseKey, frameBase, onAnchorLink, path],
  );

  const renderFileLink = useCallback(
    ({ href, children }: SharedMarkdownLinkRenderInfo) => (
      <SharedMarkdownRelativeLink
        frameBase={frameBase}
        docPath={activePath}
        rootRelative={isRootRelative}
        anonymousEntry={anonymousEntry}
        href={href}
        probe={probe}
        onOpen={handleOpenTarget}
      >
        {children}
      </SharedMarkdownRelativeLink>
    ),
    [frameBase, activePath, isRootRelative, anonymousEntry, probe, handleOpenTarget],
  );

  const entryDisplayName = path.split('/').pop() || path;
  const subDocBar = activeSubPath ? (
    <div
      data-testid="shared-subdoc-bar"
      className="flex items-center gap-2 border-b px-4 py-2 text-xs"
      style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-text-secondary)' }}
    >
      <button
        type="button"
        className="inline-flex items-center gap-1 font-medium hover:underline cursor-pointer"
        style={{ color: 'var(--color-accent-primary)' }}
        onClick={() => setNavState({ baseKey, subPath: null })}
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        <span>{t('filePanel:backToSharedDoc', { name: entryDisplayName })}</span>
      </button>
      <span aria-hidden="true">/</span>
      <span className="truncate font-mono" style={{ color: 'var(--color-text-primary)' }}>
        {activeSubPath.split('/').pop() || activeSubPath}
      </span>
    </div>
  ) : null;

  return {
    activePath,
    readers,
    imageDownloader,
    resolveImagePath,
    renderFileLink,
    subDocBar,
  };
}
