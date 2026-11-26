import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { QrCode, RefreshCw } from 'lucide-react';
import { apiErrorStatus, isNotFoundError } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonChannelBindings,
  startDaltonWeixinQrLogin,
  unbindDaltonChannel,
  verifyDaltonWeixinQrCode,
  waitDaltonWeixinQrLogin,
  type DaltonWeixinMemberAccountView,
  type DaltonWeixinQrPublicResponse,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonActionConfirmButton,
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
  type DaltonStatusTone,
} from '@/components/dalton/primitives';
import { encodeQrCodeMatrix } from './qrCodeSvg';
import { resolveWeixinAllowExternal } from './useWeixinVisibility';

type PersonalWeixinDisplayStatus =
  | 'active'
  | 'pending_verification'
  | 'expired'
  | 'paused'
  | 'unconfigured';

interface ExtractedWeixinError {
  code: string | null;
  message: string | null;
  needRescan: boolean;
  revoked: boolean;
}

const WEIXIN_ERROR_KEY_MAP: Record<string, string> = {
  ilink_user_already_bound: 'dalton.weixinBind.errors.alreadyBound',
  weixin_capacity_full: 'dalton.weixinBind.errors.capacityFull',
  weixin_channel_disabled: 'dalton.weixinBind.errors.channelDisabled',
  external_access_disabled: 'dalton.weixinBind.errors.externalDisabled',
  member_not_in_roster: 'dalton.weixinBind.errors.memberNotInRoster',
  session_revoked: 'dalton.weixinBind.errors.sessionRevoked',
  login_session_revoked: 'dalton.weixinBind.errors.sessionRevoked',
  session_expired: 'dalton.weixinBind.errors.sessionExpired',
  qr_expired: 'dalton.weixinBind.errors.sessionExpired',
  invalid_verify_code: 'dalton.weixinBind.errors.invalidVerifyCode',
  verify_code_expired: 'dalton.weixinBind.errors.verifyCodeExpired',
  weixin_login_failed: 'dalton.weixinBind.errors.weixinLoginFailed',
  weixin_rate_limited: 'dalton.weixinBind.errors.rateLimited',
  stale_bot_generation: 'dalton.weixinBind.errors.staleBotGeneration',
  roster_lookup_error: 'dalton.weixinBind.errors.rosterLookupError',
  stale_gateway_epoch: 'dalton.weixinBind.errors.gatewayBusy',
  gateway_unreachable: 'dalton.weixinBind.errors.gatewayBusy',
};

const KNOWN_QR_TEXT_URL_RE = /^https:\/\/liteapp\.weixin\.qq\.com\//i;

function isAbortError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false;
  const e = err as { code?: unknown; name?: unknown };
  return e.code === 'ERR_CANCELED' || e.name === 'CanceledError' || e.name === 'AbortError';
}

function extractWeixinError(err: unknown): ExtractedWeixinError {
  const data = (err as { response?: { data?: unknown } })?.response?.data;
  const obj =
    data && typeof data === 'object' && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : null;
  const nestedDetail =
    obj?.detail && typeof obj.detail === 'object' && !Array.isArray(obj.detail)
      ? (obj.detail as Record<string, unknown>)
      : null;

  const rawCode =
    typeof obj?.code === 'string'
      ? obj.code
      : typeof nestedDetail?.code === 'string'
        ? nestedDetail.code
        : null;
  const rawMessage =
    typeof obj?.message === 'string'
      ? obj.message
      : typeof nestedDetail?.message === 'string'
        ? nestedDetail.message
        : null;
  const needRescan = Boolean(obj?.need_rescan ?? nestedDetail?.need_rescan);
  const revoked = Boolean(obj?.revoked ?? nestedDetail?.revoked);

  return {
    code: rawCode ? rawCode.trim().toLowerCase() : null,
    message: rawMessage ? rawMessage.trim() : null,
    needRescan,
    revoked,
  };
}

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

