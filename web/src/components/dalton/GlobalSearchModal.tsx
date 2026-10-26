import React, {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { FileText, MessageSquare, Search, StickyNote, X } from 'lucide-react';
import { ModalShell } from '@/components/ui/ModalShell';
import { Loader } from '@/components/ui/loader';
import { useNow } from '@/hooks/useNow';
import { useLocale } from '@/hooks/useLocale';
import { resolveChatWorkspace, useHomeWorkspaceId } from '@/hooks/useHomeWorkspace';
import { relativeTime } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchGlobalSearch,
  type GlobalSearchConversationItem,
  type GlobalSearchDocumentItem,
  type HighlightSpan,
} from '@/lib/dalton/searchApi';
import { MEMO_USER_DIR } from '@/pages/ChatAgent/utils/agentPaths.generated';

export {
  closeGlobalSearch,
  GlobalSearchHost,
  openGlobalSearch,
  toggleGlobalSearch,
} from './globalSearchHost';

// ---------------------------------------------------------------------------
// Snippet code-point highlighter
// ---------------------------------------------------------------------------

export interface GlobalSearchModalProps {
  open: boolean;
  onClose: () => void;
}

type NavigableSearchResult =
  | { section: 'conversations'; item: GlobalSearchConversationItem }
  | { section: 'documents'; item: GlobalSearchDocumentItem };

/**
 * Slices `snippet` by Unicode code points (`Array.from(snippet)`) rather than
 * UTF-16 code units so emoji and supplementary ideographs align with Python's
 * character indices.
 */
export function renderHighlightedSnippet(
  snippet: string,
  highlights: HighlightSpan[],
): React.ReactNode {
  if (!snippet) return null;
  if (!Array.isArray(highlights) || highlights.length === 0) {
    return snippet;
  }

  const codePoints = Array.from(snippet);
  const totalPoints = codePoints.length;
  const slicePoints = (start: number, end: number) => codePoints.slice(start, end).join('');

  const validSpans = highlights
    .filter(
      (span): span is HighlightSpan =>
        Array.isArray(span) &&
        span.length === 2 &&
        typeof span[0] === 'number' &&
        typeof span[1] === 'number' &&
        span[0] >= 0 &&
        span[1] > span[0] &&
        span[1] <= totalPoints,
    )
    .sort((a, b) => a[0] - b[0]);

  if (validSpans.length === 0) {
    return snippet;
  }

  const nodes: React.ReactNode[] = [];
  let cursor = 0;

  for (let i = 0; i < validSpans.length; i += 1) {
    const [rawStart, end] = validSpans[i];
    const start = Math.max(cursor, rawStart);
    if (start >= end) continue;
    if (start > cursor) {
      nodes.push(slicePoints(cursor, start));
    }
    nodes.push(
      <mark
        key={`hl-${i}-${start}`}
        data-testid="search-highlight"
        className="rounded-xs px-0.5 font-medium"
        style={{
          backgroundColor: 'var(--color-accent-soft)',
          color: 'var(--color-accent-primary)',
        }}
      >
        {slicePoints(start, end)}
      </mark>,
    );
    cursor = end;
  }

  if (cursor < totalPoints) {
    nodes.push(slicePoints(cursor, totalPoints));
  }

  return nodes;
}

