import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadDaltonAuth() {
  vi.resetModules();
  return import('../daltonAuth');
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('daltonAuth config', () => {
  it('defaults to oss mode with isDaltonAuth=false when VITE_DALTON_AUTH is unset', async () => {
    vi.stubEnv('VITE_DALTON_AUTH', '');
    vi.stubEnv('VITE_SUPABASE_URL', 'https://example.supabase.co');
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb-pub-key');

    const { DALTON_AUTH_MODE, isDaltonAuth } = await loadDaltonAuth();
    expect(DALTON_AUTH_MODE).toBe('oss');
    expect(isDaltonAuth).toBe(false);
  });

  it('keeps isDaltonAuth=false when VITE_DALTON_AUTH=supabase but Supabase env vars are missing', async () => {
    vi.stubEnv('VITE_DALTON_AUTH', 'supabase');
    vi.stubEnv('VITE_SUPABASE_URL', 'https://example.supabase.co');
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', '');

    const { DALTON_AUTH_MODE, isDaltonAuth } = await loadDaltonAuth();
    expect(DALTON_AUTH_MODE).toBe('supabase');
    expect(isDaltonAuth).toBe(false);
  });

  it('enables isDaltonAuth when VITE_DALTON_AUTH=supabase and both Supabase env vars are set', async () => {
    vi.stubEnv('VITE_DALTON_AUTH', 'supabase');
    vi.stubEnv('VITE_SUPABASE_URL', 'https://example.supabase.co');
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb-pub-key');

    const { DALTON_AUTH_MODE, isDaltonAuth } = await loadDaltonAuth();
    expect(DALTON_AUTH_MODE).toBe('supabase');
    expect(isDaltonAuth).toBe(true);
  });

  it('keeps APP_ENTRY_PATH="/" in OSS mode so unauthenticated routes redirect to LoginPage at "/"', async () => {
    vi.stubEnv('VITE_HOST_MODE', 'oss');
    vi.stubEnv('VITE_DALTON_AUTH', 'supabase');
    vi.stubEnv('VITE_SUPABASE_URL', 'https://example.supabase.co');
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb-pub-key');
    vi.resetModules();

    const { APP_ENTRY_PATH, isPlatformMode } = await import('../hostMode');
    const { isDaltonAuth } = await import('../daltonAuth');
    expect(isPlatformMode).toBe(false);
    expect(isDaltonAuth).toBe(true);
    expect(APP_ENTRY_PATH).toBe('/');
  });
});
