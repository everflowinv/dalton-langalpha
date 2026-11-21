import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import i18n from '@/i18n';
import { renderWithProviders } from '@/test/utils';
import { DuplicateCompanyNotice } from '../DuplicateCompanyNotice';

describe('DuplicateCompanyNotice', () => {
  afterEach(async () => {
    await i18n.changeLanguage('en-US');
  });

  it('renders readable and unreadable notices in English and Chinese without server copy', async () => {
    const onCloseModal = vi.fn();
    const onCreateAnyway = vi.fn();

    renderWithProviders(
      <DuplicateCompanyNotice
        requestedName="Anthropic"
        matches={[
          {
            workspace_id: 'ws-1',
            workspace_name: 'ANT',
            owner_name: 'Richard',
            lead_analyst_name: 'Richard',
            ticker: 'ANT',
            can_read: true,
          },
        ]}
        onCloseModal={onCloseModal}
        onCreateAnyway={onCreateAnyway}
      />,
    );

    const card = screen.getByRole('alert');
    expect(card).toHaveTextContent('Anthropic already has a workspace "ANT" (owner: Richard, lead: Richard).');
    expect(card).toHaveTextContent('Open this workspace');
    expect(card).toHaveTextContent('Create anyway');

    await i18n.changeLanguage('zh-CN');
    expect(card).toHaveTextContent('「Anthropic」已有工作区「ANT」（所有者 Richard，主分析师 Richard），请直接在那里继续。确实要另建，请确认后重试。');
    expect(card).toHaveTextContent('打开该工作区');
    expect(card).toHaveTextContent('仍然新建');
  });

  it('hides ticker and lead for unreadable matches and passes requestedName to onCreateAnyway', async () => {
    const user = userEvent.setup();
    const onCreateAnyway = vi.fn();

    renderWithProviders(
      <DuplicateCompanyNotice
        requestedName="Anthropic"
        matches={[
          {
            workspace_name: 'ANT',
            owner_name: 'CY',
            lead_analyst_name: 'CY',
            ticker: 'ANT',
            can_read: false,
          },
        ]}
        onCloseModal={vi.fn()}
        onCreateAnyway={onCreateAnyway}
      />,
    );

    const card = screen.getByRole('alert');
    expect(card).toHaveTextContent('Ask the owner to share access');
    expect(card).not.toHaveTextContent('Lead:');
    expect(card.querySelector('.cwm-dup-ticker')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Create anyway' }));
    expect(onCreateAnyway).toHaveBeenCalledWith('Anthropic');
  });
});
