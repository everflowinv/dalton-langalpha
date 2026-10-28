import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import { Input } from '@/components/ui/input';
import { useRefreshAfterAccessChange } from '@/hooks/useModelAccessActions';
import {
  initiateAntigravityOAuth,
  pollAntigravityOAuth,
  submitAntigravityCallback,
  subscriptionKey,
} from '@/api/oauth';
import { SubscriptionBefore } from '@/components/modelAccess/SubscriptionBefore';
import type { SubscriptionProvider } from '@/api/oauth';

// DALTON-HOOK(native-oauth-ui): Google Antigravity's sign-in — the provider's
// page opens with an authorize URL and answers a loopback redirect this client
// polls for; pasting the callback URL by hand is the fallback where the
// loopback port cannot be reached (Docker, remote browsers).
export function AntigravityConnect({
  provider,
  onConnected,
  onCancel,
}: {
  provider: SubscriptionProvider;
  onConnected: () => void;
  onCancel?: () => void;
}) {
  const { t } = useTranslation();
  const refresh = useRefreshAfterAccessChange();
  const [agreed, setAgreed] = useState(false);
  const [phase, setPhase] = useState<'before' | 'starting' | 'waiting'>('before');
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [callbackInput, setCallbackInput] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const cancelled = useRef(false);
  useEffect(() => () => { cancelled.current = true; }, []);

  const ns = `modelAccess.${subscriptionKey(provider)}`;

  const finish = useCallback(async () => {
    cancelled.current = true;
    await refresh(provider);
    onConnected();
  }, [refresh, provider, onConnected]);

  const start = useCallback(async () => {
    setPhase('starting');
    setError(null);
    try {
      const { authorize_url } = await initiateAntigravityOAuth();
      if (cancelled.current) return;
      setAuthorizeUrl(authorize_url);
      setPhase('waiting');
      window.open(authorize_url, '_blank', 'noopener');

      for (let i = 0; i < 200; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        if (cancelled.current) return;
        try {
          if ((await pollAntigravityOAuth()).success) {
            await finish();
            return;
          }
        } catch {
          // Keep waiting; the manual paste below is the escape hatch.
        }
      }
      setError(t(`${ns}.timeout`));
    } catch {
      setError(t(`${ns}.failed`));
      setPhase('before');
    }
  }, [finish, ns, t]);

  const submitCallback = useCallback(async () => {
    const trimmed = callbackInput.trim();
    if (!trimmed) return;
    setSubmitting(true);
    setError(null);
    try {
      if ((await submitAntigravityCallback(trimmed)).success) {
        await finish();
      }
    } catch {
      setError(t(`${ns}.invalidCallback`));
    } finally {
      setSubmitting(false);
    }
  }, [callbackInput, finish, ns, t]);

  return (
    <div className="flex flex-col gap-4">
      {(phase === 'before' || phase === 'starting') && (
        <>
          <SubscriptionBefore provider={provider} agreed={agreed} onAgreedChange={setAgreed} />
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" onClick={start} disabled={!agreed || phase === 'starting'} aria-busy={phase === 'starting'} className="gap-1.5">
              {phase === 'starting' && <Loader size={14} className="text-current" />}
              {phase === 'starting' ? t('setup.connecting') : t(`${ns}.start`)}
              {phase !== 'starting' && <ExternalLink className="h-4 w-4" />}
            </Button>
            {onCancel && (
              <Button type="button" variant="ghost" onClick={onCancel} disabled={phase === 'starting'}>
                {t('common.cancel')}
              </Button>
            )}
          </div>
        </>
      )}

      {phase === 'waiting' && (
        <div
          className="flex flex-col items-center gap-4 rounded-lg p-6"
          style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
        >
          <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t(`${ns}.waiting`)}
          </p>
          <p className="flex items-center gap-1.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            <span aria-hidden="true" className="shrink-0">
              <Loader size={12} className="text-current" />
            </span>
            {t(`${ns}.waitingDetail`)}
          </p>
          <div className="flex w-full gap-2">
            <Input
              value={callbackInput}
              onChange={(e) => setCallbackInput(e.target.value)}
              placeholder="http://localhost:51121/oauth-callback?code=...&state=..."
              className="flex-1 font-mono text-xs"
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              type="button"
              disabled={submitting || !callbackInput.trim()}
              onClick={submitCallback}
              className="shrink-0"
            >
              {t(`${ns}.submit`)}
            </Button>
          </div>
          {authorizeUrl && (
            <a
              href={authorizeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm font-medium"
              style={{ color: 'var(--color-accent-primary)' }}
            >
              {t(`${ns}.openAgain`)}
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
        </div>
      )}

      {error && (
        <p className="text-sm" style={{ color: 'var(--color-loss)' }}>
          {error}
        </p>
      )}
    </div>
  );
}
