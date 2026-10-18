// integrations/openclaw/dalton-langalpha-broker/src/limits.mjs

export function createConcurrencyLimiter(maxConcurrent = 8) {
  const limit = Math.max(1, Math.min(64, Number(maxConcurrent) || 8));
  let active = 0;

  return {
    get activeCount() {
      return active;
    },
    get limit() {
      return limit;
    },
    tryAcquire() {
      if (active >= limit) return null;
      active += 1;
      let released = false;
      return () => {
        if (!released) {
          released = true;
          active = Math.max(0, active - 1);
        }
      };
    },
  };
}