function isPollingStatus(status: string | undefined): boolean {
  return status === 'wait' || status === 'scaned' || status === 'scaned_but_redirect';
}

function resolveDisplayStatus(
  wx: DaltonWeixinMemberAccountView | null | undefined,
  fallbackLinked: boolean,
): PersonalWeixinDisplayStatus {
  if (wx) {
    if (wx.status === 'paused' || wx.channel_enabled === false || wx.status === 'disabled') {
      return 'paused';
    }
    if (wx.status === 'active') return 'active';
    if (wx.status === 'pending_verification') return 'pending_verification';
    if (wx.status === 'expired') return 'expired';
    return 'unconfigured';
  }
  return fallbackLinked ? 'active' : 'unconfigured';
}

function statusBadgeProps(
  status: PersonalWeixinDisplayStatus,
  t: (key: string) => string,
): { tone: DaltonStatusTone; label: string } {
  switch (status) {
    case 'active':
      return { tone: 'success', label: t('dalton.weixinBind.statusActive') };
    case 'pending_verification':
      return { tone: 'accent', label: t('dalton.weixinBind.statusPendingVerification') };
    case 'expired':
      return { tone: 'warning', label: t('dalton.weixinBind.statusExpired') };
    case 'paused':
      return { tone: 'warning', label: t('dalton.weixinBind.statusPaused') };
    default:
      return { tone: 'neutral', label: t('dalton.weixinBind.statusUnconfigured') };
  }
}

function WeixinQrCodeGraphic({
  qrSrc,
  alt,
  tooLongText,
}: {
  qrSrc: string;
  alt: string;
  tooLongText: string;
}) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const imgFailed = failedSrc === qrSrc;

  const isDataImg = qrSrc.startsWith('data:image/');
  const isHttpUrl = /^https?:\/\//i.test(qrSrc);
  const preferEncodedSvg =
    !isDataImg && (!isHttpUrl || KNOWN_QR_TEXT_URL_RE.test(qrSrc) || imgFailed);

  const svgMatrix = useMemo(
    () => (preferEncodedSvg ? encodeQrCodeMatrix(qrSrc) : null),
    [preferEncodedSvg, qrSrc],
  );

  if (preferEncodedSvg) {
    if (svgMatrix) {
      return (
        <div className="flex flex-wrap items-center gap-3">
          <svg
            data-testid="dalton-weixin-qr-svg"
            role="img"
            aria-label={alt}
            viewBox={`0 0 ${svgMatrix.size} ${svgMatrix.size}`}
            shapeRendering="crispEdges"
            className="h-40 w-40 rounded border p-1 bg-white"
            style={{ borderColor: 'var(--color-border-muted)' }}
          >
            <rect width={svgMatrix.size} height={svgMatrix.size} fill="#ffffff" />
            <path d={svgMatrix.svgPath} fill="#000000" />
          </svg>
        </div>
      );
    }
    return (
      <DaltonInlineNotice tone="warning" testId="dalton-weixin-qr-too-long">
        <div className="space-y-1">
          <p>{tooLongText}</p>
          {isHttpUrl && (
            <a
              href={qrSrc}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="dalton-weixin-qr-open-link"
              className="underline break-all font-mono text-[11px]"
            >
              {qrSrc}
            </a>
          )}
        </div>
      </DaltonInlineNotice>
    );
  }

  if (isDataImg || isHttpUrl) {
    return (
      <div className="flex flex-wrap items-center gap-3">
        <img
          src={qrSrc}
          alt={alt}
          data-testid="dalton-weixin-qr-image"
          onError={() => {
            if (!isDataImg) setFailedSrc(qrSrc);
          }}
          className="h-40 w-40 rounded border p-1"
          style={{
            borderColor: 'var(--color-border-muted)',
            backgroundColor: 'var(--color-bg-card)',
          }}
        />
      </div>
    );
  }

  return null;
}

interface StartQrVariables {
  gen: number;
  signal: AbortSignal;
}

