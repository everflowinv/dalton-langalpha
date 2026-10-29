import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FilePreviewCard } from '../chat-input.parts';
import ChatInput from '../chat-input';
import ImageLightbox from '@/pages/ChatAgent/components/ImageLightbox';
import type { FileAttachment } from '../chat-input.types';

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ data: undefined, isLoading: false }),
}));

vi.mock('../use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
  toast: vi.fn(),
}));

const PREVIEW_SRC = 'data:image/png;base64,ZGlkYXRh';

const imageAttachment = (over: Partial<FileAttachment> = {}): FileAttachment => ({
  id: 'att-1',
  file: new File(['x'], 'cat.png', { type: 'image/png' }),
  type: 'image/png',
  preview: PREVIEW_SRC,
  uploadStatus: 'complete',
  dataUrl: null,
  ...over,
});

// The card and the lightbox both render an <img alt="cat.png">; the lightbox
// portals into document.body, so an open preview is the second one.
const images = () => screen.getAllByRole('img', { name: 'cat.png' });
const openButton = () => screen.getByRole('button', { name: 'Preview image' });
const closeButton = () => screen.getByRole('button', { name: 'Close' });

describe('FilePreviewCard image preview', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('opens the full-size image on a single click of the thumbnail', () => {
    render(<FilePreviewCard file={imageAttachment()} onRemove={vi.fn()} />);
    expect(images()).toHaveLength(1);
    fireEvent.click(openButton());
    const [thumbnail, fullSize] = images();
    expect(fullSize).not.toBe(thumbnail);
    expect(fullSize).toHaveAttribute('src', PREVIEW_SRC);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('closes the preview on Escape', () => {
    render(<FilePreviewCard file={imageAttachment()} onRemove={vi.fn()} />);
    fireEvent.click(openButton());
    expect(images()).toHaveLength(2);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(images()).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
  });

  it('removes the attachment without opening the preview', () => {
    const onRemove = vi.fn();
    render(<FilePreviewCard file={imageAttachment()} onRemove={onRemove} />);
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    expect(onRemove).toHaveBeenCalledWith('att-1');
    expect(images()).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
  });

  it('keeps non-image attachments inert on click', () => {
    const attachment = imageAttachment({
      file: new File(['x'], 'notes.txt', { type: 'text/plain' }),
      type: 'text/plain',
      preview: null,
    });
    render(<FilePreviewCard file={attachment} onRemove={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Preview image' })).toBeNull();
    fireEvent.click(screen.getByText('notes.txt'));
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
  });

  it('opens the preview from the keyboard (Enter, then Space)', async () => {
    render(<FilePreviewCard file={imageAttachment()} onRemove={vi.fn()} />);
    openButton().focus();
    await userEvent.keyboard('{Enter}');
    expect(images()).toHaveLength(2);
    await userEvent.keyboard('{Escape}');
    expect(images()).toHaveLength(1);
    openButton().focus();
    // A literal space: `{Space}` names the key "Space", which no button
    // answers to — the character is the space bar's real key value.
    await userEvent.keyboard(' ');
    expect(images()).toHaveLength(2);
  });

  it('still previews while the upload is in flight', () => {
    render(
      <FilePreviewCard
        file={imageAttachment({ uploadStatus: 'uploading' })}
        onRemove={vi.fn()}
      />,
    );
    fireEvent.click(openButton());
    expect(images()).toHaveLength(2);
  });

  it('paints the focus indicator above the image as its own layer', () => {
    render(<FilePreviewCard file={imageAttachment()} onRemove={vi.fn()} />);
    const button = openButton();
    const image = button.querySelector('img')!;
    const focusLayer = Array.from(button.querySelectorAll('span')).find((s) =>
      s.className.includes('group-focus-visible/preview:opacity-100'),
    )!;
    expect(focusLayer).toBeTruthy();
    expect(focusLayer.className).toContain('pointer-events-none');
    // DOM order: the layer renders after the image, so it paints above it —
    // a ring on the button itself is a box-shadow under the image.
    expect(
      image.compareDocumentPosition(focusLayer) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    // The button claims no ring of its own anymore.
    expect(button.className).not.toContain('focus-visible:ring-');
  });
});

describe('ImageLightbox without trapFocus (workspace images, file panel)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('keeps focus where it was and still names itself a dialog', () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    render(<ImageLightbox src={PREVIEW_SRC} alt="cat.png" open onClose={vi.fn()} />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'cat.png' })).toBeInTheDocument();
    // No aria-modal and no focus trap: an outer Radix dialog owns focus here.
    expect(screen.getByRole('dialog')).not.toHaveAttribute('aria-modal');
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it('closes on Escape without a focus trap', () => {
    const onClose = vi.fn();
    render(<ImageLightbox src={PREVIEW_SRC} alt="cat.png" open onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not close when the image itself is clicked', () => {
    const onClose = vi.fn();
    render(<ImageLightbox src={PREVIEW_SRC} alt="cat.png" open onClose={onClose} />);
    fireEvent.click(screen.getByRole('img', { name: 'cat.png' }));
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('image preview against the composer container', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  // The composer's own container: its click handler focuses the textarea
  // (chat-input.tsx), which is exactly what an open preview must not let
  // through. An image is staged the way a user would, by pasting it.
  function renderComposerWithImage() {
    const onSend = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatInput onSend={onSend} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:cat');
    fireEvent.paste(textarea, {
      clipboardData: {
        items: [{ kind: 'file', getAsFile: () => new File(['x'], 'cat.png', { type: 'image/png' }) }],
        getData: () => '',
      },
    });
    return { onSend, textarea };
  }

  it('takes focus from the composer and Enter does not send', async () => {
    const { onSend, textarea } = renderComposerWithImage();
    await userEvent.click(openButton());
    expect(document.activeElement).not.toBe(textarea);
    expect(document.activeElement).toBe(closeButton());
    await userEvent.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();
  });

  it('returns focus to the thumbnail when the preview closes', async () => {
    const { textarea } = renderComposerWithImage();
    const thumb = openButton();
    await userEvent.click(thumb);
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
    expect(document.activeElement).toBe(thumb);
    expect(document.activeElement).not.toBe(textarea);
  });
});
