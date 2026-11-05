import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import {
  formatAccessLevelZh,
  WorkspaceSharingModal,
  type WorkspaceSettingsResponse,
  type WorkspaceSharingResponse,
} from '../WorkspaceSharingModal';

const WS_OWN = '11111111-1111-4111-8111-111111111111';
const WS_COLLEAGUE = '22222222-2222-4222-8222-222222222222';
const WS_HOME = '99999999-9999-4999-8999-999999999999';

const h = vi.hoisted(() => ({
  fetchWorkspaceSharing: vi.fn<(wsId: string) => Promise<WorkspaceSharingResponse>>(),
  updateWorkspaceSharing: vi.fn<
    (
      wsId: string,
      payload: { cos_access?: 'rw' | 'ro' | 'hidden'; web_access: 'rw' | 'ro' | 'hidden' },
    ) => Promise<WorkspaceSharingResponse>
  >(),
  fetchWorkspaceSettings: vi.fn<(wsId: string) => Promise<WorkspaceSettingsResponse>>(),
  updateWorkspaceSettings: vi.fn<
    (
      wsId: string,
      payload: { team_memory_enabled: boolean },
    ) => Promise<WorkspaceSettingsResponse>
  >(),
}));

vi.mock('../../../utils/api/workspaces', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../utils/api/workspaces')>();
  return {
    ...actual,
    fetchWorkspaceSharing: (wsId: string) => h.fetchWorkspaceSharing(wsId),
    updateWorkspaceSharing: (
      wsId: string,
      payload: { cos_access?: 'rw' | 'ro' | 'hidden'; web_access: 'rw' | 'ro' | 'hidden' },
    ) => h.updateWorkspaceSharing(wsId, payload),
    fetchWorkspaceSettings: (wsId: string) => h.fetchWorkspaceSettings(wsId),
    updateWorkspaceSettings: (
      wsId: string,
      payload: { team_memory_enabled: boolean },
    ) => h.updateWorkspaceSettings(wsId, payload),
  };
});

const STABLE_OWN_SETTINGS: WorkspaceSettingsResponse = {
  workspace_id: WS_OWN,
  team_memory_enabled: false,
  is_home: false,
  can_edit: true,
  owner_external: false,
  updated_by: null,
  updated_at: null,
};

const STABLE_OWN_SHARING: WorkspaceSharingResponse = {
  workspace_id: WS_OWN,
  cos_access: 'ro',
  web_access: 'hidden',
  is_home: false,
  can_edit: true,
  colleague_dispatch_enabled: true,
  updated_by: 'u-chao',
  updated_at: '2026-04-20T12:00:00Z',
};

const STABLE_COLLEAGUE_SHARING: WorkspaceSharingResponse = {
  workspace_id: WS_COLLEAGUE,
  cos_access: 'ro',
  web_access: 'ro',
  is_home: false,
  can_edit: false,
  colleague_dispatch_enabled: true,
  updated_by: 'u-richard',
  updated_at: '2026-04-20T12:00:00Z',
};

const STABLE_HOME_SHARING: WorkspaceSharingResponse = {
  workspace_id: WS_HOME,
  cos_access: 'hidden',
  web_access: 'hidden',
  is_home: true,
  can_edit: false,
  updated_by: null,
  updated_at: null,
};

