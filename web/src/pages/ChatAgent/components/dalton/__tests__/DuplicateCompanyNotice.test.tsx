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

  it('renders unbound coverage unit notices in English and Chinese without workspace actions', async () => {
    renderWithProviders(
      <DuplicateCompanyNotice
        requestedName="Anthropic"
        matches={[
          {
            unbound_unit: true,
            display_name: 'Anthropic, PBC',
            ticker: 'ANT',
            lead_analyst_name: 'Richard',
            can_read: true,
          },
        ]}
        onCloseModal={vi.fn()}
        onCreateAnyway={vi.fn()}
      />,
    );

    const card = screen.getByRole('alert');
    expect(card).toHaveTextContent(
      '"Anthropic" is already registered as a team coverage company (lead: Richard) with no workspace bound yet.',
    );
    expect(card).toHaveTextContent('Anthropic, PBC');
    expect(card).toHaveTextContent('ANT');
    expect(card).toHaveTextContent('Lead: Richard');
    expect(card).toHaveTextContent('No workspace bound yet');
    expect(screen.queryByRole('button', { name: 'Open this workspace' })).toBeNull();
    expect(card).not.toHaveTextContent('Ask the owner to share access');

    await i18n.changeLanguage('zh-CN');
    expect(card).toHaveTextContent(
      '「Anthropic」已登记为团队覆盖公司（主分析师 Richard），尚未绑定工作区',
    );
    expect(card).toHaveTextContent('尚未绑定工作区');
    expect(card).toHaveTextContent('仍然新建');
  });
});
