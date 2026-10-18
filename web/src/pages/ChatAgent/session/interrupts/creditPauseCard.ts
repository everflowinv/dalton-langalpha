/**
 * The card state a `credit_pause` interrupt projects to. Shared by the live and
 * history paths so one wire payload cannot render two different cards.
 */

import type { CreditPauseState } from '@/types/chat';
import type { ActionRequest } from '@/types/sse';
import { isPlatformMode } from '@/config/hostMode';
import { buildRateLimitError } from '@/utils/rateLimitError';

export function buildCreditPauseState(
  request: ActionRequest,
  interruptId: string,
): CreditPauseState {
  // The denial copy is the platform's and lands here straight off the wire, so
  // `message?: string` is a compile-time claim rather than a check: a non-string
  // would reach JSX and take the transcript down for a user already stuck at a
  // pause, so an unusable one degrades to the card's no-message layout.
  const message = typeof request.message === 'string' ? request.message : undefined;
  // A pause is a quota denial the user resolves on the same two pages a 429
  // sends them to, so the links come from that builder rather than a second
  // copy — including its exclusion for denials no account page can fix.
  let { links } = buildRateLimitError(
    { type: request.type, message },
    (import.meta.env.VITE_PLATFORM_URL as string | undefined) || '/account',
  );
  // DALTON-HOOK(pause-links): point OSS credit pause card to Dalton budget settings
  if (!isPlatformMode && (Boolean(import.meta.env.VITE_DALTON_MODE) || message?.includes('Dalton') || message?.includes('今日预算') || message?.includes('任务预算'))) {
    links = [{ label: '调整日预算', labelKey: 'dalton.budget.adjust', url: '/settings?tab=model#dalton-budget' }];
  }
  return { status: 'pending', message, links, interruptId };
}
