import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Send } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonFeishuGroups,
  sendDaltonNotifyTest,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

interface GroupTestState {
  sending: boolean;
  ok: boolean | null;
  detail: string | null;
}

/** Shared Feishu team group chats listing for Team Channels & Ops, including live test push per group. */
export function DaltonFeishuGroupsCard() {
  const { t } = useTranslation();
  const [testStates, setTestStates] = useState<Record<string, GroupTestState>>({});

  const groupsQuery = useQuery({
    queryKey: queryKeys.dalton.feishuGroups(),
    queryFn: fetchDaltonFeishuGroups,
    retry: false,
  });

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
      const detail = err instanceof Error && err.message ? err.message : null;
      setTestStates((prev) => ({
        ...prev,
        [chatId]: { sending: false, ok: false, detail },
      }));
    }
  };

  return (
    <DaltonSettingsCard
      id="dalton-feishu-groups"
      testId="dalton-feishu-groups-card"
      title={t('dalton.feishuGroups.title')}
      description={t('dalton.feishuGroups.description')}
      layout="padded"
      action={
        <DaltonRefreshButton
          busy={groupsQuery.isFetching}
          onClick={() => void groupsQuery.refetch()}
          testId="dalton-feishu-groups-refresh"
        />
      }
    >
      {groupsQuery.isLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : groupsQuery.isError ? (
        <DaltonInlineNotice tone="error">{t('dalton.feishuGroups.loadFailed')}</DaltonInlineNotice>
      ) : (
        <DaltonKeyValueList
          isEmpty={groups.length === 0}
          empty={t('dalton.feishuGroups.empty')}
        >
          {groups.map((group) => {
            const test = testStates[group.chat_id];
            const target = `feishu:chat:${group.chat_id}`;
            return (
              <DaltonItemRow
                key={group.chat_id}
                testId={`dalton-feishu-group-row-${group.chat_id}`}
                title={group.name || t('dalton.feishuGroups.unnamedGroup')}
                meta={
                  <span className="font-mono">
                    {t('dalton.feishuGroups.chatIdLabel')}: {group.chat_id}
                  </span>
                }
                trailing={
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-8 gap-1 text-xs"
                      disabled={test?.sending === true}
                      aria-label={`${t('dalton.notifyTargets.testButton')} ${group.name || group.chat_id}`}
                      onClick={() => void handleSendTest(group.chat_id)}
                      data-testid={`dalton-feishu-group-test-${group.chat_id}`}
                    >
                      <Send className="h-3.5 w-3.5" />
                      <span>
                        {test?.sending === true
                          ? t('dalton.notifyTargets.testing')
                          : t('dalton.notifyTargets.testButton')}
                      </span>
                    </Button>
                    <DaltonStatusBadge
                      dot
                      tone="success"
                      label={t('dalton.feishuGroups.memberBadge')}
                    />
                  </div>
                }
              >
                {test?.ok === true && (
                  <DaltonInlineNotice
                    tone="success"
                    testId={`dalton-feishu-group-test-ok-${group.chat_id}`}
                  >
                    {t('dalton.notifyTargets.testSuccess', { target })}
                  </DaltonInlineNotice>
                )}
                {test?.ok === false && (
                  <DaltonInlineNotice
                    tone="error"
                    testId={`dalton-feishu-group-test-err-${group.chat_id}`}
                  >
                    {test.detail || t('dalton.notifyTargets.testFailed')}
                  </DaltonInlineNotice>
                )}
              </DaltonItemRow>
            );
          })}
        </DaltonKeyValueList>
      )}
    </DaltonSettingsCard>
  );
}
