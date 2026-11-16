import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Check, Copy } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/hooks/useLocale';
import { copyText } from '@/lib/clipboard';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  createDaltonChannelBindCode,
  fetchDaltonChannelBindings,
  unbindDaltonChannel,
  type DaltonChannelBindCodeCreateResponse,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonActionConfirmButton,
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from './primitives';

/** Personal WeChat account binding card for Messaging & Channels. */
export function DaltonWeixinBindCard() {
  const { t } = useTranslation();
  const locale = useLocale();
  const queryClient = useQueryClient();

  const [bindCode, setBindCode] = useState<DaltonChannelBindCodeCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [unlinkedNotice, setUnlinkedNotice] = useState(false);

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    retry: false,
  });

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const codeMutation = useMutation({
    mutationFn: () => createDaltonChannelBindCode('weixin'),
    onSuccess: (created) => {
      setBindCode(created);
      setUnlinkedNotice(false);
      setCopyError(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
    },
  });

  const unbindMutation = useMutation({
    mutationFn: () => unbindDaltonChannel('weixin'),
    onSuccess: () => {
      setBindCode(null);
      setUnlinkedNotice(true);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
    },
  });

  const handleCopy = async (command: string) => {
    try {
      await copyText(command);
      setCopied(true);
      setCopyError(null);
    } catch {
      setCopyError(t('dalton.weixinBind.copyFailed'));
    }
  };

  const state = bindingsQuery.data?.bindings?.weixin;
  const bot = bindingsQuery.data?.weixin_bot;
  const botStatus =
    bot == null
      ? 'unknown'
      : !bot.configured || bot.status === 'unconfigured'
        ? 'unconfigured'
        : bot.status === 'active'
          ? 'active'
          : 'expired';
  const linked = state?.linked === true;
  const accountLabel = state?.platform_username || state?.platform_user_id;

  return (
    <DaltonSettingsCard
      id="dalton-weixin-bind"
      testId="dalton-weixin-bind-card"
      title={t('dalton.weixinBind.title')}
      description={t('dalton.weixinBind.description')}
      layout="padded"
      bodyClassName="space-y-3"
      action={
        <div className="flex items-center gap-2">
          {!bindingsQuery.isLoading && !bindingsQuery.isError && (
            <DaltonStatusBadge
              dot
              tone={linked ? 'success' : 'neutral'}
              label={
                linked
                  ? t('dalton.weixinBind.linkedBadge')
                  : t('dalton.weixinBind.unlinkedBadge')
              }
            />
          )}
          <DaltonRefreshButton
            busy={bindingsQuery.isFetching}
            onClick={() => void bindingsQuery.refetch()}
          />
        </div>
      }
    >
      {bindingsQuery.isLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : bindingsQuery.isError ? (
        <DaltonInlineNotice tone="error">{t('dalton.weixinBind.loadFailed')}</DaltonInlineNotice>
      ) : (
        <>
          {botStatus === 'unknown' && (
            <DaltonInlineNotice tone="info" testId="dalton-weixin-bot-unknown">
              {t('dalton.weixinBind.botUnknownHint')}
            </DaltonInlineNotice>
          )}
          {botStatus === 'unconfigured' && (
            <DaltonInlineNotice tone="warning" testId="dalton-weixin-bot-not-ready">
              {t('dalton.weixinBind.botNotReadyHint')}
            </DaltonInlineNotice>
          )}
          {botStatus === 'expired' && bot && (
            <DaltonInlineNotice tone="warning" testId="dalton-weixin-bot-expired">
              {t('dalton.weixinBind.botExpiredHint', { status: bot.status })}
            </DaltonInlineNotice>
          )}

          {linked && (
            <div
              className="flex flex-wrap items-center justify-between gap-3 rounded-md border px-3 py-2.5"
              style={{
                borderColor: 'var(--color-border-muted)',
                backgroundColor: 'var(--color-bg-elevated)',
              }}
            >
              <div className="space-y-0.5 text-xs">
                {accountLabel && (
                  <div style={{ color: 'var(--color-text-primary)' }} className="font-medium">
                    {t('dalton.weixinBind.linkedAccount', { account: accountLabel })}
                  </div>
                )}
                {state?.updated_at && (
                  <div style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('dalton.weixinBind.linkedAt', {
                      time: formatTimestamp(state.updated_at, locale),
                    })}
                  </div>
                )}
              </div>

              <DaltonActionConfirmButton
                label={
                  unbindMutation.isPending
                    ? t('dalton.weixinBind.unbinding')
                    : t('dalton.weixinBind.unbind')
                }
                confirmTitle={t('dalton.weixinBind.unbindTitle')}
                confirmMessage={t('dalton.weixinBind.unbindConfirm')}
                confirmLabel={t('dalton.weixinBind.unbind')}
                busy={unbindMutation.isPending}
                onConfirm={() => unbindMutation.mutateAsync().then(() => undefined)}
                testId="dalton-weixin-unbind"
              />
            </div>
          )}

          {unlinkedNotice && (
            <DaltonInlineNotice tone="success">{t('dalton.weixinBind.unlinkedSuccess')}</DaltonInlineNotice>
          )}
          {unbindMutation.isError && (
            <DaltonInlineNotice tone="error">{t('dalton.weixinBind.unbindFailed')}</DaltonInlineNotice>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 text-xs"
              disabled={codeMutation.isPending}
              onClick={() => codeMutation.mutate()}
              data-testid="dalton-weixin-generate-code"
            >
              {codeMutation.isPending
                ? t('dalton.weixinBind.generating')
                : t('dalton.weixinBind.generateCode')}
            </Button>
          </div>

          {codeMutation.isError && (
            <DaltonInlineNotice tone="error">{t('dalton.weixinBind.generateFailed')}</DaltonInlineNotice>
          )}

          {bindCode && (
            <div
              className="rounded-md border px-3 py-2.5 space-y-2"
              style={{
                borderColor: 'var(--color-border-muted)',
                backgroundColor: 'var(--color-bg-elevated)',
              }}
            >
              <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {t('dalton.weixinBind.commandHint')}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <code
                  className="text-xs font-mono select-all break-all"
                  style={{ color: 'var(--color-text-primary)' }}
                >
                  {bindCode.command}
                </code>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 gap-1.5 text-xs shrink-0"
                  onClick={() => void handleCopy(bindCode.command)}
                  data-testid="dalton-weixin-copy-command"
                >
                  {copied ? (
                    <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                  <span>
                    {copied ? t('dalton.weixinBind.copied') : t('dalton.weixinBind.copyCommand')}
                  </span>
                </Button>
              </div>
              {copyError && <DaltonInlineNotice tone="warning">{copyError}</DaltonInlineNotice>}
              <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('dalton.weixinBind.codeExpires', {
                  time: formatTimestamp(bindCode.expires_at, locale),
                })}
              </div>
            </div>
          )}
        </>
      )}
    </DaltonSettingsCard>
  );
}
