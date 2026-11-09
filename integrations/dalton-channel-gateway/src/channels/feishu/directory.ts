/**
 * Feishu group directory listing (`im.chat.list`), chat info (`im.chat.get`),
 * and chat membership verification (`im.chatMembers.get`) with pagination
 * loop protection (`seenPageTokens`).
 *
 * Adapted from:
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/channel.runtime-CC15v8AP.mjs` (lines 91–183)
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/chat-fC3dvxFs.mjs` (lines 46–101)
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 7 (`src/channels/feishu/directory.ts`).
 */

import type * as Lark from "@larksuiteoapi/node-sdk";

export const MAX_FEISHU_DIRECTORY_PAGES = 100;

export interface FeishuDirectoryGroup {
  kind: "group";
  id: string;
  name?: string;
}

export interface FeishuDirectoryGroupListResult {
  groups: FeishuDirectoryGroup[];
  complete: boolean;
  error: string | null;
}

export interface FeishuDirectoryPeer {
  kind: "user";
  id: string;
  name?: string;
}

export interface FeishuChatInfo {
  chatId: string;
  name?: string;
  description?: string;
  ownerId?: string;
  chatMode?: string;
  chatType?: string;
  userCount?: number;
}

export interface FeishuChatMembersPage {
  hasMore: boolean;
  pageToken?: string;
  memberTotal?: number;
  members: Array<{
    memberId: string;
    name?: string;
    memberIdType?: string;
  }>;
}

/**
 * Lists groups the Feishu bot is in via `client.im.chat.list`, guarding
 * pagination with `seenPageTokens` and `MAX_FEISHU_DIRECTORY_PAGES`,
 * and reporting whether the listing is `complete` or was truncated/aborted.
 */
export async function listFeishuDirectoryGroupsDetailed(
  client: Lark.Client,
  params: {
    query?: string;
    limit?: number;
    pageSize?: number;
    maxPages?: number;
  } = {},
): Promise<FeishuDirectoryGroupListResult> {
  const limit = Math.max(1, params.limit ?? 200);
  const maxPages = Math.max(1, params.maxPages ?? MAX_FEISHU_DIRECTORY_PAGES);
  const q = params.query?.trim().toLowerCase() || "";
  const pageSize = Math.min(Math.max(1, params.pageSize ?? Math.min(limit, 100)), 100);
  const groups: FeishuDirectoryGroup[] = [];
  const seenGroupIds = new Set<string>();
  const seenPageTokens = new Set<string>();
  let pageToken: string | undefined;
  let pagesFetched = 0;

  while (groups.length < limit) {
    if (pagesFetched >= maxPages) {
      return {
        groups,
        complete: false,
        error: `Feishu im.chat.list exceeded maximum pagination limit (${maxPages} pages)`,
      };
    }

    const response = (await client.im.chat.list({
      params: {
        page_size: pageSize,
        ...(pageToken ? { page_token: pageToken } : {}),
      },
    })) as {
      code?: number;
      msg?: string;
      data?: {
        items?: Array<{ chat_id?: string; name?: string }>;
        has_more?: boolean;
        page_token?: string;
      };
    };
    pagesFetched += 1;

    if (response.code !== undefined && response.code !== 0) {
      throw new Error(
        response.msg || `Feishu im.chat.list failed (code ${response.code})`,
      );
    }

    const items = response.data?.items ?? [];
    let truncatedInPage = false;
    for (let idx = 0; idx < items.length; idx += 1) {
      const chat = items[idx]!;
      const chatId = chat.chat_id?.trim();
      if (!chatId || seenGroupIds.has(chatId)) {
        continue;
      }
      const name = chat.name?.trim() || undefined;
      if (
        !q ||
        chatId.toLowerCase().includes(q) ||
        (name?.toLowerCase().includes(q) ?? false)
      ) {
        if (groups.length >= limit) {
          truncatedInPage = true;
          break;
        }
        seenGroupIds.add(chatId);
        groups.push({
          kind: "group",
          id: chatId,
          name,
        });
      }
    }

    const hasMore = Boolean(response.data?.has_more);
    if (truncatedInPage || (groups.length >= limit && hasMore)) {
      return {
        groups,
        complete: false,
        error: null,
      };
    }

    if (!hasMore) {
      return {
        groups,
        complete: true,
        error: null,
      };
    }

    const nextPageToken = response.data?.page_token?.trim();
    if (!nextPageToken) {
      return {
        groups,
        complete: false,
        error: "Feishu im.chat.list returned has_more=true without a page_token",
      };
    }
    if (seenPageTokens.has(nextPageToken)) {
      return {
        groups,
        complete: false,
        error: `Feishu im.chat.list returned repeated page_token: ${nextPageToken}`,
      };
    }
    seenPageTokens.add(nextPageToken);
    pageToken = nextPageToken;
  }

  return {
    groups,
    complete: true,
    error: null,
  };
}

export async function listFeishuDirectoryGroupsLive(
  client: Lark.Client,
  params: {
    query?: string;
    limit?: number;
    pageSize?: number;
    maxPages?: number;
  } = {},
): Promise<FeishuDirectoryGroup[]> {
  const result = await listFeishuDirectoryGroupsDetailed(client, params);
  return result.groups;
}

