import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { renderWithProviders } from '@/test/utils';

const h = vi.hoisted(() => ({
  isMobile: false,
  startWorkspace: vi.fn(),
  uploadWorkspaceFile: vi.fn(),
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => h.isMobile,
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: null, isLoading: false, isError: false }),
}));

vi.mock('../../utils/api', async () => {
  const { computersApi } = await import('./computersDialogApi');
  return {
    ...(await computersApi()),
    uploadWorkspaceFile: (...args: unknown[]) => h.uploadWorkspaceFile(...args),
    startWorkspace: (...args: unknown[]) => h.startWorkspace(...args),
    setComputerSpec: vi.fn(),
  };
});

vi.mock('@/components/ui/use-toast', () => ({
  toast: vi.fn(),
}));

vi.mock('@/lib/dalton/coverage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/dalton/coverage')>()),
  useDaltonCoverageUnits: () => ({ data: { enabled: false, units: [] }, isError: false }),
}));

import CreateWorkspaceModal from '../CreateWorkspaceModal';

describe('CreateWorkspaceModal mobile keyboard and dialog accessibility', () => {
  beforeEach(() => {
    h.isMobile = false;
    h.startWorkspace.mockReset();
    h.uploadWorkspaceFile.mockReset();
  });

  it('skips autoFocus on mobile, moves focus to a non-input control inside the dialog, wires aria-labelledby, and traps Tab', () => {
    h.isMobile = true;
    renderWithProviders(
      <>
        <button type="button">Background action</button>
        <CreateWorkspaceModal isOpen onClose={vi.fn()} onCreate={vi.fn()} />
      </>,
    );

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('data-sheet', '');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const labelledBy = dialog.getAttribute('aria-labelledby');
    expect(labelledBy).toBeTruthy();
    const heading = document.getElementById(labelledBy!);
    expect(heading?.tagName).toBe('H2');
    expect(dialog).toHaveAccessibleName(heading!.textContent ?? '');

    const nameInput = screen.getByPlaceholderText(/Enter workspace name|输入工作区名称/i);
    expect(document.activeElement).not.toBe(nameInput);
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement?.tagName).toBe('BUTTON');

    // Shift+Tab from the first control wraps to the last focusable inside the modal instead of escaping to background.
    const closeBtn = dialog.querySelector<HTMLButtonElement>('.cwm-close-btn')!;
    const cancelBtn = dialog.querySelector<HTMLButtonElement>('.cwm-btn-cancel')!;
    expect(document.activeElement).toBe(closeBtn);

    fireEvent.keyDown(closeBtn, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(cancelBtn);

    // Tab from the last control wraps back to the first control inside the modal.
    fireEvent.keyDown(cancelBtn, { key: 'Tab' });
    expect(document.activeElement).toBe(closeBtn);
  });

  it('preserves desktop focus on the name input, omits data-sheet, wires aria-labelledby, and traps Tab', () => {
    h.isMobile = false;
    renderWithProviders(
      <>
        <button type="button">Background action</button>
        <CreateWorkspaceModal isOpen onClose={vi.fn()} onCreate={vi.fn()} />
      </>,
    );

    const dialog = screen.getByRole('dialog');
    expect(dialog).not.toHaveAttribute('data-sheet');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAttribute('aria-labelledby');

    const nameInput = screen.getByPlaceholderText(/Enter workspace name|输入工作区名称/i);
    expect(document.activeElement).toBe(nameInput);

    const closeBtn = dialog.querySelector<HTMLButtonElement>('.cwm-close-btn')!;
    const cancelBtn = dialog.querySelector<HTMLButtonElement>('.cwm-btn-cancel')!;
    cancelBtn.focus();
    fireEvent.keyDown(cancelBtn, { key: 'Tab' });
    expect(document.activeElement).toBe(closeBtn);
  });

  it.each([
    ['mobile', true],
    ['desktop', false],
  ])('restores focus to the opener button when closed on %s', (_, mobile) => {
    h.isMobile = mobile;
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open modal</button>
          <CreateWorkspaceModal isOpen={open} onClose={() => setOpen(false)} onCreate={vi.fn()} />
        </>
      );
    }

    renderWithProviders(<Harness />);
    const opener = screen.getByRole('button', { name: 'Open modal' });
    opener.focus();
    fireEvent.click(opener);

    const dialog = screen.getByRole('dialog');
    expect(dialog.contains(document.activeElement)).toBe(true);
    if (!mobile) {
      const nameInput = screen.getByPlaceholderText(/Enter workspace name|输入工作区名称/i);
      expect(document.activeElement).toBe(nameInput);
    }

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it('returns focus through the real CreateWorkspaceHost when a Safari click never focused the opener', async () => {
    h.isMobile = false;
    const { CreateWorkspaceHost, useOpenCreateWorkspace } = await import('@/components/CreateWorkspaceHost');
    function Opener() {
      const open = useOpenCreateWorkspace();
      return <button type="button" onClick={() => open?.()}>Open modal</button>;
    }

    renderWithProviders(<CreateWorkspaceHost><Opener /></CreateWorkspaceHost>);
    const opener = screen.getByRole('button', { name: 'Open modal' });
    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).toBe(document.body);
    // Safari: the press reaches the button but focus stays on <body>.
    fireEvent.pointerDown(opener);
    fireEvent.click(opener);
    const dialog = await screen.findByRole('dialog');
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(opener);
  });

  it('keeps focus inside the modal container when transitioning from form to upload progress phase', async () => {
    h.isMobile = false;
    // Keep startWorkspace pending so the modal stays in the uploading progress phase (where close button is hidden).
    h.startWorkspace.mockImplementation(() => new Promise(() => {}));
    const onCreate = vi.fn().mockResolvedValue({
      workspace_id: 'ws-progress-1',
      user_id: 'u1',
      name: 'Upload WS',
      description: '',
      status: 'running',
      config: {},
      is_pinned: false,
      sort_order: 0,
      resource_tier: 'standard',
      is_always_on: false,
    });

    renderWithProviders(
      <CreateWorkspaceModal isOpen onClose={vi.fn()} onCreate={onCreate} />,
    );

    const nameInput = screen.getByPlaceholderText(/Enter workspace name|输入工作区名称/i);
    fireEvent.change(nameInput, { target: { value: 'Upload WS' } });

    const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    const file = new File(['hello'], 'notes.txt', { type: 'text/plain' });
    fireEvent.change(fileInput, { target: { files: [file] } });

    const createBtn = screen.getByRole('button', { name: /^(Create|创建)$/i });
    createBtn.focus();
    fireEvent.click(createBtn);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Uploading files|正在上传文件/i })).toBeInTheDocument();
    });

    const progressDialog = screen.getByRole('dialog');
    expect(progressDialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(progressDialog);
  });
});
