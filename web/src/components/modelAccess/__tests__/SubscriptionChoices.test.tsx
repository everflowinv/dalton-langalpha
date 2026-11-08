/**
 * Settings lists Claude Code under the Claude sign-in, with where it stands;
 * the setup wizard, which passes nothing for it, does not.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { SubscriptionChoices } from '../SubscriptionChoices';

describe('SubscriptionChoices', () => {
  it('lists Claude Code right under Claude, with its own subtitle and status', () => {
    const onPick = vi.fn();
    renderWithProviders(
      <SubscriptionChoices planGroups={[]} configured={new Set()} onPickSignIn={vi.fn()} onPickPlan={vi.fn()} claudeCode={{ status: 'needs_computer', onPick }} />,
    );
    const names = screen.getAllByRole('button').map((b) => b.querySelector('.truncate')?.textContent);
    // DALTON-HOOK(native-oauth-ui): Subscription list includes Google Antigravity, Muse Code, and Grok (SuperGrok).
    expect(names).toEqual(['ChatGPT', 'Claude', 'Claude Code', 'Google Antigravity', 'Muse Code', 'Grok (SuperGrok)']);
    expect(screen.getByText('Claude Pro or Max, called by LangAlpha')).toBeInTheDocument();
    expect(screen.getByText('Claude Pro or Max, run on your computer')).toBeInTheDocument();
    fireEvent.click(screen.getByText('No computer'));
    expect(onPick).toHaveBeenCalled();
  });

  it('lists no Claude Code where none is passed', () => {
    renderWithProviders(<SubscriptionChoices planGroups={[]} configured={new Set()} onPickSignIn={vi.fn()} onPickPlan={vi.fn()} />);
    expect(screen.queryByText('Claude Code')).toBeNull();
  });
});
