import React, { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QrCode, RefreshCw } from 'lucide-react';
import { ApiError } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { queryKeys } from '@/lib/queryKeys';
import {
  createDaltonChannelBindCode,
  fetchDaltonChannelBindings,
  startDaltonWeixinQrLogin,
  unbindDaltonChannel,
  verifyDaltonWeixinQrCode,
  waitDaltonWeixinQrLogin,
  type DaltonChannelBindCodeCreateResponse,
  type DaltonWeixinBotPublicState,
  type DaltonWeixinQrPublicResponse,
  type DaltonWeixinQrPublicStatus,
} from '@/pages/Settings/utils/daltonApi';

const POLLABLE_QR_STATUSES: ReadonlySet<DaltonWeixinQrPublicStatus> = new Set([
  'wait',
  'scaned',
  'scaned_but_redirect',
]);

function isBotActive(bot: DaltonWeixinBotPublicState | null | undefined): boolean {
  return bot?.configured === true && bot.status === 'active';
}

function formatBotStatusLabel(bot: DaltonWeixinBotPublicState | null | undefined): string {
  if (!bot || bot.configured !== true) return '未连接';
  if (bot.status === 'active') return '已连接';
  if (bot.status === 'expired' || bot.status === 'session_expired') return '登录已过期（请重新扫码）';
  if (bot.status === 'error') return '连接异常';
  return '未连接';
}

function formatQrStatusLabel(qr: DaltonWeixinQrPublicResponse): string {
  switch (qr.status) {
    case 'wait':
      return '请使用个人微信扫描二维码';
    case 'scaned':
    case 'scaned_but_redirect':
      return '已扫码，请在手机微信上确认登录';
    case 'verify_code':
      return '手机端要求输入验证码，请在下方输入后提交';
    case 'confirmed':
      return '微信扫码连接成功';
    case 'expired':
      return '二维码已过期，请重新生成二维码';
    case 'error':
      return qr.message || '扫码过程发生错误，请重新生成二维码';
    default:
      return qr.message || qr.status;
  }
}

function isQrLoginConfirmed(status: DaltonWeixinQrPublicStatus): boolean {
  return status === 'confirmed';
}