export async function listFeishuDirectoryPeersLive(
  client: Lark.Client,
  params: {
    query?: string;
    limit?: number;
    pageSize?: number;
  } = {},
): Promise<FeishuDirectoryPeer[]> {
  const limit = Math.max(1, params.limit ?? 200);
  const q = params.query?.trim().toLowerCase() || "";
  const pageSize = Math.min(Math.max(1, params.pageSize ?? Math.min(limit, 50)), 50);
  const peers: FeishuDirectoryPeer[] = [];
  const seenPeerIds = new Set<string>();
  const seenPageTokens = new Set<string>();
  let pageToken: string | undefined;
  let pagesFetched = 0;

  while (peers.length < limit && pagesFetched < MAX_FEISHU_DIRECTORY_PAGES) {
    pagesFetched += 1;
    const response = (await client.contact.user.list({
      params: {
        page_size: pageSize,
        ...(pageToken ? { page_token: pageToken } : {}),
      },
    })) as {
      code?: number;
      msg?: string;
      data?: {
        items?: Array<{ open_id?: string; name?: string }>;
        has_more?: boolean;
        page_token?: string;
      };
    };

    if (response.code !== undefined && response.code !== 0) {
      throw new Error(
        response.msg ||
          `Feishu contact.user.list failed (code ${response.code})`,
      );
    }

    const items = response.data?.items ?? [];
    for (const user of items) {
      const openId = user.open_id?.trim();
      if (!openId || seenPeerIds.has(openId)) {
        continue;
      }
      const name = user.name?.trim() || undefined;
      if (
        !q ||
        openId.toLowerCase().includes(q) ||
        (name?.toLowerCase().includes(q) ?? false)
      ) {
        seenPeerIds.add(openId);
        peers.push({
          kind: "user",
          id: openId,
          name,
        });
      }
      if (peers.length >= limit) {
        break;
      }
    }

    if (!response.data?.has_more || peers.length >= limit) {
      break;
    }
    const nextPageToken = response.data.page_token?.trim();
    if (!nextPageToken || seenPageTokens.has(nextPageToken)) {
      break;
    }
    seenPageTokens.add(nextPageToken);
    pageToken = nextPageToken;
  }

  return peers;
}

export async function getChatInfo(
  client: Lark.Client,
  chatId: string,
): Promise<FeishuChatInfo> {
  const res = (await client.im.chat.get({
    path: { chat_id: chatId },
  })) as {
    code?: number;
    msg?: string;
    data?: {
      name?: string;
      description?: string;
      owner_id?: string;
      chat_mode?: string;
      chat_type?: string;
      user_count?: string | number;
    };
  };

  if (res.code !== undefined && res.code !== 0) {
    throw new Error(
      res.msg || `Feishu getChatInfo failed (code ${res.code})`,
    );
  }

  const data = res.data;
  return {
    chatId,
    name: data?.name,
    description: data?.description,
    ownerId: data?.owner_id,
    chatMode: data?.chat_mode,
    chatType: data?.chat_type,
    userCount:
      typeof data?.user_count === "number"
        ? data.user_count
        : typeof data?.user_count === "string"
          ? Number.parseInt(data.user_count, 10)
          : undefined,
  };
}

export async function getChatMembers(
  client: Lark.Client,
  chatId: string,
  pageSize = 50,
  pageToken?: string,
  memberIdType: "open_id" | "user_id" | "union_id" = "open_id",
): Promise<FeishuChatMembersPage> {
  const res = (await client.im.chatMembers.get({
    path: { chat_id: chatId },
    params: {
      page_size: Math.min(Math.max(pageSize, 1), 100),
      member_id_type: memberIdType,
      ...(pageToken ? { page_token: pageToken } : {}),
    },
  })) as {
    code?: number;
    msg?: string;
    data?: {
      has_more?: boolean;
      page_token?: string;
      member_total?: number;
      items?: Array<{
        member_id?: string;
        name?: string;
        member_id_type?: string;
      }>;
    };
  };

  if (res.code !== undefined && res.code !== 0) {
    throw new Error(
      res.msg || `Feishu getChatMembers failed (code ${res.code})`,
    );
  }

  return {
    hasMore: Boolean(res.data?.has_more),
    pageToken: res.data?.page_token,
    memberTotal: res.data?.member_total,
    members:
      res.data?.items?.map((item) => ({
        memberId: item.member_id ?? "",
        name: item.name,
        memberIdType: item.member_id_type,
      })) ?? [],
  };
}

export async function isFeishuChatMember(
  client: Lark.Client,
  chatId: string,
  senderOpenId: string,
): Promise<boolean> {
  const targetOpenId = senderOpenId.trim();
  if (!targetOpenId) {
    return false;
  }

  let pageToken: string | undefined;
  const seenPageTokens = new Set<string>();
  let pagesFetched = 0;

  while (pagesFetched < MAX_FEISHU_DIRECTORY_PAGES) {
    pagesFetched += 1;
    const res = await getChatMembers(client, chatId, 100, pageToken, "open_id");
    if (res.members.some((item) => item.memberId?.trim() === targetOpenId)) {
      return true;
    }
    if (!res.hasMore) {
      return false;
    }
    const nextToken = res.pageToken?.trim();
    if (!nextToken || seenPageTokens.has(nextToken)) {
      throw new Error(
        `Unable to verify membership for user ${senderOpenId} in chat ${chatId}: member list pagination incomplete`,
      );
    }
    seenPageTokens.add(nextToken);
    pageToken = nextToken;
  }
  throw new Error(
    `Unable to verify membership for user ${senderOpenId} in chat ${chatId}: member list pagination incomplete`,
  );
}

export async function assertFeishuChatMember(
  client: Lark.Client,
  chatId: string,
  senderOpenId: string,
): Promise<void> {
  const isMember = await isFeishuChatMember(client, chatId, senderOpenId);
  if (!isMember) {
    throw new Error(
      `Permission denied: user ${senderOpenId} is not a member of chat ${chatId}`,
    );
  }
}
