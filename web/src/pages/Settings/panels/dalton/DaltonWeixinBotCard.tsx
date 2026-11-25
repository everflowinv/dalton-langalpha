import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { QrCode } from 'lucide-react';
import { apiErrorStatus } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonChannelBindings,
  startDaltonWeixinQrLogin,
  verifyDaltonWeixinQrCode,
  waitDaltonWeixinQrLogin,
  type DaltonWeixinQrPublicResponse,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

function mergeQrState(
  prev: DaltonWeixinQrPublicResponse | null,
  next: DaltonWeixinQrPublicResponse,
): DaltonWeixinQrPublicResponse {
  return {
    ...prev,
    ...next,
    qrcode_url: next.qrcode_url ?? prev?.qrcode_url,
    qrcode_img_content: next.qrcode_img_content ?? prev?.qrcode_img_content,
  };
}

/** Admin-only team WeChat bot QR login card (split from DaltonWeixinChannelCard). */
export function DaltonWeixinBotCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { isAdmin } = useCallerIdentity();

  const [qrState, setQrState] = useState<DaltonWeixinQrPublicResponse | null>(null);
  const [verifyCode, setVerifyCode] = useState('');
  const [qrError, setQrError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const pollTokenRef = useRef(0);
  const pollTimerRef = useRef<number | null>(null);

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    retry: false,
  });

  const clearPollTimer = () => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      pollTokenRef.current += 1;
      clearPollTimer();
    };
  }, []);

  const pollQr = async (sessionKey: string, token: number, failureCount = 0) => {
    if (!mountedRef.current || pollTokenRef.current !== token) return;
    try {
      const next = await waitDaltonWeixinQrLogin(sessionKey);
      if (!mountedRef.current || pollTokenRef.current !== token) return;
      setQrState((prev) => mergeQrState(prev, next));
      if (next.status === 'confirmed') {
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
        return;
      }
      if (
        next.status === 'wait' ||
        next.status === 'scaned' ||
        next.status === 'scaned_but_redirect'
      ) {
        clearPollTimer();
        pollTimerRef.current = window.setTimeout(() => {
          pollTimerRef.current = null;
          if (mountedRef.current && pollTokenRef.current === token) {
            void pollQr(sessionKey, token, 0);
          }
        }, 1200);
      }
    } catch (err) {
      if (!mountedRef.current || pollTokenRef.current !== token) return;
      const status = apiErrorStatus(err);
      const nonRetryable = status === 401 || status === 403 || status === 404 || status === 409;
      if (nonRetryable || failureCount >= 5) {
        setQrError(t('dalton.weixinBot.loginFailed'));
        return;
      }
      const backoffMs = Math.min(1000 * 2 ** failureCount, 8000);
      clearPollTimer();
      pollTimerRef.current = window.setTimeout(() => {
        pollTimerRef.current = null;
        if (mountedRef.current && pollTokenRef.current === token) {
          void pollQr(sessionKey, token, failureCount + 1);
        }
      }, backoffMs);
    }
  };

  const startMutation = useMutation({
    mutationFn: startDaltonWeixinQrLogin,
    onSuccess: (resp) => {
      if (!mountedRef.current) return;
      clearPollTimer();
      pollTokenRef.current += 1;
      const token = pollTokenRef.current;
      setQrError(null);
      setVerifyCode('');
      setQrState(resp);
      if (
        resp.session_key &&
        (resp.status === 'wait' || resp.status === 'scaned' || resp.status === 'scaned_but_redirect')
      ) {
        void pollQr(resp.session_key, token);
      } else if (resp.status === 'confirmed') {
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      }
    },
    onError: () => {
      if (!mountedRef.current) return;
      setQrError(t('dalton.weixinBot.loginFailed'));
    },
  });

  const verifyMutation = useMutation({
    mutationFn: () =>
      verifyDaltonWeixinQrCode(qrState?.session_key ?? '', verifyCode.trim()),
    onSuccess: (resp) => {
      if (!mountedRef.current) return;
      setQrState((prev) => mergeQrState(prev, resp));
      if (resp.status === 'confirmed') {
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      } else if (resp.session_key && resp.status === 'wait') {
        clearPollTimer();
        pollTokenRef.current += 1;
        const token = pollTokenRef.current;
        void pollQr(resp.session_key, token);
      }
    },
    onError: () => {
      if (!mountedRef.current) return;
      setQrError(t('dalton.weixinBot.loginFailed'));
    },
  });

  const bot = bindingsQuery.data?.weixin_bot;
  const botConnected = Boolean(bot?.configured && bot.status === 'active');
  const botExpired = Boolean(
    bot?.configured && (bot.status === 'expired' || bot.status === 'qr_expired'),
  );
  const botErrored = Boolean(bot?.configured && !botConnected && !botExpired);
  const botAccount = bot?.ilink_username || bot?.ilink_user_id;

  return (
    <DaltonSettingsCard
      id="dalton-weixin-bot"
      testId="dalton-weixin-bot-card"
      title={t('dalton.weixinBot.title')}
      description={t('dalton.weixinBot.description')}
      layout="padded"
      bodyClassName="space-y-3"
      action={
        <div className="flex items-center gap-2">
          {!bindingsQuery.isLoading && !bindingsQuery.isError && (
            <DaltonStatusBadge
              dot
              testId="dalton-weixin-bot-status-badge"
              tone={
                botConnected
                  ? 'success'
                  : botExpired
                    ? 'warning'
                    : botErrored
                      ? 'danger'
                      : 'neutral'
              }
              label={
                botConnected
                  ? t('dalton.weixinBot.connectedBadge')
                  : botExpired
                    ? t('dalton.weixinBot.expiredBadge')
                    : botErrored
                      ? t('dalton.weixinBot.errorBadge')
                      : t('dalton.weixinBot.disconnectedBadge')
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
        <DaltonInlineNotice tone="error">{t('dalton.weixinBot.loadFailed')}</DaltonInlineNotice>
      ) : !isAdmin ? (
        <DaltonInlineNotice tone="info">{t('dalton.weixinBot.adminOnlyNotice')}</DaltonInlineNotice>
      ) : (
        <>
          {botConnected && botAccount && (
            <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('dalton.weixinBot.connectedAccount', { account: botAccount })}
            </div>
          )}
          {botExpired && (
            <DaltonInlineNotice tone="warning" testId="dalton-weixin-bot-expired-notice">
              {t('dalton.weixinBot.expiredNotice')}
            </DaltonInlineNotice>
          )}
          {botErrored && bot && (
            <DaltonInlineNotice tone="error" testId="dalton-weixin-bot-error-notice">
              {t('dalton.weixinBot.errorNotice', { status: bot.status })}
            </DaltonInlineNotice>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs"
              disabled={startMutation.isPending}
              onClick={() => startMutation.mutate()}
              data-testid="dalton-weixin-bot-start-qr"
            >
              <QrCode className="h-3.5 w-3.5" />
              <span>
                {startMutation.isPending
                  ? t('dalton.weixinBot.starting')
                  : qrState
                    ? t('dalton.weixinBot.refreshQr')
                    : t('dalton.weixinBot.startLogin')}
              </span>
            </Button>
          </div>

          {botConnected && (
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('dalton.weixinBot.logoutNote')}
            </p>
          )}

          {qrError && <DaltonInlineNotice tone="error">{qrError}</DaltonInlineNotice>}

          {qrState && (
            <div
              className="rounded-md border p-3 space-y-2.5"
              style={{
                borderColor: 'var(--color-border-muted)',
                backgroundColor: 'var(--color-bg-elevated)',
              }}
            >
              {qrState.status === 'confirmed' ? (
                <DaltonInlineNotice tone="success">
                  {t('dalton.weixinBot.connectedSuccess')}
                </DaltonInlineNotice>
              ) : qrState.status === 'expired' ? (
                <DaltonInlineNotice tone="warning">
                  {t('dalton.weixinBot.qrExpired')}
                </DaltonInlineNotice>
              ) : (
                <>
                  <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('dalton.weixinBot.scanPrompt')}
                  </p>
                  {qrState.qrcode_url && (
                    <div className="flex items-center gap-3">
                      {qrState.qrcode_url.startsWith('data:image/') ? (
                        <img
                          src={qrState.qrcode_url}
                          alt={t('dalton.weixinBot.title')}
                          className="h-36 w-36 rounded border p-1"
                          style={{
                            borderColor: 'var(--color-border-muted)',
                            backgroundColor: 'var(--color-bg-card)',
                          }}
                        />
                      ) : (
                        <a
                          href={qrState.qrcode_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-xs underline"
                          style={{ color: 'var(--color-accent-primary)' }}
                        >
                          {qrState.qrcode_url}
                        </a>
                      )}
                    </div>
                  )}
                  <DaltonLoadingRow label={t('dalton.weixinBot.pollingHint')} />
                </>
              )}

              {qrState.status === 'verify_code' && (
                <div className="flex flex-wrap items-center gap-2">
                  <Input
                    value={verifyCode}
                    onChange={(e) => setVerifyCode(e.target.value)}
                    placeholder={t('dalton.weixinBot.verifyCodeLabel', { code: '' })}
                    className="h-8 w-44 text-xs"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8 text-xs"
                    disabled={!verifyCode.trim() || verifyMutation.isPending}
                    onClick={() => verifyMutation.mutate()}
                  >
                    {t('dalton.common.confirm')}
                  </Button>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </DaltonSettingsCard>
  );
}
