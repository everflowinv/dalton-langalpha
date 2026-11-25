import { describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { DaltonActionConfirmButton } from '../DaltonActionConfirmButton';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => false,
}));

describe('DaltonActionConfirmButton', () => {
  it('disables the open ConfirmDialog confirm button and blocks onConfirm when disabled flips true', async () => {
    const onConfirm = vi.fn();
    const { rerender } = render(
      <DaltonActionConfirmButton
        label="Revoke"
        confirmTitle="Revoke invite"
        confirmMessage="Are you sure?"
        confirmLabel="Confirm revoke"
        onConfirm={onConfirm}
        testId="trigger-btn"
      />,
    );

    fireEvent.click(screen.getByTestId('trigger-btn'));
    const confirmBtn = await screen.findByRole('button', { name: 'Confirm revoke' });
    expect(confirmBtn).toBeEnabled();

    // Parent enters disabled/confirming state while the dialog is already open.
    rerender(
      <DaltonActionConfirmButton
        label="Revoke"
        confirmTitle="Revoke invite"
        confirmMessage="Are you sure?"
        confirmLabel="Confirm revoke"
        disabled
        onConfirm={onConfirm}
        testId="trigger-btn"
      />,
    );

    expect(confirmBtn).toBeDisabled();
    fireEvent.click(confirmBtn);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('disables the open ConfirmStrip confirm button and blocks onConfirm when disabled flips true', async () => {
    const onConfirm = vi.fn();
    const { rerender } = render(
      <DaltonActionConfirmButton
        mode="strip"
        label="Delete"
        confirmTitle="Delete item"
        confirmMessage="Delete this item?"
        confirmLabel="Confirm delete"
        onConfirm={onConfirm}
        testId="strip-trigger-btn"
      />,
    );

    fireEvent.click(screen.getByTestId('strip-trigger-btn'));
    const confirmBtn = await screen.findByRole('button', { name: 'Confirm delete' });
    expect(confirmBtn).toBeEnabled();

    rerender(
      <DaltonActionConfirmButton
        mode="strip"
        label="Delete"
        confirmTitle="Delete item"
        confirmMessage="Delete this item?"
        confirmLabel="Confirm delete"
        disabled
        onConfirm={onConfirm}
        testId="strip-trigger-btn"
      />,
    );

    expect(confirmBtn).toBeDisabled();
    fireEvent.click(confirmBtn);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
