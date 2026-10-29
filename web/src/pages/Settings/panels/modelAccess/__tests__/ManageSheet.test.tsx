/**
 * The deployment's own key has no row of the user's, so removing it would
 * delete nothing and the row would come straight back: the sheet offers no
 * removal for it.
 */
import { describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';

vi.mock('@/components/modelAccess/useModelPicker', () => ({
  useModelPicker: () => ({ loading: false, saving: false }),
}));
vi.mock('@/components/modelAccess/ModelPicker', () => ({ ModelPickerList: () => null }));
// DALTON-HOOK(team-credentials): the sheet's remove, reconnect, and key-change controls respect the PM-only guard
let teamCredsMock = {
  isShared: false,
  canDisconnect: true,
  canManage: true,
  isPm: true,
  canInvite: false,
};

vi.mock('@/config/daltonAuth', () => ({
  isDaltonAuth: true,
}));
vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: 'team hint',
  useTeamCredentials: () => teamCredsMock,
}));
vi.mock('@/components/modelAccess/SubscriptionSummary', () => ({
  SubscriptionSummary: () => <div data-testid="subscription-summary" />,
}));

import { ManageSheet, SubscriptionFlow } from '../ManageSheet';
import { KeySheet } from '../KeySheet';
import { optionsOf } from '@/components/modelAccess/providerOptions';

const key = (masked_key: string): ConfiguredProvider => ({
  provider: 'deepseek', display_name: 'DeepSeek', access_type: 'api_key', masked_key,
});

const subscription = (state: ConfiguredProvider['state'] = 'connected'): ConfiguredProvider => ({
  provider: 'codex-oauth',
  display_name: 'ChatGPT',
  access_type: 'oauth',
  state,
});

const sampleKeyOption = optionsOf({
  provider: 'deepseek',
  display_name: 'DeepSeek',
  access_type: 'api_key',
  brand_key: 'deepseek',
  byok_eligible: true,
  base_url: null,
})[0];

function removeButton(p: ConfiguredProvider) {
  renderWithProviders(
    <ManageSheet p={p} option={null} configured={[p]} onClose={() => {}} onRemove={() => {}} />,
  );
  return screen.queryByRole('button', { name: 'Remove key' });
}

describe('ManageSheet', () => {
  it('offers to remove a key the user saved', () => {
    teamCredsMock = { isShared: false, canDisconnect: true, canManage: true, isPm: true, canInvite: false };
    expect(removeButton(key('sk-...7e48'))).not.toBeNull();
  });

  it("offers no removal for the deployment's own key", () => {
    teamCredsMock = { isShared: false, canDisconnect: true, canManage: true, isPm: true, canInvite: false };
    expect(removeButton(key('(server)'))).toBeNull();
  });

  // DALTON-HOOK(team-credentials): non-admin under team sharing sees no write/disconnect buttons or inputs
  it('hides key change, remove, and reconnect actions for non-admin in team mode', () => {
    teamCredsMock = { isShared: true, canDisconnect: false, canManage: false, isPm: false, canInvite: false };
    const apiKeyProvider = key('sk-...7e48');
    const { unmount } = renderWithProviders(
      <ManageSheet
        p={apiKeyProvider}
        option={sampleKeyOption}
        configured={[apiKeyProvider]}
        onClose={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.queryByRole('button', { name: 'Remove key' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Change' })).toBeNull();
    unmount();

    const subProvider = subscription('connected');
    const { unmount: unmountSub } = renderWithProviders(
      <ManageSheet
        p={subProvider}
        option={null}
        configured={[subProvider]}
        onClose={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull();
    unmountSub();
  });

  it('shows teamManagedByAdmin note instead of OAuth connect flow or API key inputs for non-admin', () => {
    teamCredsMock = { isShared: true, canDisconnect: false, canManage: false, isPm: false, canInvite: false };
    const reauthSub = subscription('needs_reauth');
    const { unmount } = renderWithProviders(
      <ManageSheet
        p={reauthSub}
        option={null}
        configured={[reauthSub]}
        onClose={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(
      screen.getByText(/Team-shared credentials are managed by the administrator|团队共用凭据由管理员管理/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull();
    unmount();

    const { unmount: unmountKeySheet } = renderWithProviders(
      <KeySheet option={sampleKeyOption} configured={[]} onSaved={() => {}} />,
    );
    expect(
      screen.getByText(/Team-shared credentials are managed by the administrator|团队共用凭据由管理员管理/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save key' })).toBeNull();
    unmountKeySheet();

    const { unmount: unmountFlow } = renderWithProviders(
      <SubscriptionFlow provider="codex-oauth" onConnected={() => {}} />,
    );
    expect(
      screen.getByText(/Team-shared credentials are managed by the administrator|团队共用凭据由管理员管理/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    unmountFlow();
  });
});
