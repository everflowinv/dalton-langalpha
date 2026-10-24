import { useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router';
import { ONBOARDING_CONNECT, ONBOARDING_PARAM, useStartOnboarding } from './useStartOnboarding';

/**
 * DALTON-HOOK(hide-trading): never render the brokerage connect sheet. If a URL
 * carries `?onboarding=connect`, strip the param and jump straight to the
 * personalization interview.
 */
export function OnboardingConnectHost() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { start } = useStartOnboarding();
  const open = searchParams.get(ONBOARDING_PARAM) === ONBOARDING_CONNECT;
  const startedRef = useRef(false);

  useEffect(() => {
    if (!open) {
      startedRef.current = false;
      return;
    }
    if (startedRef.current) return;
    startedRef.current = true;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete(ONBOARDING_PARAM);
        return next;
      },
      { replace: true },
    );
    void start({ replace: true });
  }, [open, setSearchParams, start]);

  // DALTON-HOOK(hide-trading): render nothing and guard against duplicate start() calls
  return null;
}
