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
import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useToast } from '@/components/ui/use-toast';
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
 * Renders nothing while it works; on a workspace-resolution failure it falls
 * back to the chat shell with a toast rather than a dead screen.
 */
export function DaltonOnboardingRedirect({ edit = false }: { edit?: boolean }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  const { toast } = useToast();
  const firedRef = useRef(false);

  useEffect(() => {
    if (firedRef.current) return;
    firedRef.current = true;
    let cancelled = false;
    void (async () => {
      const workspaceId = await resolveChatWorkspace(queryClient, 'all').catch(() => null);
      if (cancelled) return;
      if (!workspaceId) {
        toast({
          variant: 'destructive',
          title: t('common.error'),
          description: t('dashboard.failedOnboarding'),
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
    })();
    return () => {
      cancelled = true;
    };
  }, [navigate, queryClient, toast, t, edit]);

  return null;
}