describe('WorkspaceSharingModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.fetchWorkspaceSharing.mockResolvedValue(STABLE_OWN_SHARING);
    h.fetchWorkspaceSettings.mockResolvedValue(STABLE_OWN_SETTINGS);
  });

  it('portals ModalShell directly under document.body, uses design tokens, and closes on Escape, backdrop click, close button, and cancel', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_OWN), STABLE_OWN_SHARING);

    renderWithProviders(
      <div className="enter-fade-up" style={{ transform: 'translateY(0px)' }}>
        <WorkspaceSharingModal workspaceId={WS_OWN} />
      </div>,
      { queryClient },
    );

    const control = screen.getByTestId('workspace-sharing-control');
    const trigger = screen.getByTestId('workspace-sharing-button');
    fireEvent.click(trigger);

    const roleDialog = screen.getByRole('dialog');
    const modalContent = screen.getByTestId('workspace-sharing-modal');
    expect(modalContent).toBeInTheDocument();
    expect(roleDialog).toContainElement(modalContent);

    // Rendered via portal under document.body, not inside the transformed header container
    expect(control.contains(roleDialog)).toBe(false);
    const backdrop = roleDialog.parentElement as HTMLElement;
    expect(backdrop).not.toBeNull();
    expect(backdrop.parentElement).toBe(document.body);

    // Uses design tokens for overlay and primary button
    expect(backdrop.style.backgroundColor).toBe('var(--color-bg-overlay-strong)');
    expect(backdrop.className).not.toContain('bg-black/50');
    const saveBtn = screen.getByTestId('sharing-save-btn');
    expect(saveBtn.style.backgroundColor).toBe('var(--color-btn-primary-bg)');
    expect(saveBtn.style.color).toBe('var(--color-btn-primary-text)');

    // 1. Escape closes the dialog
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();

    // 2. Backdrop click closes the dialog
    fireEvent.click(trigger);
    const backdrop2 = screen.getByRole('dialog').parentElement as HTMLElement;
    fireEvent.mouseDown(backdrop2, { button: 0, clientX: 8, clientY: 8 });
    fireEvent.mouseUp(backdrop2, { button: 0, clientX: 8, clientY: 8 });
    fireEvent.click(backdrop2, { button: 0, clientX: 8, clientY: 8 });
    expect(screen.queryByRole('dialog')).toBeNull();

    // 3. ModalShell close button closes the dialog
    fireEvent.click(trigger);
    const closeBtn = screen.getByRole('button', { name: /关闭|close/i });
    fireEvent.click(closeBtn);
    expect(screen.queryByRole('dialog')).toBeNull();

    // 4. Cancel button closes the dialog
    fireEvent.click(trigger);
    fireEvent.click(screen.getByTestId('sharing-cancel-btn'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('cannot be closed by Escape, backdrop, close or cancel while a save is pending', async () => {
    let resolveSave: (value: WorkspaceSharingResponse) => void = () => {};
    h.updateWorkspaceSharing.mockImplementationOnce(
      () => new Promise<WorkspaceSharingResponse>((resolve) => { resolveSave = resolve; }),
    );
    const queryClient = createTestQueryClient();
    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_OWN} />, { queryClient });

    fireEvent.click(await screen.findByTestId('workspace-sharing-button'));
    fireEvent.click(screen.getByTestId('sharing-web-rw'));
    fireEvent.click(screen.getByTestId('sharing-save-btn'));
    await waitFor(() => expect(h.updateWorkspaceSharing).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId('sharing-cancel-btn')).toBeDisabled());

    const dialog = screen.getByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    fireEvent.keyDown(document, { key: 'Escape' });
    const backdrop = dialog.parentElement as HTMLElement;
    fireEvent.mouseDown(backdrop, { button: 0, clientX: 8, clientY: 8 });
    fireEvent.mouseUp(backdrop, { button: 0, clientX: 8, clientY: 8 });
    fireEvent.click(backdrop, { button: 0, clientX: 8, clientY: 8 });
    fireEvent.click(screen.getByTestId('sharing-cancel-btn'));
    const closeBtn = screen.queryByRole('button', { name: /关闭|close/i });
    if (closeBtn) fireEvent.click(closeBtn);
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    resolveSave({ ...STABLE_OWN_SHARING, web_access: 'rw' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('loads initial access levels, shows settings hint and rw trust warning, saves updated web_access, updates cache, and closes', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    const updatedSharing: WorkspaceSharingResponse = {
      ...STABLE_OWN_SHARING,
      web_access: 'rw',
      updated_at: '2026-04-20T12:05:00Z',
    };
    h.updateWorkspaceSharing.mockResolvedValueOnce(updatedSharing);

    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_OWN} />, {
      queryClient,
    });

    // Initially loading before query resolves -> not rendered yet
    expect(screen.queryByTestId('workspace-sharing-button')).toBeNull();

    const trigger = await screen.findByTestId('workspace-sharing-button');
    expect(h.fetchWorkspaceSharing).toHaveBeenCalledWith(WS_OWN);
    fireEvent.click(trigger);

    expect(screen.queryByTestId('sharing-cos-fieldset')).toBeNull();
    expect(screen.getByTestId('sharing-cos-settings-hint')).toHaveTextContent(
      '其他人的研究总监访问权限已在「设置」中按账户统一管理',
    );
    expect(screen.queryByTestId('sharing-web-rw-warning')).toBeNull();

    const webRw = screen.getByTestId('sharing-web-rw') as HTMLInputElement;
    const webRo = screen.getByTestId('sharing-web-ro') as HTMLInputElement;
    const webHidden = screen.getByTestId('sharing-web-hidden') as HTMLInputElement;

    expect(webHidden.checked).toBe(true);
    expect(webRo.checked).toBe(false);

    fireEvent.click(webRw);
    expect(webRw.checked).toBe(true);
    expect(screen.getByTestId('sharing-web-rw-warning')).toHaveTextContent(
      '同事在这里提问会在你的计算机上运行，等于信任这台计算机',
    );
    fireEvent.click(screen.getByTestId('sharing-save-btn'));

    await waitFor(() => {
      expect(h.updateWorkspaceSharing).toHaveBeenCalledWith(WS_OWN, {
        web_access: 'rw',
      });
    });
    expect(queryClient.getQueryData(queryKeys.dalton.sharing(WS_OWN))).toEqual(updatedSharing);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspaces.team() });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  it('keeps workspace-level cos_access radio fieldset when colleague_dispatch_enabled is false', async () => {
    const legacySharing: WorkspaceSharingResponse = {
      ...STABLE_OWN_SHARING,
      cos_access: 'ro',
      web_access: 'hidden',
      colleague_dispatch_enabled: false,
    };
    const updatedLegacySharing: WorkspaceSharingResponse = {
      ...legacySharing,
      cos_access: 'hidden',
      web_access: 'ro',
    };
    h.fetchWorkspaceSharing.mockResolvedValueOnce(legacySharing);
    h.updateWorkspaceSharing.mockResolvedValueOnce(updatedLegacySharing);

    const queryClient = createTestQueryClient();
    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_OWN} />, {
      queryClient,
    });

    const trigger = await screen.findByTestId('workspace-sharing-button');
    fireEvent.click(trigger);

    expect(screen.queryByTestId('sharing-cos-settings-hint')).toBeNull();
    expect(screen.getByTestId('sharing-cos-fieldset')).toBeInTheDocument();

    const cosRo = screen.getByTestId('sharing-cos-ro') as HTMLInputElement;
    const cosHidden = screen.getByTestId('sharing-cos-hidden') as HTMLInputElement;
    const webRo = screen.getByTestId('sharing-web-ro') as HTMLInputElement;

    expect(cosRo.checked).toBe(true);
    fireEvent.click(cosHidden);
    fireEvent.click(webRo);
    fireEvent.click(screen.getByTestId('sharing-save-btn'));

    await waitFor(() => {
      expect(h.updateWorkspaceSharing).toHaveBeenCalledWith(WS_OWN, {
        cos_access: 'hidden',
        web_access: 'ro',
      });
    });
  });

  it('disables radio groups and save button for a colleague workspace without edit rights, and hides on Home', async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_COLLEAGUE), STABLE_COLLEAGUE_SHARING);
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_HOME), STABLE_HOME_SHARING);

    // Home workspace renders nothing
    const { unmount: unmountHome } = renderWithProviders(
      <WorkspaceSharingModal workspaceId={WS_HOME} />,
      { queryClient },
    );
    expect(screen.queryByTestId('workspace-sharing-control')).toBeNull();
    unmountHome();

    // Colleague read-only workspace renders trigger, but disables editing and saving inside modal
    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_COLLEAGUE} readOnly />, {
      queryClient,
    });

    fireEvent.click(screen.getByTestId('workspace-sharing-button'));
    expect(screen.queryByTestId('sharing-cos-fieldset')).toBeNull();
    expect(screen.getByTestId('sharing-web-fieldset')).toBeDisabled();
    expect(screen.getByTestId('sharing-web-rw')).toBeDisabled();
    expect(screen.getByTestId('sharing-save-btn')).toBeDisabled();

    fireEvent.click(screen.getByTestId('sharing-save-btn'));
    expect(h.updateWorkspaceSharing).not.toHaveBeenCalled();

    expect(formatAccessLevelZh('rw')).toBe('可读写');
    expect(formatAccessLevelZh('ro')).toBe('仅可读');
    expect(formatAccessLevelZh('hidden')).toBe('不可见');
  });

  it('shows Team Memory switch only to the workspace owner (default off) and saves when toggled', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    const updatedSettings: WorkspaceSettingsResponse = {
      ...STABLE_OWN_SETTINGS,
      team_memory_enabled: true,
      updated_by: 'u-chao',
      updated_at: '2026-11-05T12:00:00Z',
    };
    h.updateWorkspaceSettings.mockResolvedValueOnce(updatedSettings);

    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_OWN), STABLE_OWN_SHARING);
    queryClient.setQueryData(
      queryKeys.dalton.workspaceSettings(WS_OWN),
      STABLE_OWN_SETTINGS,
    );

    const { unmount } = renderWithProviders(
      <WorkspaceSharingModal workspaceId={WS_OWN} />,
      { queryClient },
    );

    fireEvent.click(screen.getByTestId('workspace-sharing-button'));

    const section = screen.getByTestId('workspace-team-memory-section');
    expect(section).toHaveTextContent('团队记忆');
    expect(section).toHaveTextContent(
      '允许分析师查阅团队过去的对话记录（OpenClaw 对话卡片）',
    );
    const toggle = screen.getByTestId('workspace-team-memory-switch');
    expect(toggle).toHaveAttribute('aria-checked', 'false');

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-checked', 'true');

    fireEvent.click(screen.getByTestId('sharing-save-btn'));

    await waitFor(() => {
      expect(h.updateWorkspaceSettings).toHaveBeenCalledWith(WS_OWN, {
        team_memory_enabled: true,
      });
    });
    expect(h.updateWorkspaceSharing).not.toHaveBeenCalled();
    expect(
      queryClient.getQueryData(queryKeys.dalton.workspaceSettings(WS_OWN)),
    ).toEqual(updatedSettings);
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    unmount();

    // Non-owner (settings can_edit=false, even if sharing can_edit=true for a PM) does not see the switch
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_COLLEAGUE), {
      ...STABLE_COLLEAGUE_SHARING,
      can_edit: true,
    });
    queryClient.setQueryData(
      queryKeys.dalton.workspaceSettings(WS_COLLEAGUE),
      {
        ...STABLE_OWN_SETTINGS,
        workspace_id: WS_COLLEAGUE,
        can_edit: false,
      },
    );

    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_COLLEAGUE} />, {
      queryClient,
    });
    fireEvent.click(screen.getByTestId('workspace-sharing-button'));
    expect(screen.queryByTestId('workspace-team-memory-section')).toBeNull();
    expect(screen.queryByTestId('workspace-team-memory-switch')).toBeNull();
  });

  it('does not send Team Memory PUT when only sharing settings are modified', async () => {
    const enabledSettings: WorkspaceSettingsResponse = {
      ...STABLE_OWN_SETTINGS,
      team_memory_enabled: true,
    };
    const updatedSharing: WorkspaceSharingResponse = {
      ...STABLE_OWN_SHARING,
      web_access: 'rw',
    };
    h.updateWorkspaceSharing.mockResolvedValueOnce(updatedSharing);

    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_OWN), STABLE_OWN_SHARING);
    queryClient.setQueryData(
      queryKeys.dalton.workspaceSettings(WS_OWN),
      enabledSettings,
    );

    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_OWN} />, {
      queryClient,
    });

    fireEvent.click(screen.getByTestId('workspace-sharing-button'));
    expect(screen.getByTestId('workspace-team-memory-switch')).toHaveAttribute(
      'aria-checked',
      'true',
    );

    fireEvent.click(screen.getByTestId('sharing-web-rw'));
    fireEvent.click(screen.getByTestId('sharing-save-btn'));

    await waitFor(() => {
      expect(h.updateWorkspaceSharing).toHaveBeenCalledWith(WS_OWN, {
        web_access: 'rw',
      });
    });
    expect(h.updateWorkspaceSettings).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  it('keeps modal open, shows error message, preserves failed draft, and retries only the failed Team Memory request when Team Memory PUT fails', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    const updatedSharing: WorkspaceSharingResponse = {
      ...STABLE_OWN_SHARING,
      web_access: 'rw',
    };
    const updatedSettings: WorkspaceSettingsResponse = {
      ...STABLE_OWN_SETTINGS,
      team_memory_enabled: true,
    };
    h.updateWorkspaceSharing.mockResolvedValueOnce(updatedSharing);
    h.updateWorkspaceSettings
      .mockRejectedValueOnce(new Error('503 Service Unavailable'))
      .mockResolvedValueOnce(updatedSettings);

    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_OWN), STABLE_OWN_SHARING);
    queryClient.setQueryData(
      queryKeys.dalton.workspaceSettings(WS_OWN),
      STABLE_OWN_SETTINGS,
    );

    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_OWN} />, {
      queryClient,
    });

    fireEvent.click(screen.getByTestId('workspace-sharing-button'));
    fireEvent.click(screen.getByTestId('sharing-web-rw'));
    fireEvent.click(screen.getByTestId('workspace-team-memory-switch'));
    fireEvent.click(screen.getByTestId('sharing-save-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('workspace-sharing-save-error')).toHaveTextContent(
        '设置保存失败，请稍后重试',
      );
    });

    // Modal stays open; sharing succeeded and updated cache; team memory draft stays true for retry
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(h.updateWorkspaceSharing).toHaveBeenCalledTimes(1);
    expect(h.updateWorkspaceSettings).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData(queryKeys.dalton.sharing(WS_OWN))).toEqual(
      updatedSharing,
    );
    expect(
      queryClient.getQueryData(queryKeys.dalton.workspaceSettings(WS_OWN)),
    ).toEqual(STABLE_OWN_SETTINGS);
    expect(screen.getByTestId('workspace-team-memory-switch')).toHaveAttribute(
      'aria-checked',
      'true',
    );

    // Retry only sends the failed Team Memory PUT and closes on success
    fireEvent.click(screen.getByTestId('sharing-save-btn'));
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    expect(h.updateWorkspaceSharing).toHaveBeenCalledTimes(1);
    expect(h.updateWorkspaceSettings).toHaveBeenCalledTimes(2);
    expect(
      queryClient.getQueryData(queryKeys.dalton.workspaceSettings(WS_OWN)),
    ).toEqual(updatedSettings);
  });

  it('disables Team Memory switch while workspace settings are loading, omits Team Memory PUT if saved before loaded, and initializes draft when settings arrive', async () => {
    const { default: i18n } = await import('@/i18n');
    await i18n.changeLanguage('zh-CN');
    onTestFinished(async () => {
      await i18n.changeLanguage('en-US');
    });

    let resolveSettings: (val: WorkspaceSettingsResponse) => void = () => {};
    h.fetchWorkspaceSettings.mockImplementationOnce(
      () =>
        new Promise<WorkspaceSettingsResponse>((resolve) => {
          resolveSettings = resolve;
        }),
    );
    const updatedSharing: WorkspaceSharingResponse = {
      ...STABLE_OWN_SHARING,
      web_access: 'ro',
    };
    h.updateWorkspaceSharing.mockResolvedValueOnce(updatedSharing);

    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dalton.sharing(WS_OWN), STABLE_OWN_SHARING);

    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_OWN} />, {
      queryClient,
    });

    fireEvent.click(screen.getByTestId('workspace-sharing-button'));

    const section = screen.getByTestId('workspace-team-memory-section');
    expect(section).toHaveTextContent('正在加载设置…');
    const toggle = screen.getByTestId('workspace-team-memory-switch');
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute('aria-busy', 'true');

    // Clicking disabled switch does nothing
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-checked', 'false');

    // Saving while settings are still unloaded only submits sharing changes
    fireEvent.click(screen.getByTestId('sharing-web-ro'));
    fireEvent.click(screen.getByTestId('sharing-save-btn'));

    await waitFor(() => {
      expect(h.updateWorkspaceSharing).toHaveBeenCalledWith(WS_OWN, {
        web_access: 'ro',
      });
    });
    expect(h.updateWorkspaceSettings).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    // Reopen while still loading, then resolve settings with team_memory_enabled=true -> draft initializes to true
    fireEvent.click(screen.getByTestId('workspace-sharing-button'));
    expect(screen.getByTestId('workspace-team-memory-switch')).toBeDisabled();

    resolveSettings({
      ...STABLE_OWN_SETTINGS,
      team_memory_enabled: true,
    });

    await waitFor(() => {
      expect(
        screen.getByTestId('workspace-team-memory-switch'),
      ).not.toBeDisabled();
    });
    expect(screen.getByTestId('workspace-team-memory-switch')).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });
});
