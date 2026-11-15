/**
 * The light and dark row is drawn as Mode under the Theme heading, and people
 * still look for it as the theme, in either language.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import i18n from '@/i18n';
import { renderWithProviders } from '@/test/utils';

// DALTON-HOOK(team-credentials): the rail reads shared-credential status
// through useTeamCredentials, whose hook calls useAuth, and the sharing
// query needs the test QueryClient. These tests search the theme row, so
// the auth context answers the logged-out shape.
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ userId: null, logout: vi.fn() }),
}));

import { SettingsRail } from '../components/SettingsRail';

afterEach(async () => {
  await i18n.changeLanguage('en-US');
});

function search(query: string) {
  renderWithProviders(<SettingsRail section="general" />);
  fireEvent.change(screen.getByRole('combobox'), { target: { value: query } });
  return within(screen.getByRole('listbox'));
}

describe('searching Settings for the theme', () => {
  it('finds the Mode row by "theme"', () => {
    expect(search('theme').getByText('Mode')).toBeTruthy();
  });

  it('finds the Mode row by 主题', async () => {
    await i18n.changeLanguage('zh-CN');
    expect(search('主题').getByText('模式')).toBeTruthy();
  });
});
