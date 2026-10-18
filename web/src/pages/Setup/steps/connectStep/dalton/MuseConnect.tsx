import { useState, useCallback, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { ExternalLink, AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import { queryKeys } from '@/lib/queryKeys';
import {
  initiateMuseDevice,
  pollMuseDevice,
} from '@/pages/Settings/utils/daltonOAuthApi';
import { useTranslation } from 'react-i18next';
import { ProcessStep, DisclaimerBox, CopyButton, type LocationState } from '../shared';

export function MuseConnect({ state }: { state: LocationState }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation();

  const method = state.method ?? 'oauth';
  const provider = state.provider ?? 'muse-oauth';
  const displayName = state.displayName ?? provider;
  const brandKey = state.brandKey ?? provider;

  const [oauthPhase, setOauthPhase] = useState<'disclaimer' | 'connecting' | 'active'>('disclaimer');
  const [oauthError, setOauthError] = useState<string | null>(null);
  const [agreed, setAgreed] = useState(false);
  const [userCode, setUserCode] = useState<string | null>(null);
  const [verifyUrl, setVerifyUrl] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  const cancelledRef = useRef(false);
  useEffect(() => {
    return () => {
      cancelledRef.current = true;
    };
  }, []);

  const handleStart = useCallback(async () => {
    setOauthPhase('connecting');
    setOauthError(null);
    try {
      const result = await initiateMuseDevice();
      const code = result.user_code;
      const url = result.verification_url;
      const interval = result.interval || 5;

      setUserCode(code);
      setVerifyUrl(url);
      setOauthPhase('active');
      window.open(url, '_blank', 'noopener');

      cancelledRef.current = false;
      setPolling(true);
      const maxAttempts = 120;
      for (let i = 0; i < maxAttempts; i++) {
        await new Promise((r) => setTimeout(r, interval * 1000));
        if (cancelledRef.current) return;
        try {
          const pollResult = await pollMuseDevice();
          if (pollResult.success) {
            await queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
            await queryClient.invalidateQueries({ queryKey: queryKeys.oauth.muse() });
            await queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
            navigate('/setup/models', {
              state: { method, provider, displayName, brandKey },
            });
            return;
          }
        } catch {
          // Continue polling
        }
      }
      setOauthError(t('setup.errorAuthTimeout'));
      setPolling(false);
    } catch {
      setOauthError(t('setup.errorMuseOAuth'));
      setOauthPhase('disclaimer');
    }
  }, [queryClient, navigate, method, provider, displayName, brandKey, t]);

  const handleBack = useCallback(() => {
    navigate('/setup/provider', { state: { method } });
  }, [navigate, method]);

  return (
    <div className="flex flex-col gap-4 sm:gap-6">
      <div className="flex flex-col gap-1">
        <h2
          className="font-semibold"
          style={{ fontSize: '1.125rem', color: 'var(--color-text-primary)' }}
        >
          {t('setup.connectTitle', { provider: displayName })}
        </h2>
        <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          {t('setup.oauthMuseDesc')}
        </p>
      </div>

      {oauthPhase === 'disclaimer' && (
        <>
          <div
            className="rounded-lg p-4"
            style={{
              background: 'var(--color-bg-surface)',
              border: '1px solid var(--color-border-default)',
            }}
          >
            <p
              className="text-xs font-medium uppercase tracking-wide mb-3"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {t('setup.howItWorks')}
            </p>
            <div className="space-y-3">
              <ProcessStep
                number={1}
                title={t('setup.museStep1Title')}
                description={t('setup.museStep1Desc')}
              />
              <ProcessStep
                number={2}
                title={t('setup.museStep2Title')}
                description={t('setup.museStep2Desc')}
              />
              <ProcessStep
                number={3}
                title={t('setup.museStep3Title')}
                description={t('setup.museStep3Desc')}
              />
            </div>
          </div>

          <DisclaimerBox provider={provider} />

          <label className="flex items-start gap-3 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border accent-(--color-accent-primary)"
            />
            <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              {t('setup.oauthAgreeMeta')}
            </span>
          </label>

          <Button
            variant="default"
            disabled={!agreed}
            onClick={handleStart}
            className="w-full h-11"
          >
            <ExternalLink className="h-4 w-4 mr-1.5" />
            {t('setup.openVerification')}
          </Button>
        </>
      )}

      {oauthPhase === 'connecting' && (
        <div className="flex items-center justify-center py-12">
          <span aria-hidden="true" className="shrink-0">
            <Loader size={24} className="text-(--color-accent-primary)" />
          </span>
          <span className="ml-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('setup.connecting')}
          </span>
        </div>
      )}

      {oauthPhase === 'active' && userCode && (
        <div
          className="flex flex-col items-center gap-4 rounded-lg p-6"
          style={{
            background: 'var(--color-bg-surface)',
            border: '1px solid var(--color-border-default)',
          }}
        >
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4" style={{ color: 'var(--color-warning, #f59e0b)' }} />
            <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('setup.enterCodeOnMeta')}
            </p>
          </div>

          <div className="flex items-center gap-3">
            <code
              className="text-2xl font-mono font-bold tracking-widest px-4 py-2 rounded"
              style={{
                background: 'var(--color-bg-page)',
                color: 'var(--color-text-primary)',
                border: '1px solid var(--color-border-default)',
              }}
            >
              {userCode}
            </code>
            <CopyButton text={userCode} />
          </div>

          {polling && (
            <p
              className="text-xs flex items-center gap-1.5"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              <span aria-hidden="true" className="shrink-0">
                <Loader size={12} className="text-current" />
              </span>
              {t('setup.waitingApproval')}
            </p>
          )}

          <a
            href={verifyUrl ?? '#'}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm font-medium"
            style={{ color: 'var(--color-accent-primary)' }}
          >
            {t('setup.openVerificationAgain')}
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </div>
      )}

      {oauthError && (
        <p className="text-sm" style={{ color: 'var(--color-loss)' }}>
          {oauthError}
        </p>
      )}

      <div className="flex items-center justify-between pt-2">
        <Button variant="outline" onClick={handleBack}>
          {t('setup.back')}
        </Button>
      </div>
    </div>
  );
}
