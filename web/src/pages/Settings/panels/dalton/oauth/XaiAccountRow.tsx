import { useEffect, useRef, useState } from 'react';
import { Link2, Unlink, ExternalLink, Shield, ClipboardCopy } from 'lucide-react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { useTranslation } from 'react-i18next';
import {
  initiateXaiDevice,
  pollXaiDevice,
  getXaiOAuthStatus,
  disconnectXaiOAuth,
  type DeviceCodeResponse,
  type OAuthStatusResponse,
} from '../../../utils/daltonOAuthApi';

export function XaiAccountRow({
  onError,
  canDisconnect = true,
}: {
  onError?: (msg: string | null) => void;
  canDisconnect?: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [status, setStatus] = useState<OAuthStatusResponse>({ connected: false });
  const [showDisclaimer, setShowDisclaimer] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [deviceCode, setDeviceCode] = useState<DeviceCodeResponse | null>(null);
  const [deviceError, setDeviceError] = useState<string | null>(null);
  const [isPolling, setIsPolling] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollingActiveRef = useRef(false);
  const generationRef = useRef<number>(0);

  useEffect(() => {
    let cancelled = false;
    const initGen = generationRef.current;
    (async () => {
      try {
        const res = await getXaiOAuthStatus();
        if (!cancelled && generationRef.current === initGen) {
          setStatus(res || { connected: false });
        }
      } catch {
        // Ignore initial status error
      }
    })();
    return () => {
      cancelled = true;
      generationRef.current += 1;
      pollingActiveRef.current = false;
      if (pollRef.current) {
        clearTimeout(pollRef.current);
        pollRef.current = null;
      }
    };
  }, []);

  const handleDeviceCancel = () => {
    generationRef.current += 1;
    pollingActiveRef.current = false;
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
    setIsConnecting(false);
    setIsPolling(false);
    setDeviceCode(null);
    setDeviceError(null);
  };

  const handleConnect = async () => {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
    const gen = ++generationRef.current;
    setShowDisclaimer(false);
    setIsConnecting(true);
    onError?.(null);
    setDeviceError(null);
    try {
      const device = await initiateXaiDevice();
      if (generationRef.current !== gen) return;
      setDeviceCode(device);
      window.open(device.verification_url, '_blank', 'noopener');
      setIsPolling(true);
      pollingActiveRef.current = true;
      const initialIntervalSec = device.interval || 5;
      const startTime = Date.now();
      const maxDuration = 5 * 60 * 1000;

      const scheduleNextPoll = (delaySeconds: number) => {
        if (!pollingActiveRef.current || generationRef.current !== gen) return;
        pollRef.current = setTimeout(async () => {
          pollRef.current = null;
          if (!pollingActiveRef.current || generationRef.current !== gen) return;
          if (Date.now() - startTime > maxDuration) {
            handleDeviceCancel();
            setDeviceError(t('settings.codexTimeout'));
            return;
          }
          try {
            const result = await pollXaiDevice();
            if (!pollingActiveRef.current || generationRef.current !== gen) return;
            if (result.success) {
              handleDeviceCancel();
              setStatus({
                connected: true,
                account_id: result.account_id ?? null,
                email: result.email ?? null,
                plan_type: result.plan_type ?? 'supergrok',
              });
              queryClient.invalidateQueries({ queryKey: queryKeys.oauth.xai() });
              queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
              return;
            }
            const nextIntervalSec = result.interval || delaySeconds || 5;
            scheduleNextPoll(nextIntervalSec);
          } catch (e: unknown) {
            if (!pollingActiveRef.current || generationRef.current !== gen) return;
            const apiError = e as {
              response?: { data?: { detail?: string } };
              detail?: string;
              message?: string;
            };
            handleDeviceCancel();
            setDeviceError(
              apiError.response?.data?.detail ||
                apiError.detail ||
                apiError.message ||
                t('settings.codexPollFailed')
            );
          }
        }, delaySeconds * 1000);
      };

      scheduleNextPoll(initialIntervalSec);
    } catch {
      if (generationRef.current !== gen) return;
      onError?.(t('setup.errorXaiOAuth', 'Could not start xAI Grok sign-in. Please try again.'));
    } finally {
      if (generationRef.current === gen) {
        setIsConnecting(false);
      }
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    onError?.(null);
    try {
      await disconnectXaiOAuth();
      setStatus({ connected: false, account_id: null, email: null, plan_type: null });
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.xai() });
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
    } catch {
      onError?.('Failed to disconnect xAI Grok');
    } finally {
      setIsDisconnecting(false);
    }
  };

  return (
    <>
      <div
        className="rounded-lg px-4 py-3 mt-2"
        style={{
          backgroundColor: 'var(--color-bg-card)',
          border: `1px solid ${status.connected ? 'var(--color-success-soft)' : 'var(--color-border-muted)'}`,
        }}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div
              className="h-8 w-8 rounded-md flex items-center justify-center"
              style={{ backgroundColor: status.connected ? 'var(--color-success-soft)' : 'var(--color-accent-soft)' }}
            >
              <Link2 className="h-4 w-4" style={{ color: status.connected ? 'var(--color-success)' : 'var(--color-accent-primary)' }} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>xAI Grok (SuperGrok)</span>
                {status.connected && status.plan_type && (
                  <span
                    className="inline-flex items-center px-1.5 py-0.5 rounded text-[0.625rem] font-medium"
                    style={{ backgroundColor: 'var(--color-success-soft)', color: 'var(--color-success)' }}
                  >
                    {status.plan_type}
                  </span>
                )}
              </div>
              {status.connected ? (
                <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{status.email || status.account_id || t('settings.connected', 'Connected')}</p>
              ) : (
                <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('setup.oauthXaiDesc', 'Use Grok models with your SuperGrok subscription')}
                </p>
              )}
            </div>
          </div>
          <div className="shrink-0">
            {status.connected ? (
              canDisconnect ? (
                <button
                  type="button"
                  onClick={handleDisconnect}
                  disabled={isDisconnecting}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors"
                  style={{ color: 'var(--color-loss)', backgroundColor: 'transparent', border: '1px solid var(--color-loss)' }}
                >
                  <Unlink className="h-3 w-3" />
                  {isDisconnecting ? t('common.loading', 'Loading...') : t('settings.disconnect', 'Disconnect')}
                </button>
              ) : null
            ) : !deviceCode ? (
              <button
                type="button"
                onClick={() => setShowDisclaimer(true)}
                disabled={isConnecting}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-opacity hover:opacity-90"
                style={{
                  backgroundColor: isConnecting ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)',
                  color: isConnecting ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)',
                }}
              >
                <Link2 className="h-3 w-3" />
                {isConnecting ? t('common.loading', 'Loading...') : t('settings.connect', 'Connect')}
              </button>
            ) : null}
          </div>
        </div>

        {deviceCode && !status.connected && (
          <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
            <p className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)' }}>
              {t('setup.enterCodeOnXai', 'Confirm this code on auth.x.ai')}:
            </p>
            <div className="flex items-center gap-2 mb-2">
              <code
                className="text-lg font-mono font-bold tracking-widest px-3 py-1.5 rounded-md select-all"
                style={{
                  backgroundColor: 'var(--color-bg-elevated)',
                  border: '1px solid var(--color-border-muted)',
                  color: 'var(--color-text-primary)',
                  letterSpacing: '0.15em',
                }}
              >
                {deviceCode.user_code}
              </code>
              <button
                type="button"
                onClick={() => navigator.clipboard.writeText(deviceCode.user_code)}
                className="p-1.5 rounded-md transition-colors hover:opacity-80"
                style={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border-muted)' }}
                title={t('common.copy', 'Copy')}
              >
                <ClipboardCopy className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
              </button>
              {isPolling && (
                <span className="text-xs animate-pulse" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings.codexWaitingApproval')}
                </span>
              )}
            </div>
            <div className="flex items-center gap-3">
              <a
                href={deviceCode.verification_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs underline"
                style={{ color: 'var(--color-accent-primary)' }}
              >
                {t('settings.claudeOpenAgain', 'Open authorize page again')}
              </a>
              <button
                type="button"
                onClick={handleDeviceCancel}
                className="px-3 py-1.5 rounded-md text-xs font-medium"
                style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}
              >
                {t('common.cancel', 'Cancel')}
              </button>
            </div>
          </div>
        )}
        {deviceError && !status.connected && (
          <p className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>
            {deviceError}
          </p>
        )}
      </div>

      <Dialog open={showDisclaimer} onOpenChange={setShowDisclaimer}>
        <DialogContent
          className="sm:max-w-md border"
          style={{ backgroundColor: 'var(--color-bg-elevated)', borderColor: 'var(--color-border-elevated)' }}
        >
          <DialogHeader>
            <DialogTitle className="title-font flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
              <Link2 className="h-5 w-5" style={{ color: 'var(--color-accent-primary)' }} />
              {t('settings.xaiConnectTitle', 'Connect xAI Grok (SuperGrok)')}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-3">
              <p className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.claudeHowItWorks', 'How it works')}</p>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>1</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.xaiStep1Title', 'Open the xAI verification page')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.xaiStep1Desc', 'Click the button below to generate a one-time code and open auth.x.ai.')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>2</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.xaiStep2Title', 'Confirm the code')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.xaiStep2Desc', 'Sign in with your xAI account and approve the code shown here.')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>3</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.xaiStep3Title', 'Done')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.xaiStep3Desc', 'This screen detects the approval automatically.')}</p>
                </div>
              </div>
            </div>

            <div className="rounded-lg p-3" style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}>
              <div className="flex gap-2 items-start">
                <Shield className="h-4 w-4 shrink-0 mt-0.5" style={{ color: 'var(--color-text-tertiary)' }} />
                <div>
                  <p className="text-xs font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>{t('settings.claudeSecurityTitle', 'Security & Privacy')}</p>
                  <p className="text-[0.6875rem] leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.claudeSecurityDesc', 'Your tokens are encrypted at rest. We use them only to make API calls on your behalf.')}
                  </p>
                  <p className="text-[0.6875rem] leading-relaxed mt-1.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('setup.usageCountsXai', 'Requests count against your SuperGrok subscription quota, exactly as they would in the Grok CLI.')}
                  </p>
                </div>
              </div>
            </div>
          </div>

          <DialogFooter className="gap-2 pt-2">
            <button
              type="button"
              onClick={() => setShowDisclaimer(false)}
              className="px-3 py-1.5 rounded text-sm border"
              style={{ color: 'var(--color-text-primary)', borderColor: 'var(--color-border-default)' }}
              onMouseEnter={(e) => e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'}
              onMouseLeave={(e) => e.currentTarget.style.backgroundColor = 'transparent'}
            >
              {t('common.cancel', 'Cancel')}
            </button>
            <button
              type="button"
              onClick={handleConnect}
              className="px-4 py-1.5 rounded text-sm font-medium transition-opacity hover:opacity-90 flex items-center gap-1.5"
              style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t('setup.openVerification', 'Open verification page')}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
