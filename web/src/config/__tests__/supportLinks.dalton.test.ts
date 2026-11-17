import { afterEach, describe, expect, it, vi } from 'vitest';

describe('Dalton supportLinks & docs URL mapping', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('keeps upstream langalpha.ai URLs when VITE_BRAND_MARK is unset', async () => {
    vi.stubEnv('VITE_BRAND_MARK', '');
    vi.resetModules();

    const { DOCS_URL, DESKTOP_DOWNLOAD_URL } = await import('@/config/supportLinks');
    const { CLAUDE_CODE_DOCS } = await import('@/lib/claudeCode');

    expect(DOCS_URL).toBe('https://docs.langalpha.ai');
    expect(DESKTOP_DOWNLOAD_URL).toBe('https://langalpha.ai/download');
    expect(CLAUDE_CODE_DOCS.howItRuns).toBe('https://docs.langalpha.ai/concepts/claude-code/#how-it-runs');
  });

  it('routes DOCS_URL and CLAUDE_CODE_DOCS to /help and hides DESKTOP_DOWNLOAD_URL in Dalton builds', async () => {
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { DOCS_URL, DESKTOP_DOWNLOAD_URL } = await import('@/config/supportLinks');
    const { CLAUDE_CODE_DOCS, CLAUDE_CODE_DOCS_URL } = await import('@/lib/claudeCode');

    expect(DOCS_URL).toBe('/help');
    expect(DESKTOP_DOWNLOAD_URL).toBeNull();
    expect(CLAUDE_CODE_DOCS_URL).toBe('/help/concepts/claude-code/');

    const helpTargets = [
      `${DOCS_URL}/`,
      `${DOCS_URL}/whats-new/`,
      `${DOCS_URL}/changelog/`,
      ...Object.values(CLAUDE_CODE_DOCS),
    ];

    for (const url of helpTargets) {
      expect(url).not.toContain('langalpha.ai');
      expect(url.startsWith('/help/')).toBe(true);
    }

    expect(CLAUDE_CODE_DOCS).toEqual({
      howItRuns: '/help/concepts/claude-code/#how-it-runs',
      requirements: '/help/concepts/claude-code/#requirements',
      whereItRuns: '/help/concepts/claude-code/#where-it-runs-and-when-it-can-run',
      setUp: '/help/concepts/claude-code/#set-it-up',
      fromClaudeSignIn: '/help/concepts/claude-code/#moving-from-the-claude-sign-in',
    });
  });
});
