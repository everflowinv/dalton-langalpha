import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonFeishuBotName,
  fetchDaltonFeishuGroups,
  sendDaltonNotifyTest,
} from '@/pages/Settings/utils/daltonApi';

interface GroupTestState {
  sending: boolean;
  ok: boolean | null;
  detail: string | null;
}

/** Internal/admin group push. No auth context is read: the group list endpoint is
 *  itself the tier gate — 403 means "external member" and the card stays away; any
 *  other failure still shows the card (with its refresh) to internal/admin members. */
export function DaltonFeishuGroupsCard(): React.ReactElement | null {
  const [testStates, setTestStates] = useState<Record<string, GroupTestState>>({});

  const groupsQuery = useQuery({
    queryKey: queryKeys.dalton.feishuGroups(),
    queryFn: fetchDaltonFeishuGroups,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const botQuery = useQuery({
    queryKey: queryKeys.dalton.feishuBotName(),
    queryFn: fetchDaltonFeishuBotName,
    // Only an admin gets this far (the group list above refused 403 otherwise);
    // the name exists only inside this card, so it is fetched once the card is.
    enabled: groupsQuery.isSuccess,
    staleTime: 10 * 60_000,
    retry: false,
  });

  if (groupsQuery.isPending) {
    return null;
  }
  const refusalStatus = (groupsQuery.error as ApiError | null)?.response?.status;
  if (groupsQuery.isError && (refusalStatus === 403 || refusalStatus === 401)) {
    // Not an admin: no card at all.
    return null;
  }

  const botName = botQuery.data?.ok === true ? botQuery.data.app_name : null;
  const groups = groupsQuery.data?.groups ?? [];

  const handleSendTest = async (chatId: string) => {
    setTestStates((prev) => ({
      ...prev,
      [chatId]: { sending: true, ok: null, detail: null },
    }));
    try {
      const res = await sendDaltonNotifyTest(`feishu:chat:${chatId}`);
      setTestStates((prev) => ({
        ...prev,
        [chatId]: { sending: false, ok: res.ok, detail: res.detail },
      }));
    } catch (err: unknown) {
      const msg = err instanceof Error && err.message ? err.message : '发送测试失败';
      setTestStates((prev) => ({
        ...prev,
        [chatId]: { sending: false, ok: false, detail: msg },
      }));
    }
  };

  return (
    <div
      className="flex flex-col gap-3 rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-feishu-groups-card"
    >
      <div className="flex items-start justify-between gap-4 border-b pb-3" style={{ borderColor: 'var(--color-border-muted)' }}>
        <div className="flex flex-col gap-1">
          <span
            className="text-sm font-semibold"
            style={{ color: 'var(--color-text-primary)' }}
          >
            飞书群推送
          </span>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {botName
              ? `以下是 OpenClaw 机器人「${botName}」已加入的群，系统可向这些群推送；在自动化列表里为每个任务选择要推送的群`
              : '以下是 OpenClaw 机器人已加入的群，系统可向这些群推送；在自动化列表里为每个任务选择要推送的群'}
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="shrink-0"
          aria-label="刷新群列表"
          disabled={groupsQuery.isFetching}
          onClick={() => void groupsQuery.refetch()}
        >
          <RefreshCw className={groupsQuery.isFetching ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
          刷新
        </Button>
      </div>

      {groupsQuery.isError ? (
        <p role="alert" className="text-xs" style={{ color: 'var(--color-loss)' }}>
          群列表暂时不可用，请稍后点击刷新重试
        </p>
      ) : groups.length > 0 ? (
        <ul className="flex flex-col gap-1.5 text-xs" data-testid="dalton-feishu-group-list">
          {groups.map((group) => {
            const test = testStates[group.chat_id];
            return (
              <li
                key={group.chat_id}
                className="flex flex-col gap-1 rounded px-2.5 py-1.5"
                style={{ backgroundColor: 'var(--color-bg-tag)' }}
                data-testid={`dalton-feishu-group-row-${group.chat_id}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                    {group.name || '未命名群'}
                  </span>
                  <button
                    type="button"
                    onClick={() => void handleSendTest(group.chat_id)}
                    disabled={test?.sending === true}
                    aria-label={`发送测试 ${group.name || group.chat_id}`}
                    className="shrink-0 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                    style={{ color: 'var(--color-text-secondary)' }}
                  >
                    {test?.sending === true ? '发送中…' : '发送测试'}
                  </button>
                </div>
                <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                  feishu:chat:{group.chat_id}
                </span>
                {test?.ok === true && (
                  <p className="text-[11px]" style={{ color: 'var(--color-profit)' }}>
                    测试已发送
                  </p>
                )}
                {test?.ok === false && (
                  <p role="alert" className="text-[11px]" style={{ color: 'var(--color-loss)' }}>
                    {test.detail ?? '发送失败'}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {groupsQuery.isFetching ? '正在获取群列表…' : '机器人尚未加入任何群（把机器人拉进群后点击刷新）'}
        </p>
      )}
    </div>
  );
}
