import { useEffect, useRef, useState } from 'react';
import { Link2, Unlink, ExternalLink, Shield, ClipboardCopy } from 'lucide-react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { useTranslation } from 'react-i18next';
import {
  initiateMuseDevice,
  pollMuseDevice,
  getMuseOAuthStatus,
  disconnectMuseOAuth,
  type DeviceCodeResponse,
  type OAuthStatusResponse,
} from '../../../utils/daltonOAuthApi';

export function MuseAccountRow({
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
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getMuseOAuthStatus();
        if (!cancelled) setStatus(res || { connected: false });
      } catch {
        // Ignore initial status error
      }
    })();
    return () => {
      cancelled = true;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, []);

  const handleDeviceCancel = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setIsPolling(false);
    setDeviceCode(null);
    setDeviceError(null);
  };

  const handleConnect = async () => {
    setShowDisclaimer(false);
    setIsConnecting(true);
    onError?.(null);
    setDeviceError(null);
    try {
      const device = await initiateMuseDevice();
      setDeviceCode(device);
      window.open(device.verification_url, '_blank', 'noopener');
      setIsPolling(true);
      const interval = (device.interval || 5) * 1000;
      const startTime = Date.now();
      const maxDuration = 10 * 60 * 1000;
      pollRef.current = setInterval(async () => {
        if (Date.now() - startTime > maxDuration) {
          handleDeviceCancel();
          setDeviceError(t('settings.codexTimeout'));
          return;
        }
        try {
          const result = await pollMuseDevice();
          if (result.success) {
            handleDeviceCancel();
            setStatus({ connected: true, email: null, plan_type: 'muse' });
            queryClient.invalidateQueries({ queryKey: queryKeys.oauth.muse() });
            queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
          }
        } catch (e: unknown) {
          const apiError = e as { response?: { data?: { detail?: string } } };
          handleDeviceCancel();
          setDeviceError(apiError.response?.data?.detail || t('settings.codexPollFailed'));
        }
      }, interval);
    } catch {
      onError?.(t('setup.errorMuseOAuth', 'Could not start Muse sign-in. Please try again.'));
    } finally {
      setIsConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    onError?.(null);
    try {
      await disconnectMuseOAuth();
      setStatus({ connected: false, account_id: null, email: null, plan_type: null });
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.muse() });
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
    } catch {
      onError?.('Failed to disconnect Muse');
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
                <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>Muse Code (Meta)</span>
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
                <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{status.email || t('settings.connected', 'Connected')}</p>
              ) : (
                <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('setup.oauthMuseDesc', 'Use Muse Code models with your Meta account')}
                </p>
              )}
            </div>
          </div>
          <div className="shrink-0">
            {/* DALTON-HOOK(team-credentials): hide disconnect button for non-PM team members */}
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
              {t('setup.enterCodeOnMeta', 'Confirm this code on auth.meta.com')}:
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
            {deviceError && (
              <p className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>{deviceError}</p>
            )}
          </div>
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
              {t('settings.museConnectTitle', 'Connect Muse Code (Meta)')}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-3">
              <p className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.claudeHowItWorks', 'How it works')}</p>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>1</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.museStep1Title', 'Open the Meta verification page')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.museStep1Desc', 'Click the button below to generate a one-time code and open auth.meta.com.')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>2</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.museStep2Title', 'Confirm the code')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.museStep2Desc', 'Sign in with your Meta account and approve the code shown here.')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>3</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.museStep3Title', 'Done')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.museStep3Desc', 'This screen detects the approval automatically.')}</p>
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
                    {t('setup.usageCountsMuse', 'Requests count against your Muse subscription quota, exactly as they would in the Muse Code CLI.')}
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