export function DaltonWeixinChannelCard(): React.ReactElement | null {
  const queryClient = useQueryClient();
  const [bindCodeResult, setBindCodeResult] = useState<DaltonChannelBindCodeCreateResponse | null>(null);
  const [qrState, setQrState] = useState<DaltonWeixinQrPublicResponse | null>(null);
  const [verifyCodeInput, setVerifyCodeInput] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);
  const pollStoppedRef = useRef(false);

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    staleTime: 30_000,
    retry: false,
  });

  const createCodeMutation = useMutation({
    mutationFn: () => createDaltonChannelBindCode('weixin'),
    onSuccess: (res) => {
      setActionError(null);
      setBindCodeResult(res);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error && err.message ? err.message : '生成绑定码失败';
      setActionError(msg);
    },
  });

  const unbindMutation = useMutation({
    mutationFn: () => unbindDaltonChannel('weixin'),
    onSuccess: () => {
      setActionError(null);
      setBindCodeResult(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error && err.message ? err.message : '解除绑定失败';
      setActionError(msg);
    },
  });

  const startQrMutation = useMutation({
    mutationFn: () => startDaltonWeixinQrLogin(),
    onSuccess: (res) => {
      setActionError(null);
      setVerifyCodeInput('');
      pollStoppedRef.current = false;
      setQrState(res);
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error && err.message ? err.message : '发起微信扫码失败';
      setActionError(msg);
    },
  });

  const verifyCodeMutation = useMutation({
    mutationFn: (params: { sessionKey: string; code: string }) =>
      verifyDaltonWeixinQrCode(params.sessionKey, params.code),
    onSuccess: (res) => {
      setActionError(null);
      setVerifyCodeInput('');
      setQrState(res);
      if (isQrLoginConfirmed(res.status)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      }
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error && err.message ? err.message : '提交验证码失败';
      setActionError(msg);
    },
  });

  const sessionKey = qrState?.session_key ?? null;
  const qrStatus = qrState?.status ?? null;
  const shouldPollQr =
    Boolean(sessionKey) &&
    qrStatus !== null &&
    POLLABLE_QR_STATUSES.has(qrStatus);

  useEffect(() => {
    if (!sessionKey || !shouldPollQr) {
      return;
    }
    let cancelled = false;
    let timerId: ReturnType<typeof setTimeout> | null = null;

    const pollOnce = async () => {
      if (cancelled || pollStoppedRef.current) return;
      try {
        const next = await waitDaltonWeixinQrLogin(sessionKey);
        if (cancelled || pollStoppedRef.current) return;
        setQrState((prev) => (prev?.session_key === sessionKey ? next : prev));
        if (isQrLoginConfirmed(next.status)) {
          void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
          return;
        }
        const keepPolling = POLLABLE_QR_STATUSES.has(next.status);
        if (keepPolling && !cancelled) {
          timerId = setTimeout(() => {
            void pollOnce();
          }, 1200);
        }
      } catch (err: unknown) {
        if (cancelled || pollStoppedRef.current) return;
        const msg = err instanceof Error && err.message ? err.message : '查询扫码状态失败';
        setActionError(msg);
      }
    };

    timerId = setTimeout(() => {
      void pollOnce();
    }, 800);

    return () => {
      cancelled = true;
      if (timerId !== null) {
        clearTimeout(timerId);
      }
    };
  }, [sessionKey, shouldPollQr, queryClient]);

  if (bindingsQuery.isPending) {
    return null;
  }

  const refusalStatus = (bindingsQuery.error as ApiError | null)?.response?.status;
  if (bindingsQuery.isError && (refusalStatus === 401 || refusalStatus === 403)) {
    return null;
  }

  const bindings = bindingsQuery.data;
  const isAdmin = bindings?.user_tier === 'admin';
  const weixinIdentity = bindings?.identities?.weixin ?? null;
  const weixinBot = bindings?.weixin_bot ?? null;

  const handleVerifySubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!sessionKey) return;
    const trimmed = verifyCodeInput.trim();
    if (!trimmed) {
      setActionError('请输入验证码');
      return;
    }
    verifyCodeMutation.mutate({ sessionKey, code: trimmed });
  };

  return (
    <div
      id="dalton-weixin-channel"
      className="flex flex-col gap-3 rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-weixin-channel-card"
    >
      <div
        className="flex items-start justify-between gap-4 border-b pb-3"
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        <div className="flex flex-col gap-1">
          <span
            className="text-sm font-semibold"
            style={{ color: 'var(--color-text-primary)' }}
          >
            个人微信（iLink 私聊）
          </span>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            绑定个人微信后可直接私聊研究总监（Chief of Staff），无需 @ 提及；当前仅支持私聊。
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="shrink-0"
          aria-label="刷新微信状态"
          disabled={bindingsQuery.isFetching}
          onClick={() => void bindingsQuery.refetch()}
        >
          <RefreshCw
            className={bindingsQuery.isFetching ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'}
          />
          刷新
        </Button>
      </div>

      {/* User Binding Section */}
      <div className="flex flex-col gap-2" data-testid="dalton-weixin-identity-section">
        <span className="text-xs font-semibold" style={{ color: 'var(--color-text-primary)' }}>
          我的微信绑定
        </span>
        {weixinIdentity ? (
          <div
            className="flex items-center justify-between gap-2 rounded px-3 py-2 text-xs"
            style={{ backgroundColor: 'var(--color-bg-tag)' }}
            data-testid="dalton-weixin-bound-identity"
          >
            <div className="flex flex-col gap-0.5 min-w-0">
              <span className="font-mono truncate" style={{ color: 'var(--color-text-primary)' }}>
                {weixinIdentity.platform_display_name || weixinIdentity.platform_user_id}
              </span>
              <span className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                已绑定微信标识：{weixinIdentity.platform_user_id}
              </span>
            </div>
            <Button
              variant="ghost"
              size="sm"
              disabled={unbindMutation.isPending}
              onClick={() => unbindMutation.mutate()}
              data-testid="dalton-weixin-unbind-btn"
            >
              {unbindMutation.isPending ? '解绑中…' : '解除绑定'}
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                尚未绑定个人微信账号
              </span>
              <Button
                size="sm"
                disabled={createCodeMutation.isPending}
                onClick={() => createCodeMutation.mutate()}
                data-testid="dalton-weixin-bind-code-btn"
              >
                {createCodeMutation.isPending ? '生成中…' : '生成绑定码'}
              </Button>
            </div>
            {bindCodeResult && (
              <div
                className="flex flex-col gap-1 rounded px-3 py-2 text-xs"
                style={{ backgroundColor: 'var(--color-bg-tag)' }}
                data-testid="dalton-weixin-bind-code-box"
              >
                <span className="font-mono font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                  /bind {bindCodeResult.code}
                </span>
                <span className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                  {bindCodeResult.instruction}
                </span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Bot Status Section (visible when backend provides weixin_bot) */}
      {(weixinBot || isAdmin) && (
        <div
          className="flex flex-col gap-2 border-t pt-3"
          style={{ borderColor: 'var(--color-border-muted)' }}
          data-testid="dalton-weixin-bot-section"
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex flex-col gap-0.5">
              <span className="text-xs font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                微信机器人连接状态：{formatBotStatusLabel(weixinBot)}
              </span>
              {weixinBot?.ilink_bot_id && (
                <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                  Bot ID: {weixinBot.ilink_bot_id}
                </span>
              )}
            </div>
            {isAdmin && (
              <Button
                size="sm"
                disabled={startQrMutation.isPending}
                onClick={() => startQrMutation.mutate()}
                data-testid="dalton-weixin-qr-start-btn"
              >
                <QrCode className="mr-1 h-3.5 w-3.5" />
                {startQrMutation.isPending
                  ? '获取二维码…'
                  : qrState || isBotActive(weixinBot)
                    ? '重新扫码连接'
                    : '管理员扫码连接'}
              </Button>
            )}
          </div>

          {isAdmin && qrState && (
            <div
              className="flex flex-col gap-2 rounded p-3 text-xs"
              style={{ backgroundColor: 'var(--color-bg-tag)' }}
              data-testid="dalton-weixin-qr-panel"
            >
              <span
                className="font-semibold"
                style={{ color: 'var(--color-text-primary)' }}
                data-testid="dalton-weixin-qr-status"
              >
                {formatQrStatusLabel(qrState)}
              </span>

              {qrState.qrcode_url &&
                (qrState.status === 'wait' ||
                  qrState.status === 'scaned' ||
                  qrState.status === 'scaned_but_redirect') && (
                  <div className="flex flex-col items-start gap-1.5">
                    {qrState.qrcode_url.startsWith('data:image/') ? (
                      <img
                        src={qrState.qrcode_url}
                        alt="微信扫码二维码"
                        className="h-44 w-44 rounded border bg-white p-2 object-contain"
                        data-testid="dalton-weixin-qr-image"
                      />
                    ) : (
                      <a
                        href={qrState.qrcode_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="underline font-mono break-all"
                        style={{ color: 'var(--color-text-primary)' }}
                        data-testid="dalton-weixin-qr-link"
                      >
                        打开微信扫码页面
                      </a>
                    )}
                  </div>
                )}

              {qrState.status === 'verify_code' && (
                <form
                  onSubmit={handleVerifySubmit}
                  className="flex items-center gap-2 pt-1"
                  data-testid="dalton-weixin-verify-form"
                >
                  <Input
                    aria-label="微信登录验证码"
                    placeholder="输入手机显示的验证码"
                    value={verifyCodeInput}
                    disabled={verifyCodeMutation.isPending}
                    onChange={(e) => {
                      setVerifyCodeInput(e.target.value);
                      if (actionError) setActionError(null);
                    }}
                    data-testid="dalton-weixin-verify-input"
                  />
                  <Button
                    type="submit"
                    size="sm"
                    disabled={verifyCodeMutation.isPending}
                    className="shrink-0"
                    data-testid="dalton-weixin-verify-submit"
                  >
                    {verifyCodeMutation.isPending ? '提交中…' : '提交验证码'}
                  </Button>
                </form>
              )}
            </div>
          )}
        </div>
      )}

      {actionError && (
        <p role="alert" className="text-xs" style={{ color: 'var(--color-loss)' }} data-testid="dalton-weixin-error">
          {actionError}
        </p>
      )}
    </div>
  );
}
