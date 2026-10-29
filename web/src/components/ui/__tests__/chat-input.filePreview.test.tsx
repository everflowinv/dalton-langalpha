import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FilePreviewCard } from '../chat-input.parts';
import type { FileAttachment } from '../chat-input.types';

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

describe('FilePreviewCard image preview', () => {
  it('opens the full-size image on a single click of the thumbnail', () => {
    render(<FilePreviewCard file={imageAttachment()} onRemove={vi.fn()} />);
    expect(images()).toHaveLength(1);
    fireEvent.click(openButton());
    const [thumbnail, fullSize] = images();
    expect(fullSize).not.toBe(thumbnail);
    expect(fullSize).toHaveAttribute('src', PREVIEW_SRC);
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
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

  it('opens the preview with the keyboard (Enter)', async () => {
    render(<FilePreviewCard file={imageAttachment()} onRemove={vi.fn()} />);
    await userEvent.type(openButton(), '{Enter}');
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
});