export function GlobalSearchModal({ open, onClose }: GlobalSearchModalProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const locale = useLocale();
  const now = useNow(60_000, open);
  const homeWorkspaceId = useHomeWorkspaceId();
  const titleId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxRef = useRef<HTMLDivElement>(null);

  const [rawQuery, setRawQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);

  const trimmedQuery = rawQuery.trim();
  const isQueryValid = trimmedQuery.length >= 2;

  useEffect(() => {
    if (!open) return;
    const raf = window.requestAnimationFrame(() => {
      inputRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(raf);
  }, [open]);

  useEffect(() => {
    if (!isQueryValid) {
      setDebouncedQuery('');
      return;
    }
    const timer = window.setTimeout(() => {
      setDebouncedQuery(trimmedQuery);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [trimmedQuery, isQueryValid]);

  const { data, isFetching, isError } = useQuery({
    queryKey: queryKeys.dalton.search(debouncedQuery, 20),
    queryFn: ({ signal }) => fetchGlobalSearch(debouncedQuery, 20, signal),
    enabled: open && debouncedQuery.length >= 2,
    staleTime: 30_000,
  });

  const conversations = useMemo(
    () => (debouncedQuery.length >= 2 ? (data?.conversations ?? []) : []),
    [data?.conversations, debouncedQuery],
  );
  const documents = useMemo(
    () => (debouncedQuery.length >= 2 ? (data?.documents ?? []) : []),
    [data?.documents, debouncedQuery],
  );

  const flatItems = useMemo<NavigableSearchResult[]>(() => {
    const list: NavigableSearchResult[] = [];
    for (const item of conversations) {
      list.push({ section: 'conversations', item });
    }
    for (const item of documents) {
      list.push({ section: 'documents', item });
    }
    return list;
  }, [conversations, documents]);

  useEffect(() => {
    setActiveIndex(0);
  }, [debouncedQuery, conversations.length, documents.length]);

  useEffect(() => {
    const container = listboxRef.current;
    if (!container) return;
    const activeEl = container.querySelector<HTMLElement>('[data-active="true"]');
    if (activeEl && typeof activeEl.scrollIntoView === 'function') {
      activeEl.scrollIntoView({ block: 'nearest' });
    }
  }, [activeIndex]);

  const handleSelectResult = useCallback(
    (entry: NavigableSearchResult) => {
      onClose();
      if (entry.section === 'conversations') {
        navigate(`/chat/t/${entry.item.thread_id}`);
        return;
      }
      const doc = entry.item;
      if (doc.kind === 'memo') {
        const memoPath = doc.memo_key
          ? `${MEMO_USER_DIR}/${doc.memo_key}`
          : doc.path;
        const knownWorkspaceId = doc.workspace_id || homeWorkspaceId;
        if (knownWorkspaceId) {
          navigate(`/chat/${knownWorkspaceId}`, {
            state: { openFilePath: memoPath, workspaceId: knownWorkspaceId },
          });
        } else {
          void resolveChatWorkspace(queryClient, 'all').then((resolvedId) => {
            if (resolvedId) {
              navigate(`/chat/${resolvedId}`, {
                state: { openFilePath: memoPath, workspaceId: resolvedId },
              });
            }
          });
        }
        return;
      }
      if (doc.workspace_id) {
        navigate(`/chat/${doc.workspace_id}`, {
          state: { openFilePath: doc.path, workspaceId: doc.workspace_id },
        });
      }
    },
    [homeWorkspaceId, navigate, onClose, queryClient],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) {
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (flatItems.length > 0) {
        setActiveIndex((prev) => (prev + 1) % flatItems.length);
      }
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (flatItems.length > 0) {
        setActiveIndex((prev) => (prev - 1 + flatItems.length) % flatItems.length);
      }
      return;
    }
    if (e.key === 'Enter') {
      const target = flatItems[activeIndex];
      if (target) {
        e.preventDefault();
        handleSelectResult(target);
      }
    }
  };

  if (!open) return null;

  const isSearching = isQueryValid && (debouncedQuery !== trimmedQuery || isFetching);
  const showResults = debouncedQuery.length >= 2 && !isError;
  const hasAnyResults = conversations.length > 0 || documents.length > 0;

  const searchHeader = (
    <div className="pr-8">
      <h3 id={titleId} className="sr-only">
        {t('sidebar.globalSearch.title')}
      </h3>
      <div
        className="flex items-center gap-2.5 px-3 py-2 rounded-md"
        style={{
          backgroundColor: 'var(--color-bg-input)',
          border: '1px solid var(--color-border-default)',
        }}
      >
        <Search
          className="h-4 w-4 shrink-0"
          style={{ color: 'var(--color-text-tertiary)' }}
          aria-hidden="true"
        />
        <input
          ref={inputRef}
          type="text"
          value={rawQuery}
          onChange={(e) => setRawQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={t('sidebar.globalSearch.placeholder')}
          aria-label={t('sidebar.globalSearch.placeholder')}
          autoFocus
          className="w-full bg-transparent text-sm outline-none"
          style={{ color: 'var(--color-text-primary)' }}
        />
        {rawQuery.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setRawQuery('');
              setDebouncedQuery('');
              setActiveIndex(0);
              inputRef.current?.focus();
            }}
            aria-label={t('sidebar.globalSearch.clear')}
            className="p-0.5 rounded transition-colors hover:bg-foreground/10 shrink-0"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );

  return (
    <ModalShell
      labelId={titleId}
      header={searchHeader}
      onClose={onClose}
      width="wide"
      density="form"
    >
      <div className="flex flex-col gap-4 min-h-[220px]" onKeyDown={handleKeyDown}>
        {!isQueryValid && (
          <div className="flex-1 flex items-center justify-center py-10 text-center">
            <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('sidebar.globalSearch.idleHint')}
            </p>
          </div>
        )}

        {isQueryValid && isSearching && !data && (
          <div
            data-testid="global-search-loading"
            className="flex-1 flex items-center justify-center gap-2 py-10"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            <Loader size={16} />
            <span className="text-sm">{t('common.loading')}</span>
          </div>
        )}

        {isQueryValid && isError && (
          <div className="flex-1 flex items-center justify-center py-10 text-center">
            <p className="text-sm" role="alert" style={{ color: 'var(--color-loss)' }}>
              {t('sidebar.globalSearch.error')}
            </p>
          </div>
        )}

        {showResults && !isSearching && !hasAnyResults && (
          <div className="flex-1 flex items-center justify-center py-10 text-center">
            <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('sidebar.globalSearch.noResults')}
            </p>
          </div>
        )}

        {showResults && (hasAnyResults || data) && (
          <div
            ref={listboxRef}
            className="flex flex-col gap-4"
            role="listbox"
            aria-label={t('sidebar.globalSearch.title')}
          >
            {/* Upper section: Conversations */}
            <section data-testid="search-section-conversations" className="flex flex-col gap-1">
              <div className="flex items-center justify-between px-2 py-1">
                <span
                  className="text-xs font-semibold uppercase tracking-wider"
                  style={{ color: 'var(--color-text-tertiary)' }}
                >
                  {t('sidebar.globalSearch.conversations')}
                </span>
                <span
                  className="text-xs tabular-nums"
                  style={{ color: 'var(--color-text-tertiary)' }}
                >
                  {conversations.length}
                </span>
              </div>

              {conversations.map((conv, idx) => {
                const flatIdx = idx;
                const isActive = activeIndex === flatIdx;
                const relTime = relativeTime(conv.timestamp, locale, now);
                return (
                  <button
                    key={`conv-${conv.thread_id}`}
                    type="button"
                    role="option"
                    aria-selected={isActive}
                    data-active={isActive ? 'true' : 'false'}
                    onMouseEnter={() => setActiveIndex(flatIdx)}
                    onClick={() => handleSelectResult({ section: 'conversations', item: conv })}
                    className="w-full text-left px-3 py-2 rounded-md transition-colors flex flex-col gap-1"
                    style={{
                      backgroundColor: isActive ? 'var(--color-bg-hover)' : 'transparent',
                    }}
                  >
                    <div className="flex items-center justify-between gap-2 min-w-0">
                      <div className="flex items-center gap-2 min-w-0">
                        <MessageSquare
                          className="h-3.5 w-3.5 shrink-0"
                          style={{ color: 'var(--color-text-tertiary)' }}
                          aria-hidden="true"
                        />
                        <span
                          className="text-sm font-medium truncate"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          {conv.title || t('nav.untitledThread')}
                        </span>
                        {conv.workspace_name && (
                          <span
                            className="text-xs px-1.5 py-0.5 rounded shrink-0 truncate max-w-[160px]"
                            style={{
                              backgroundColor: 'var(--color-bg-subtle)',
                              color: 'var(--color-text-secondary)',
                            }}
                          >
                            {conv.workspace_name}
                          </span>
                        )}
                      </div>
                      {relTime && (
                        <span
                          className="text-xs shrink-0 tabular-nums"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          {relTime}
                        </span>
                      )}
                    </div>
                    {conv.snippet && (
                      <p
                        className="text-xs line-clamp-2 pl-5.5"
                        style={{ color: 'var(--color-text-secondary)' }}
                      >
                        {renderHighlightedSnippet(conv.snippet, conv.highlights)}
                      </p>
                    )}
                  </button>
                );
              })}
            </section>

            <div
              className="h-px w-full"
              style={{ backgroundColor: 'var(--color-border-muted)' }}
              aria-hidden="true"
            />

            {/* Lower section: Documents */}
            <section data-testid="search-section-documents" className="flex flex-col gap-1">
              <div className="flex items-center justify-between px-2 py-1">
                <span
                  className="text-xs font-semibold uppercase tracking-wider"
                  style={{ color: 'var(--color-text-tertiary)' }}
                >
                  {t('sidebar.globalSearch.documents')}
                </span>
                <span
                  className="text-xs tabular-nums"
                  style={{ color: 'var(--color-text-tertiary)' }}
                >
                  {documents.length}
                </span>
              </div>

              {documents.map((doc, idx) => {
                const flatIdx = conversations.length + idx;
                const isActive = activeIndex === flatIdx;
                const relTime = relativeTime(doc.timestamp, locale, now);
                const isMemo = doc.kind === 'memo';
                const badgeLabel = isMemo
                  ? t('sidebar.globalSearch.memoTag')
                  : doc.workspace_name;
                return (
                  <button
                    key={`doc-${doc.kind}-${doc.workspace_id ?? 'user'}-${doc.path}`}
                    type="button"
                    role="option"
                    aria-selected={isActive}
                    data-active={isActive ? 'true' : 'false'}
                    onMouseEnter={() => setActiveIndex(flatIdx)}
                    onClick={() => handleSelectResult({ section: 'documents', item: doc })}
                    className="w-full text-left px-3 py-2 rounded-md transition-colors flex flex-col gap-1"
                    style={{
                      backgroundColor: isActive ? 'var(--color-bg-hover)' : 'transparent',
                    }}
                  >
                    <div className="flex items-center justify-between gap-2 min-w-0">
                      <div className="flex items-center gap-2 min-w-0">
                        {isMemo ? (
                          <StickyNote
                            className="h-3.5 w-3.5 shrink-0"
                            style={{ color: 'var(--color-text-tertiary)' }}
                            aria-hidden="true"
                          />
                        ) : (
                          <FileText
                            className="h-3.5 w-3.5 shrink-0"
                            style={{ color: 'var(--color-text-tertiary)' }}
                            aria-hidden="true"
                          />
                        )}
                        <span
                          className="text-sm font-medium truncate"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          {doc.title}
                        </span>
                        {badgeLabel && (
                          <span
                            className="text-xs px-1.5 py-0.5 rounded shrink-0 truncate max-w-[160px]"
                            style={{
                              backgroundColor: 'var(--color-bg-subtle)',
                              color: 'var(--color-text-secondary)',
                            }}
                          >
                            {badgeLabel}
                          </span>
                        )}
                      </div>
                      {relTime && (
                        <span
                          className="text-xs shrink-0 tabular-nums"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          {relTime}
                        </span>
                      )}
                    </div>
                    {doc.snippet && (
                      <p
                        className="text-xs line-clamp-2 pl-5.5"
                        style={{ color: 'var(--color-text-secondary)' }}
                      >
                        {renderHighlightedSnippet(doc.snippet, doc.highlights)}
                      </p>
                    )}
                  </button>
                );
              })}
            </section>
          </div>
        )}
      </div>
    </ModalShell>
  );
}

export default GlobalSearchModal;
