import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { useToast } from '@/components/ui/use-toast';
import { resolveChatWorkspace } from '@/hooks/useHomeWorkspace';
import { warmWorkspace } from '@/pages/ChatAgent/utils/warmWorkspace';

/** The query parameter that opens the brokerage connect sheet over the page. */
export const ONBOARDING_PARAM = 'onboarding';
export const ONBOARDING_CONNECT = 'connect';

/**
 * Start onboarding: skip the brokerage connect step and go directly to the
 * Chief of Staff's personalization interview.
 */
// DALTON-HOOK(hide-trading): skip brokerage connect sheet and enter interview directly
export function useStartOnboarding() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { t } = useTranslation();

  /** `replace` drops the caller's own page from history on the way. */
  const start = useCallback(
    async ({ replace = false }: { at?: string; replace?: boolean } = {}) => {
      const workspaceId = await resolveChatWorkspace(queryClient, 'all').catch(() => null);
      if (!workspaceId) {
        toast({
          variant: 'destructive',
          title: t('common.error'),
          description: t('dashboard.failedOnboarding'),
        });
        return;
      }
      warmWorkspace(workspaceId, queryClient);
      navigate('/chat/t/__default__', {
        replace,
        state: {
          workspaceId,
          isOnboarding: true,
          onboardingBrokerages: [],
        },
      });
    },
    [navigate, queryClient, toast, t],
  );

  return { start };
}
