import { api } from '@/api/client';

export type HighlightSpan = [number, number];

export interface GlobalSearchConversationItem {
  kind: 'conversation';
  thread_id: string;
  workspace_id: string;
  title: string;
  workspace_name: string;
  timestamp: string | null;
  snippet: string;
  highlights: HighlightSpan[];
}

export interface GlobalSearchDocumentItem {
  kind: 'workspace_file' | 'memo';
  memo_key: string | null;
  workspace_id: string | null;
  path: string;
  title: string;
  workspace_name: string;
  timestamp: string | null;
  snippet: string;
  highlights: HighlightSpan[];
}

export interface GlobalSearchResponse {
  conversations: GlobalSearchConversationItem[];
  documents: GlobalSearchDocumentItem[];
}

export async function fetchGlobalSearch(
  q: string,
  limit = 20,
  signal?: AbortSignal,
): Promise<GlobalSearchResponse> {
  const { data } = await api.get<GlobalSearchResponse>('/api/v1/dalton/search', {
    params: { q, limit },
    signal,
  });
  return {
    conversations: Array.isArray(data?.conversations) ? data.conversations : [],
    documents: Array.isArray(data?.documents) ? data.documents : [],
  };
}
