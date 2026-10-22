import { useEffect, useRef, useState } from 'react';
import { Link2, Unlink, ExternalLink, Shield } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { BRAND_NAME } from '@/config/brand';
import { useTranslation } from 'react-i18next';
import {
  initiateAntigravityOAuth,
  pollAntigravityOAuth,
  submitAntigravityCallback,
  getAntigravityOAuthStatus,
  disconnectAntigravityOAuth,
  type OAuthStatusResponse,
} from '../../../utils/daltonOAuthApi';

export function AntigravityAccountRow({
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
  const [isPolling, setIsPolling] = useState(false);
  const [isSubmittingCallback, setIsSubmittingCallback] = useState(false);
  const [callbackInput, setCallbackInput] = useState('');
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getAntigravityOAuthStatus();
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

  const handlePollCancel = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setIsPolling(false);
    setCallbackInput('');
    setError(null);
  };

  const handleConnect = async () => {
    setShowDisclaimer(false);
    setIsConnecting(true);
    onError?.(null);
    setError(null);
    try {
      const result = await initiateAntigravityOAuth();
      window.open(result.authorize_url, '_blank', 'noopener');
      setIsPolling(true);
      const startTime = Date.now();
      const maxDuration = 10 * 60 * 1000;
      pollRef.current = setInterval(async () => {
        if (Date.now() - startTime > maxDuration) {
          handlePollCancel();
          setError(t('setup.errorAuthTimeout', 'Timed out waiting for approval.'));
          return;
        }
        try {
          const poll = await pollAntigravityOAuth();
          if (poll.success) {
            handlePollCancel();
            setStatus({
              connected: true,
              email: poll.email || null,
              plan_type: poll.plan_type || null,
            });
            queryClient.invalidateQueries({ queryKey: queryKeys.oauth.antigravity() });
            queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
          }
        } catch {
          handlePollCancel();
          setError(t('setup.errorAntigravityOAuth', 'Could not start Google login. Please try again.'));
        }
      }, 3000);
    } catch {
      onError?.(t('setup.errorAntigravityOAuth', 'Could not start Google login. Please try again.'));
    } finally {
      setIsConnecting(false);
    }
  };

  const handleManualCallbackSubmit = async () => {
    const trimmed = callbackInput.trim();
    if (!trimmed) return;
    setIsSubmittingCallback(true);
    setError(null);
    try {
      const res = await submitAntigravityCallback(trimmed);
      if (res.success || res.connected) {
        handlePollCancel();
        setStatus({
          connected: true,
          email: res.email || null,
          plan_type: res.plan_type || null,
        });
        queryClient.invalidateQueries({ queryKey: queryKeys.oauth.antigravity() });
        queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
      }
    } catch (e: unknown) {
      const apiErr = e as { response?: { data?: { detail?: string } } };
      setError(apiErr.response?.data?.detail || t('setup.errorAntigravityOAuth', 'Failed to exchange callback URL.'));
    } finally {
      setIsSubmittingCallback(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    onError?.(null);
    try {
      await disconnectAntigravityOAuth();
      setStatus({ connected: false, account_id: null, email: null, plan_type: null });
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.antigravity() });
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
    } catch {
      onError?.('Failed to disconnect Antigravity');
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
                <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>Google Antigravity</span>
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
                  {t('settings.antigravityDesc', 'Use Gemini, Claude and GPT-OSS models with your Google Antigravity subscription')}
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
            ) : !isPolling ? (
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

        {isPolling && !status.connected && (
          <div className="mt-3 pt-3 space-y-2" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
            <div className="flex items-center gap-3">
              <span className="text-xs animate-pulse" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('setup.waitingGoogleCallback', 'Waiting for the Google redirect…')}
              </span>
              <button
                type="button"
                onClick={handlePollCancel}
                className="px-3 py-1.5 rounded-md text-xs font-medium"
                style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}
              >
                {t('common.cancel', 'Cancel')}
              </button>
            </div>
            <div className="flex items-center gap-2">
              <Input
                value={callbackInput}
                onChange={(e) => setCallbackInput(e.target.value)}
                placeholder="http://localhost:51121/oauth-callback?code=...&state=..."
                className="flex-1 text-xs font-mono"
                onKeyDown={(e) => e.key === 'Enter' && handleManualCallbackSubmit()}
              />
              <button
                type="button"
                onClick={handleManualCallbackSubmit}
                disabled={isSubmittingCallback || !callbackInput.trim()}
                className="px-3 py-1.5 rounded-md text-xs font-medium transition-opacity hover:opacity-90"
                style={{
                  backgroundColor: isSubmittingCallback ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)',
                  color: isSubmittingCallback ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)',
                }}
              >
                {isSubmittingCallback ? t('common.loading', 'Loading...') : t('common.submit', 'Submit')}
              </button>
            </div>
          </div>
        )}
        {error && (
          <p className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>{error}</p>
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
              {t('settings.antigravityConnectTitle', 'Connect Google Antigravity')}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-3">
              <p className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.claudeHowItWorks', 'How it works')}</p>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>1</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.antigravityStep1Title', 'Open the Google sign-in page')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.antigravityStep1Desc', 'Click the button below. A new tab opens to accounts.google.com.')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>2</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.antigravityStep2Title', 'Sign in and grant access')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.antigravityStep2Desc', `Approve the permission request for ${BRAND_NAME}. The page then redirects back to this machine automatically — nothing to copy.`)}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>3</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('setup.antigravityStep3Title', 'Done')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('setup.antigravityStep3Desc', 'This screen detects the connection by itself and moves on.')}</p>
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
                    {t('setup.usageCountsAntigravity', 'Requests count against your Antigravity subscription quota, exactly as they would in the Antigravity CLI or IDE.')}
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
              {t('setup.openGoogle', 'Open Google sign-in')}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
