import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { createTestQueryClient, renderWithProviders } from '@/test/utils';
import { WorkspaceTrackingToggle } from '../WorkspaceTrackingToggle';
import { WorkspaceSharingModal } from '../WorkspaceSharingModal';
import { TeamReadOnlyBanner } from '../TeamSharing';
import type {
  WorkspaceSettingsResponse,
  WorkspaceSharingResponse,
  WorkspaceTrackingResponse,
} from '../../../utils/api/workspaces';

const WS_ID = '11111111-1111-4111-8111-111111111111';

const h = vi.hoisted(() => ({
  fetchWorkspaceTracking: vi.fn<(wsId: string) => Promise<WorkspaceTrackingResponse>>(),
  fetchWorkspaceSharing: vi.fn<(wsId: string) => Promise<WorkspaceSharingResponse>>(),
  fetchWorkspaceSettings: vi.fn<(wsId: string) => Promise<WorkspaceSettingsResponse>>(),
}));

vi.mock('../../../utils/api/workspaces', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../utils/api/workspaces')>();
  return {
    ...actual,
    fetchWorkspaceTracking: (wsId: string) => h.fetchWorkspaceTracking(wsId),
    fetchWorkspaceSharing: (wsId: string) => h.fetchWorkspaceSharing(wsId),
    fetchWorkspaceSettings: (wsId: string) => h.fetchWorkspaceSettings(wsId),
  };
});

describe('Mobile topbar compact mode (Dalton collaboration controls)', () => {
  beforeEach(() => {
    h.fetchWorkspaceTracking.mockResolvedValue({
      ok: true,
      workspace_id: WS_ID,
      ticker: 'NVDA',
      tier: 'tier1',
      timezone: 'America/New_York',
      status: 'enabled',
      enabled: true,
      paused_by_user: false,
      label: 'NVDA',
      missing_tasks: [],
      paused_tasks: [],
      active_tasks: [],
      can_edit: true,
      tasks: [],
    });
    h.fetchWorkspaceSharing.mockResolvedValue({
      workspace_id: WS_ID,
      cos_access: 'ro',
      web_access: 'ro',
      is_home: false,
      can_edit: true,
      colleague_dispatch_enabled: true,
      updated_by: null,
      updated_at: null,
    });
    h.fetchWorkspaceSettings.mockResolvedValue({
      workspace_id: WS_ID,
      team_memory_enabled: false,
      is_home: false,
      can_edit: true,
      owner_external: false,
      updated_by: null,
      updated_at: null,
    });
  });

  it('WorkspaceTrackingToggle compact hides text label and marks data-compact, while desktop keeps full label', async () => {
    const qc1 = createTestQueryClient();
    const { unmount } = renderWithProviders(
      <WorkspaceTrackingToggle workspaceId={WS_ID} compact />,
      { queryClient: qc1 },
    );

    const compactToggle = await screen.findByTestId('workspace-tracking-toggle');
    expect(compactToggle).toHaveAttribute('data-compact', 'true');
    expect(screen.queryByTestId('workspace-tracking-label')).toBeNull();
    expect(screen.getByTestId('workspace-tracking-expand')).toHaveClass('tap-target');
    unmount();

    const qc2 = createTestQueryClient();
    renderWithProviders(<WorkspaceTrackingToggle workspaceId={WS_ID} />, { queryClient: qc2 });
    const fullToggle = await screen.findByTestId('workspace-tracking-toggle');
    expect(fullToggle).not.toHaveAttribute('data-compact');
    expect(screen.getByTestId('workspace-tracking-label')).toBeInTheDocument();
  });

  it('WorkspaceSharingModal compact hides label and switches away from Share2 icon, while desktop keeps text', async () => {
    const qc1 = createTestQueryClient();
    const { unmount } = renderWithProviders(
      <WorkspaceSharingModal workspaceId={WS_ID} compact />,
      { queryClient: qc1 },
    );

    const compactBtn = await screen.findByTestId('workspace-sharing-button');
    expect(compactBtn).toHaveAttribute('data-compact', 'true');
    expect(compactBtn).toHaveClass('tap-target');
    expect(compactBtn.textContent?.trim()).toBe('');
    expect(compactBtn.querySelector('svg.lucide-users')).not.toBeNull();
    expect(compactBtn.querySelector('svg.lucide-share-2')).toBeNull();
    unmount();

    const qc2 = createTestQueryClient();
    renderWithProviders(<WorkspaceSharingModal workspaceId={WS_ID} />, { queryClient: qc2 });
    await waitFor(() => {
      const fullBtn = screen.getByTestId('workspace-sharing-button');
      expect(fullBtn).not.toHaveAttribute('data-compact');
      expect(fullBtn.textContent?.trim().length).toBeGreaterThan(0);
    });
  });

  it('TeamReadOnlyBanner compact enables truncation and marks data-compact', () => {
    const { rerender } = renderWithProviders(
      <TeamReadOnlyBanner ownerName="Alice" access="ro" compact />,
    );
    const compactBanner = screen.getByTestId('team-readonly-banner');
    expect(compactBanner).toHaveAttribute('data-compact', 'true');
    expect(compactBanner.querySelector('span')?.className).toContain('truncate');

    rerender(<TeamReadOnlyBanner ownerName="Alice" access="ro" />);
    const fullBanner = screen.getByTestId('team-readonly-banner');
    expect(fullBanner).not.toHaveAttribute('data-compact');
  });
});
