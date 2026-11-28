/**
 * DALTON-HOOK(shared-widgets): a surface chooses which of an inline widget's
 * export actions it offers. The owner's view keeps all of them; a shared view
 * offers no new tab or PDF, which run the widget outside its sandbox on the
 * app's origin, and the HTML download only where the share allows downloads.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';

let barProps: Record<string, unknown> | null = null;

vi.mock('../html/HtmlActionBar', () => ({
  default: (props: Record<string, unknown>) => {
    barProps = props;
    return null;
  },
}));
vi.mock('../html/useHtmlSandbox', () => ({
  useHtmlSandbox: () => ({ height: 120, pushTheme: () => {} }),
}));

import InlineWidget from '../InlineWidget';

describe('InlineWidget export actions', () => {
  beforeEach(() => {
    barProps = null;
  });

  it('offers every export by default', () => {
    render(<InlineWidget html="<p>x</p>" title="Chart" base={null} />);
    expect(typeof barProps!.onFullscreen).toBe('function');
    expect(typeof barProps!.onDownload).toBe('function');
    expect(typeof barProps!.onExportPdf).toBe('function');
  });

  it('offers only fullscreen and the HTML download on a share that allows downloads', () => {
    render(<InlineWidget html="<p>x</p>" title="Chart" base={null} exports="download" />);
    expect(typeof barProps!.onFullscreen).toBe('function');
    expect(typeof barProps!.onDownload).toBe('function');
    expect(barProps!.onOpenInNewTab).toBeUndefined();
    expect(barProps!.onExportPdf).toBeUndefined();
  });

  it('offers only fullscreen on a share without downloads', () => {
    render(<InlineWidget html="<p>x</p>" title="Chart" base={null} exports="none" />);
    expect(typeof barProps!.onFullscreen).toBe('function');
    expect(barProps!.onOpenInNewTab).toBeUndefined();
    expect(barProps!.onDownload).toBeUndefined();
    expect(barProps!.onExportPdf).toBeUndefined();
  });
});
