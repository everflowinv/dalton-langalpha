/**
 * Dalton's own onboarding entry: the Chief of Staff interview in Home
 * (product decision 3, roadmap §6). Upstream's `/welcome` flow — whose first
 * screen is a brokerage connect — never opens in a Dalton build; every path
 * that would reach it (first-run auto-open, the getting-started profile
 * task, Settings' edit link, the legacy `?onboarding=connect` URL) lands on
 * `DaltonOnboardingRedirect` instead, which opens Home's default thread with
 * the interview's opening turn. Trading stays hidden: the kickoff always
 * takes the no-brokerage branch.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { BrandMark } from '@/components/brand/BrandMark';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/ui/use-toast';
import { useCapabilities } from '@/hooks/useCapabilities';
import { resolveChatWorkspace } from '@/hooks/useHomeWorkspace';
import { warmWorkspace } from '@/pages/ChatAgent/utils/warmWorkspace';

type Translate = ReturnType<typeof useTranslation>['t'];

/**
 * The turn that opens the interview: what the user is shown as having said,
 * plus the instruction riding the `dalton-onboarding` skill. The message is the
 * user's and follows their language; the instruction is the agent's and
 * stays English, as every other skill instruction does.
 */
export function daltonInterviewKickoff(t: Translate): {
  initialMessage: string;
  additionalContext: Record<string, unknown>[];
} {
  return {
    initialMessage: t('onboarding.kickoff.noBrokerage'),
    additionalContext: [
      {
        type: 'skills',
        name: 'dalton-onboarding',
        instruction:
          'Start the Dalton onboarding interview: ask which stocks or assets the user holds or watches and which markets they follow, then collect investment style, risk tolerance, and answer preferences before marking onboarding complete.',
      },
    ],
  };
}

/**
 * Lands a `/welcome` visit in Dalton's interview. `edit` (Settings' edit
 * link) reviews the saved preferences instead of walking the interview.
 *
 * DALTON-HOOK(standalone-core): In Core profile (`profile === 'core'`), initial
 * entry does not auto-send a message that consumes the user's BYOK quota; it
 * renders a "Start setup" button so the user opts in explicitly. Everflow
 * profile preserves the immediate redirect.
 */
export function DaltonOnboardingRedirect({ edit = false }: { edit?: boolean }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  const { toast } = useToast();
  const { profile, isLoading: capabilitiesLoading, brandName } = useCapabilities();
  const firedRef = useRef(false);
  const mountedRef = useRef(true);
  const [starting, setStarting] = useState(false);

  const requireExplicitStart = !edit && profile === 'core';

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const launchInterview = useCallback(async () => {
    if (firedRef.current) return;
    firedRef.current = true;
    setStarting(true);
    const workspaceId = await resolveChatWorkspace(queryClient, 'all').catch(() => null);
    if (!mountedRef.current) return;
    if (!workspaceId) {
      firedRef.current = false;
      setStarting(false);
      toast({
        variant: 'destructive',
        title: t('common.error'),
        description: t('dashboard:failedOnboarding'),
      });
      navigate('/chat', { replace: true });
      return;
    }
    warmWorkspace(workspaceId, queryClient);
    navigate('/chat/t/__default__', {
      replace: true,
      state: edit
        ? { workspaceId, initialMessage: t('onboarding.kickoff.modifyPreferences') }
        : { workspaceId, ...daltonInterviewKickoff(t) },
    });
  }, [navigate, queryClient, toast, t, edit]);

  useEffect(() => {
    if (capabilitiesLoading || requireExplicitStart) return;
    void launchInterview();
  }, [capabilitiesLoading, requireExplicitStart, launchInterview]);

  if (!requireExplicitStart) {
    return null;
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center px-6 py-12"
      style={{ backgroundColor: 'var(--color-bg-page)' }}
      data-testid="dalton-core-onboarding-gate"
    >
      <div
        className="w-full max-w-md rounded-2xl border p-8 flex flex-col gap-5"
        style={{
          backgroundColor: 'var(--color-bg-card)',
          borderColor: 'var(--color-border-muted)',
        }}
      >
        <div className="flex items-center gap-2.5">
          <BrandMark className="h-6 w-6" />
          <span
            className="text-sm font-medium"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {brandName}
          </span>
        </div>
        <div className="space-y-2">
          <h1
            className="text-xl font-semibold tracking-tight"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('onboarding.daltonCore.title')}
          </h1>
          <p
            className="text-sm leading-relaxed"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('onboarding.daltonCore.description')}
          </p>
        </div>
        <div className="flex flex-col gap-2.5 pt-1">
          <Button
            type="button"
            disabled={starting}
            onClick={() => void launchInterview()}
            data-testid="dalton-onboarding-start"
          >
            {starting ? t('common.loading') : t('onboarding.daltonCore.startButton')}
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={starting}
            onClick={() => navigate('/chat', { replace: true })}
            data-testid="dalton-onboarding-skip"
          >
            {t('onboarding.daltonCore.skipButton')}
          </Button>
        </div>
      </div>
    </div>
  );
}