interface VerifyQrVariables {
  sessionKey: string;
  code: string;
  gen: number;
  signal: AbortSignal;
}

/** Personal WeChat QR connection card ("我的微信") for Messaging & Channels. */
export function DaltonWeixinBindCard() {
  const { t } = useTranslation();
  const locale = useLocale();
  const queryClient = useQueryClient();
  const identity = useCallerIdentity();
  const { hasCapability } = useCapabilities();
  const canWeixin = hasCapability('channel_weixin');

  const [qrState, setQrState] = useState<DaltonWeixinQrPublicResponse | null>(null);
  const [verifyCode, setVerifyCode] = useState('');
  const [qrError, setQrError] = useState<string | null>(null);
  const [qrFailed, setQrFailed] = useState(false);
  const [unlinkedNotice, setUnlinkedNotice] = useState(false);

  const mountedRef = useRef(true);
  const opGenerationRef = useRef(0);
  const activeSessionKeyRef = useRef<string | null>(null);
  const pollTimerRef = useRef<number | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const hadQrBeforeUnbindRef = useRef(false);

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    enabled: canWeixin,
    retry: false,
  });

  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const abortInFlight = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, []);

  /** Bump operation generation, clear timer, and abort any in-flight start/wait/verify request. */
  const cancelActiveQrOperation = useCallback((): number => {
    opGenerationRef.current += 1;
    activeSessionKeyRef.current = null;
    clearPollTimer();
    abortInFlight();
    return opGenerationRef.current;
  }, [abortInFlight, clearPollTimer]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelActiveQrOperation();
    };
  }, [cancelActiveQrOperation]);

  const wx = bindingsQuery.data?.weixin_account;
  const fallbackWeixin = bindingsQuery.data?.bindings?.weixin;
  const effectiveTier =
    bindingsQuery.data?.user_tier ?? (identity.status === 'ready' ? identity.tier : null);
  const tierResolved =
    effectiveTier === 'admin' || effectiveTier === 'internal' || effectiveTier === 'external';

  // Fail closed until caller tier is confirmed; external members also require allow_external === true.
  const isExternalCaller = effectiveTier === 'external' || identity.isExternal;
  const externalAllowed = resolveWeixinAllowExternal(wx) === true;
  const shouldHideCard =
    !canWeixin ||
    isNotFoundError(bindingsQuery.error) ||
    !tierResolved ||
    (isExternalCaller && (bindingsQuery.isLoading || bindingsQuery.isError || !externalAllowed));

  // If the card becomes hidden while remaining mounted (e.g. external access toggled off),
  // abort any in-flight long-poll immediately and reset transient QR UI state.
  useEffect(() => {
    if (!shouldHideCard) return;
    cancelActiveQrOperation();
    setQrState(null);
    setQrError(null);
    setQrFailed(false);
    setVerifyCode('');
  }, [shouldHideCard, cancelActiveQrOperation]);

  const resolveErrorText = (err: unknown): { text: string; extracted: ExtractedWeixinError } => {
    const extracted = extractWeixinError(err);
    const key = extracted.code ? WEIXIN_ERROR_KEY_MAP[extracted.code] : undefined;
    if (key) {
      return { text: t(key), extracted };
    }
    if (extracted.message) {
      return { text: extracted.message, extracted };
    }
    return { text: t('dalton.weixinBind.loginFailed'), extracted };
  };

  const enterTerminalFailure = (
    message: string,
    opts?: { expired?: boolean; revoked?: boolean; sessionKey?: string },
  ) => {
    clearPollTimer();
    abortInFlight();
    if (opts?.expired || opts?.revoked) {
      setQrFailed(false);
      setQrError(message);
      setQrState({
        session_key: opts.sessionKey ?? '',
        status: opts.revoked ? 'superseded' : 'expired',
        revoked: Boolean(opts.revoked),
        qrcode_url: null,
        qrcode_img_content: null,
      });
    } else {
      activeSessionKeyRef.current = null;
      setQrState(null);
      setQrError(message);
      setQrFailed(true);
    }
  };

  const pollQr = async (sessionKey: string, gen: number, failureCount = 0) => {
    if (
      !mountedRef.current ||
      opGenerationRef.current !== gen ||
      activeSessionKeyRef.current !== sessionKey
    ) {
      return;
    }
    // Guarantee at most one in-flight wait request at any time.
    clearPollTimer();
    abortInFlight();
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const next = await waitDaltonWeixinQrLogin(sessionKey, { signal: controller.signal });
      if (
        !mountedRef.current ||
        opGenerationRef.current !== gen ||
        activeSessionKeyRef.current !== sessionKey ||
        controller.signal.aborted
      ) {
        return;
      }
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
      }
      if (next.status === 'error') {
        enterTerminalFailure(next.message || t('dalton.weixinBind.loginFailed'));
        return;
      }
      setQrFailed(false);
      setQrError(null);
      setQrState((prev) => mergeQrState(prev, next));
      if (next.status === 'confirmed') {
        activeSessionKeyRef.current = null;
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.weixinAdminOverview() });
        return;
      }
      if (next.status === 'expired' || next.status === 'superseded' || next.status === 'failed') {
        activeSessionKeyRef.current = null;
        if (next.revoked || next.status === 'superseded') {
          void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
        }
        return;
      }
      if (isPollingStatus(next.status)) {
        clearPollTimer();
        pollTimerRef.current = window.setTimeout(() => {
          pollTimerRef.current = null;
          if (
            mountedRef.current &&
            opGenerationRef.current === gen &&
            activeSessionKeyRef.current === sessionKey
          ) {
            void pollQr(sessionKey, gen, 0);
          }
        }, 1200);
      }
    } catch (err) {
      if (
        !mountedRef.current ||
        opGenerationRef.current !== gen ||
        activeSessionKeyRef.current !== sessionKey ||
        controller.signal.aborted ||
        isAbortError(err)
      ) {
        return;
      }
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
      }
      const status = apiErrorStatus(err);
      const { text, extracted } = resolveErrorText(err);
      const nonRetryable =
        status === 400 || status === 401 || status === 403 || status === 404 || status === 409;
      if (nonRetryable || failureCount >= 5) {
        if (extracted.needRescan || extracted.revoked) {
          activeSessionKeyRef.current = null;
          enterTerminalFailure(text, {
            expired: !extracted.revoked,
            revoked: extracted.revoked,
            sessionKey,
          });
          if (extracted.revoked) {
            void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
          }
        } else {
          enterTerminalFailure(text);
        }
        return;
      }
      const backoffMs = Math.min(1000 * 2 ** failureCount, 8000);
      clearPollTimer();
      pollTimerRef.current = window.setTimeout(() => {
        pollTimerRef.current = null;
        if (
          mountedRef.current &&
          opGenerationRef.current === gen &&
          activeSessionKeyRef.current === sessionKey
        ) {
          void pollQr(sessionKey, gen, failureCount + 1);
        }
      }, backoffMs);
    }
  };

  const startMutation = useMutation({
    mutationFn: (vars: StartQrVariables) => startDaltonWeixinQrLogin({ signal: vars.signal }),
    onSuccess: (resp, vars) => {
      if (!mountedRef.current || opGenerationRef.current !== vars.gen || vars.signal.aborted) {
        return;
      }
      if (resp.status === 'error') {
        enterTerminalFailure(resp.message || t('dalton.weixinBind.loginFailed'));
        return;
      }
      setQrError(null);
      setQrFailed(false);
      setUnlinkedNotice(false);
      setVerifyCode('');
      setQrState(resp);
      activeSessionKeyRef.current = resp.session_key || null;
      if (resp.session_key && isPollingStatus(resp.status)) {
        void pollQr(resp.session_key, vars.gen);
      } else if (resp.status === 'confirmed') {
        activeSessionKeyRef.current = null;
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.weixinAdminOverview() });
      }
    },
    onError: (err, vars) => {
      if (
        !mountedRef.current ||
        opGenerationRef.current !== vars.gen ||
        vars.signal.aborted ||
        isAbortError(err)
      ) {
        return;
      }
      enterTerminalFailure(resolveErrorText(err).text);
    },
  });

  const verifyMutation = useMutation({
    mutationFn: (vars: VerifyQrVariables) =>
      verifyDaltonWeixinQrCode(vars.sessionKey, vars.code, { signal: vars.signal }),
    onSuccess: (resp, vars) => {
      if (
        !mountedRef.current ||
        opGenerationRef.current !== vars.gen ||
        activeSessionKeyRef.current !== vars.sessionKey ||
        vars.signal.aborted
      ) {
        return;
      }
      if (resp.status === 'error') {
        enterTerminalFailure(resp.message || t('dalton.weixinBind.loginFailed'));
        return;
      }
      setQrError(null);
      setQrFailed(false);
      setQrState((prev) => mergeQrState(prev, resp));
      if (resp.status === 'confirmed') {
        activeSessionKeyRef.current = null;
        setVerifyCode('');
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.weixinAdminOverview() });
      } else if (resp.status === 'superseded' || resp.status === 'expired' || resp.status === 'failed') {
        activeSessionKeyRef.current = null;
        setVerifyCode('');
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      } else if (resp.session_key && isPollingStatus(resp.status)) {
        activeSessionKeyRef.current = resp.session_key;
        void pollQr(resp.session_key, vars.gen);
      }
    },
    onError: (err, vars) => {
      if (
        !mountedRef.current ||
        opGenerationRef.current !== vars.gen ||
        activeSessionKeyRef.current !== vars.sessionKey ||
        vars.signal.aborted ||
        isAbortError(err)
      ) {
        return;
      }
      const { text, extracted } = resolveErrorText(err);
      // Invalid verification code or temporary rate limit keeps the verify_code input open for retry.
      if (extracted.code === 'invalid_verify_code' || extracted.code === 'weixin_rate_limited') {
        setQrError(text);
        setQrFailed(false);
        return;
      }
      if (
        extracted.revoked ||
        extracted.code === 'verify_code_expired' ||
        extracted.code === 'session_revoked' ||
        extracted.code === 'login_session_revoked' ||
        extracted.code === 'session_expired' ||
        extracted.code === 'qr_expired'
      ) {
        activeSessionKeyRef.current = null;
        setVerifyCode('');
        enterTerminalFailure(text, {
          expired: !extracted.revoked,
          revoked: extracted.revoked,
          sessionKey: vars.sessionKey,
        });
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
        return;
      }
      if (extracted.needRescan) {
        activeSessionKeyRef.current = null;
        setVerifyCode('');
        void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      }
      enterTerminalFailure(text);
    },
  });

  const unbindMutation = useMutation({
    mutationFn: () => unbindDaltonChannel('weixin'),
    onSuccess: () => {
      if (!mountedRef.current) return;
      hadQrBeforeUnbindRef.current = false;
      cancelActiveQrOperation();
      setQrState(null);
      setQrError(null);
      setQrFailed(false);
      setVerifyCode('');
      setUnlinkedNotice(true);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.weixinAdminOverview() });
    },
    onError: () => {
      if (!mountedRef.current) return;
      if (hadQrBeforeUnbindRef.current) {
        hadQrBeforeUnbindRef.current = false;
        setQrState(null);
        setVerifyCode('');
        setQrFailed(true);
        setQrError(t('dalton.weixinBind.unbindFailed'));
      }
    },
  });

  const handleStartQr = () => {
    const gen = cancelActiveQrOperation();
    const controller = new AbortController();
    abortControllerRef.current = controller;
    setQrError(null);
    setQrFailed(false);
    setVerifyCode('');
    startMutation.mutate({ gen, signal: controller.signal });
  };

  const handleVerifySubmit = () => {
    const code = verifyCode.trim();
    const sessionKey = activeSessionKeyRef.current ?? qrState?.session_key ?? '';
    if (
      !code ||
      !sessionKey ||
      verifyMutation.isPending ||
      startMutation.isPending ||
      unbindMutation.isPending
    ) {
      return;
    }
    const gen = opGenerationRef.current;
    clearPollTimer();
    abortInFlight();
    const controller = new AbortController();
    abortControllerRef.current = controller;
    setQrError(null);
    verifyMutation.mutate({ sessionKey, code, gen, signal: controller.signal });
  };

  const handleUnbindConfirm = () => {
    hadQrBeforeUnbindRef.current = Boolean(qrState);
    cancelActiveQrOperation();
    return unbindMutation.mutateAsync().then(
      () => undefined,
      () => undefined,
    );
  };

  if (shouldHideCard) {
    return null;
  }

  const displayStatus = resolveDisplayStatus(wx, fallbackWeixin?.linked === true);
  const badge = statusBadgeProps(displayStatus, t);
  const hasAccount = Boolean(
    wx?.account_id ||
      wx?.linked ||
      displayStatus === 'active' ||
      displayStatus === 'pending_verification' ||
      displayStatus === 'expired',
  );
  const isConnected =
    displayStatus === 'active' || displayStatus === 'pending_verification';
  const nickname = wx?.ilink_username || wx?.platform_username || fallbackWeixin?.platform_username;
  const maskedId = wx?.masked_ilink_user_id || fallbackWeixin?.masked_ilink_user_id;
  const updatedAt = wx?.updated_at ?? fallbackWeixin?.updated_at;
  const lastInboundAt = wx?.last_inbound_at;
  const activePushReady = wx?.active_push_ready === true;

  const qrSrc = qrState?.qrcode_url || qrState?.qrcode_img_content || null;
  const normalizedQrStatus =
    qrState?.status === 'scaned_but_redirect' ? 'scaned' : qrState?.status;
  const qrTerminalExpired =
    normalizedQrStatus === 'expired' ||
    normalizedQrStatus === 'superseded' ||
    normalizedQrStatus === 'failed';
  const confirmedPendingVerify = Boolean(
    qrState?.pending_verification || qrState?.account_status === 'pending_verification',
  );

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
              testId="dalton-weixin-bind-status-badge"
              tone={badge.tone}
              label={badge.label}
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
          {displayStatus === 'paused' && (
            <DaltonInlineNotice tone="warning" testId="dalton-weixin-paused-notice">
              {t('dalton.weixinBind.pausedNotice')}
            </DaltonInlineNotice>
          )}
          {displayStatus === 'expired' && (
            <DaltonInlineNotice tone="warning" testId="dalton-weixin-expired-notice">
              {t('dalton.weixinBind.expiredNotice')}
            </DaltonInlineNotice>
          )}
          {displayStatus === 'pending_verification' && (
            <DaltonInlineNotice tone="info" testId="dalton-weixin-pending-verification-notice">
              {t('dalton.weixinBind.pendingVerificationNotice')}
            </DaltonInlineNotice>
          )}

          {hasAccount && (
            <div
              data-testid="dalton-weixin-account-summary"
              className="rounded-md border px-3.5 py-3 space-y-2.5"
              style={{
                borderColor: 'var(--color-border-muted)',
                backgroundColor: 'var(--color-bg-elevated)',
              }}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1 text-xs min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span
                      className="font-medium break-words"
                      style={{ color: 'var(--color-text-primary)' }}
                    >
                      {nickname || t('dalton.weixinBind.unnamedWechat')}
                    </span>
                    {maskedId && (
                      <code
                        data-testid="dalton-weixin-masked-id"
                        className="text-[11px] font-mono px-1.5 py-0.5 rounded"
                        style={{
                          color: 'var(--color-text-secondary)',
                          backgroundColor: 'var(--color-bg-card)',
                        }}
                      >
                        {maskedId}
                      </code>
                    )}
                  </div>
                  <div
                    className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]"
                    style={{ color: 'var(--color-text-tertiary)' }}
                  >
                    {updatedAt && (
                      <span>
                        {t('dalton.weixinBind.updatedAt', {
                          time: formatTimestamp(updatedAt, locale),
                        })}
                      </span>
                    )}
                    <span>
                      {lastInboundAt
                        ? t('dalton.weixinBind.lastInboundAt', {
                            time: formatTimestamp(lastInboundAt, locale),
                          })
                        : t('dalton.weixinBind.noInboundYet')}
                    </span>
                  </div>
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
                  onConfirm={handleUnbindConfirm}
                  className="h-8 text-xs gap-1 tap-target"
                  testId="dalton-weixin-unbind"
                />
              </div>

              {isConnected && (
                <div
                  data-testid="dalton-weixin-push-status"
                  className="pt-2 border-t flex flex-wrap items-center gap-2 text-xs"
                  style={{ borderColor: 'var(--color-border-muted)' }}
                >
                  <DaltonStatusBadge
                    dot
                    tone={activePushReady ? 'success' : 'warning'}
                    label={
                      activePushReady
                        ? t('dalton.weixinBind.pushReadyBadge')
                        : t('dalton.weixinBind.pushPendingBadge')
                    }
                  />
                  <span style={{ color: 'var(--color-text-secondary)' }}>
                    {activePushReady
                      ? t('dalton.weixinBind.pushReadyDesc')
                      : t('dalton.weixinBind.pushActivationHint')}
                  </span>
                </div>
              )}
            </div>
          )}

          {isConnected && (
            <DaltonInlineNotice tone="info" testId="dalton-weixin-forward-guide">
              {t('dalton.weixinBind.forwardGuide')}
            </DaltonInlineNotice>
          )}

          {unlinkedNotice && (
            <DaltonInlineNotice tone="success" testId="dalton-weixin-unlinked-success">
              {t('dalton.weixinBind.unlinkedSuccess')}
            </DaltonInlineNotice>
          )}
          {unbindMutation.isError && !qrFailed && (
            <DaltonInlineNotice tone="error">{t('dalton.weixinBind.unbindFailed')}</DaltonInlineNotice>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5 text-xs tap-target"
              disabled={startMutation.isPending || unbindMutation.isPending || displayStatus === 'paused'}
              onClick={handleStartQr}
              data-testid="dalton-weixin-start-qr"
            >
              <QrCode className="h-3.5 w-3.5" />
              <span>
                {startMutation.isPending
                  ? t('dalton.weixinBind.starting')
                  : qrState || qrFailed
                    ? t('dalton.weixinBind.refreshQr')
                    : hasAccount
                      ? t('dalton.weixinBind.rescanConnect')
                      : t('dalton.weixinBind.scanConnect')}
              </span>
            </Button>
          </div>

          {hasAccount && displayStatus !== 'paused' && (
            <p className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('dalton.weixinBind.rescanNote')}
            </p>
          )}

          {qrFailed && qrError && (
            <DaltonInlineNotice
              tone="error"
              testId="dalton-weixin-qr-failed"
              action={
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 gap-1 text-xs tap-target"
                  disabled={startMutation.isPending || displayStatus === 'paused'}
                  onClick={handleStartQr}
                  data-testid="dalton-weixin-retry-qr"
                >
                  <RefreshCw className="h-3 w-3" />
                  <span>{t('dalton.weixinBind.retryQr')}</span>
                </Button>
              }
            >
              {qrError}
            </DaltonInlineNotice>
          )}

          {!qrFailed && qrError && (
            <DaltonInlineNotice tone="error" testId="dalton-weixin-qr-error">
              {qrError}
            </DaltonInlineNotice>
          )}

          {qrState && (
            <div
              data-testid="dalton-weixin-qr-panel"
              className="rounded-md border p-3.5 space-y-2.5"
              style={{
                borderColor: 'var(--color-border-muted)',
                backgroundColor: 'var(--color-bg-elevated)',
              }}
            >
              {normalizedQrStatus === 'confirmed' ? (
                <DaltonInlineNotice tone="success" testId="dalton-weixin-qr-confirmed">
                  {confirmedPendingVerify
                    ? t('dalton.weixinBind.connectedPendingVerify')
                    : t('dalton.weixinBind.connectedSuccess')}
                </DaltonInlineNotice>
              ) : qrTerminalExpired ? (
                <DaltonInlineNotice
                  tone="warning"
                  testId="dalton-weixin-qr-expired"
                  action={
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 gap-1 text-xs tap-target"
                      disabled={startMutation.isPending || displayStatus === 'paused'}
                      onClick={handleStartQr}
                      data-testid="dalton-weixin-refresh-qr"
                    >
                      <RefreshCw className="h-3 w-3" />
                      <span>{t('dalton.weixinBind.refreshQr')}</span>
                    </Button>
                  }
                >
                  {qrState.revoked || normalizedQrStatus === 'superseded'
                    ? t('dalton.weixinBind.qrRevoked')
                    : t('dalton.weixinBind.qrExpired')}
                </DaltonInlineNotice>
              ) : (
                <>
                  <p
                    className="text-xs font-medium"
                    style={{ color: 'var(--color-text-primary)' }}
                    data-testid={
                      normalizedQrStatus === 'scaned'
                        ? 'dalton-weixin-qr-scaned'
                        : normalizedQrStatus === 'verify_code'
                          ? 'dalton-weixin-qr-verify-prompt'
                          : 'dalton-weixin-qr-wait'
                    }
                  >
                    {normalizedQrStatus === 'scaned'
                      ? t('dalton.weixinBind.scanedPrompt')
                      : normalizedQrStatus === 'verify_code'
                        ? t('dalton.weixinBind.verifyCodePrompt')
                        : t('dalton.weixinBind.scanPrompt')}
                  </p>

                  {qrSrc && normalizedQrStatus !== 'verify_code' && (
                    <WeixinQrCodeGraphic
                      qrSrc={qrSrc}
                      alt={t('dalton.weixinBind.title')}
                      tooLongText={t('dalton.weixinBind.qrTooLong')}
                    />
                  )}

                  {normalizedQrStatus === 'verify_code' ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <Input
                        value={verifyCode}
                        onChange={(e) => setVerifyCode(e.target.value)}
                        placeholder={t('dalton.weixinBind.verifyCodePlaceholder')}
                        aria-label={t('dalton.weixinBind.verifyCodePlaceholder')}
                        disabled={
                          verifyMutation.isPending ||
                          startMutation.isPending ||
                          unbindMutation.isPending
                        }
                        className="h-9 md:h-8 w-full sm:w-52 text-xs"
                        data-testid="dalton-weixin-verify-code-input"
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            handleVerifySubmit();
                          }
                        }}
                      />
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-9 md:h-8 text-xs tap-target"
                        disabled={
                          !verifyCode.trim() ||
                          verifyMutation.isPending ||
                          startMutation.isPending ||
                          unbindMutation.isPending
                        }
                        onClick={handleVerifySubmit}
                        data-testid="dalton-weixin-verify-code-submit"
                      >
                        {verifyMutation.isPending
                          ? t('dalton.common.saving')
                          : t('dalton.common.confirm')}
                      </Button>
                    </div>
                  ) : (
                    <DaltonLoadingRow
                      label={
                        normalizedQrStatus === 'scaned'
                          ? t('dalton.weixinBind.confirmingHint')
                          : t('dalton.weixinBind.pollingHint')
                      }
                    />
                  )}
                </>
              )}
            </div>
          )}
        </>
      )}
    </DaltonSettingsCard>
  );
}
