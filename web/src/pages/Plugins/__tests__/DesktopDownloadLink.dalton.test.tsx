import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { canBeginMcpOAuth } from '@/lib/desktop';

vi.mock('@/lib/desktop', () => ({
  canBeginMcpOAuth: vi.fn<typeof canBeginMcpOAuth>(() => false),
}));

describe('DesktopDownloadLink & NativeOnlyNote in Dalton build', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('renders no download link when DESKTOP_DOWNLOAD_URL is null', async () => {
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { DesktopDownloadLink, NativeOnlyNote } = await import('../components/DesktopDownloadLink');
    const { container } = render(
      <>
        <DesktopDownloadLink />
        <NativeOnlyNote id="native-note" />
      </>,
    );

    expect(screen.queryByRole('link')).toBeNull();
    expect(container.querySelector('a')).toBeNull();
    expect(document.getElementById('native-note')).toBeInTheDocument();
  });
});
