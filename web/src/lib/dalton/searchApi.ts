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
  content_timed_out?: boolean;
}

export interface GlobalSearchOptions {
  includeConversations?: boolean;
  workspaceId?: string;
  includeContent?: boolean;
}

export async function fetchGlobalSearch(
  q: string,
  limit = 20,
  signal?: AbortSignal,
  options?: GlobalSearchOptions,
): Promise<GlobalSearchResponse> {
  const params: Record<string, string | number | boolean> = { q, limit };
  if (options?.includeConversations !== undefined) {
    params.include_conversations = options.includeConversations;
  }
  if (options?.workspaceId) {
    params.workspace_id = options.workspaceId;
  }
  if (options?.includeContent !== undefined) {
    params.include_content = options.includeContent;
  }
  const { data } = await api.get<GlobalSearchResponse>('/api/v1/dalton/search', {
    params,
    signal,
  });
  return {
    conversations: Array.isArray(data?.conversations) ? data.conversations : [],
    documents: Array.isArray(data?.documents) ? data.documents : [],
    ...(data?.content_timed_out ? { content_timed_out: true } : {}),
  };
}
