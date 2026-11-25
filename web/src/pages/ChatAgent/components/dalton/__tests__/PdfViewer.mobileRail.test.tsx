import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PdfViewer from '../../viewers/PdfViewer';

const mediaState = vi.hoisted(() => ({ mobile: true }));
const docCapture = vi.hoisted(() => ({
  onLoadSuccess: null as ((pdf: unknown) => void) | null,
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => mediaState.mobile,
  getIsMobileSnapshot: () => mediaState.mobile,
}));

vi.mock('react-pdf', () => ({
  pdfjs: {
    GlobalWorkerOptions: {},
    version: 'test',
  },
  Document: ({
    onLoadSuccess,
    children,
  }: {
    onLoadSuccess: (pdf: unknown) => void;
    children: ReactNode;
  }) => {
    docCapture.onLoadSuccess = onLoadSuccess;
    return <div>{children}</div>;
  },
  Page: () => <div data-testid="mock-pdf-page" />,
  Outline: () => null,
}));

vi.mock('../../viewers/pdf/PdfRail', () => ({
  default: ({ onGoToPage }: { onGoToPage: (index: number) => void }) => (
    <div className="pdf-rail" data-testid="mock-pdf-rail">
      <button type="button" data-testid="mock-pdf-pick-page" onClick={() => onGoToPage(1)}>
        Page 2
      </button>
    </div>
  ),
}));

function fakePdf() {
  return {
    numPages: 3,
    loadingParams: { disableAutoFetch: false },
    getPage: vi.fn(async () => ({
      getViewport: () => ({ width: 612, height: 792 }),
    })),
    getOutline: vi.fn(async () => null),
  };
}

describe('PdfViewer mobile floating rail overlay', () => {
  beforeEach(() => {
    mediaState.mobile = true;
    docCapture.onLoadSuccess = null;
  });

  it('closes floating rail on inner-element Escape without bubbling to FilePanel, and restores focus to Pages toggle on Escape and page pick', async () => {
    const parentKeyDown = vi.fn();
    const fileData = new Uint8Array([37, 80, 68, 70]).buffer;
    render(
      <div onKeyDown={parentKeyDown}>
        <PdfViewer data={fileData} />
      </div>,
    );

    await act(async () => {
      docCapture.onLoadSuccess?.(fakePdf());
    });

    const toggleBtn = await screen.findByRole('button', { name: /Pages|页面|Sidebar/i });
    await waitFor(() => expect(toggleBtn).toBeEnabled());

    // Open rail -> focus inner thumbnail button -> press Escape on the inner element
    fireEvent.click(toggleBtn);
    expect(screen.getByTestId('pdf-rail-backdrop')).toBeInTheDocument();
    const pickBtn = screen.getByTestId('mock-pdf-pick-page');
    pickBtn.focus();
    expect(pickBtn).toHaveFocus();

    fireEvent.keyDown(pickBtn, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('pdf-rail-backdrop')).toBeNull();
      expect(screen.queryByTestId('mock-pdf-rail')).toBeNull();
    });
    // Escape must be stopped inside PdfViewer so FilePanel does not open the file tree
    expect(parentKeyDown).not.toHaveBeenCalled();
    // Focus returns to the Pages toggle button (never drops to document.body)
    expect(toggleBtn).toHaveFocus();

    // Re-open rail -> focus inner thumbnail button -> select page -> rail closes and focus returns to toggleBtn
    fireEvent.click(toggleBtn);
    const pickBtnAgain = screen.getByTestId('mock-pdf-pick-page');
    pickBtnAgain.focus();
    fireEvent.click(pickBtnAgain);
    await waitFor(() => {
      expect(screen.queryByTestId('pdf-rail-backdrop')).toBeNull();
      expect(screen.queryByTestId('mock-pdf-rail')).toBeNull();
    });
    expect(toggleBtn).toHaveFocus();

    // Re-open rail -> click backdrop -> rail closes and focus returns to toggleBtn
    fireEvent.click(toggleBtn);
    const backdrop = screen.getByTestId('pdf-rail-backdrop');
    screen.getByTestId('mock-pdf-pick-page').focus();
    fireEvent.click(backdrop);
    await waitFor(() => {
      expect(screen.queryByTestId('pdf-rail-backdrop')).toBeNull();
      expect(screen.queryByTestId('mock-pdf-rail')).toBeNull();
    });
    expect(toggleBtn).toHaveFocus();
  });

  it('keeps rail open after picking a page on desktop and never renders mobile backdrop', async () => {
    mediaState.mobile = false;
    const fileData = new Uint8Array([37, 80, 68, 70]).buffer;
    render(<PdfViewer data={fileData} />);

    await act(async () => {
      docCapture.onLoadSuccess?.(fakePdf());
    });

    const toggleBtn = await screen.findByRole('button', { name: /Pages|页面|Sidebar/i });
    await waitFor(() => expect(toggleBtn).toBeEnabled());

    fireEvent.click(toggleBtn);
    expect(screen.queryByTestId('pdf-rail-backdrop')).toBeNull();
    expect(screen.getByTestId('mock-pdf-rail')).toBeInTheDocument();

    // On desktop, selecting a page leaves the side rail open
    fireEvent.click(screen.getByTestId('mock-pdf-pick-page'));
    expect(screen.getByTestId('mock-pdf-rail')).toBeInTheDocument();
  });
});
