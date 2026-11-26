// DALTON-HOOK(page-host-status-fanout): a computer that finishes preparing while Settings is open is announced by its status frame
/**
 * A computer is prepared in the background and the user moves to Settings
 * while it runs. Its outcome reaches the page as a status frame, and the
 * "is ready" toast is to follow that frame at once.
 *
 * The tree is the app's: the shell's fallback host, which yields to the page
 * host Settings mounts, and nothing else. The list says `starting` for the
 * whole test, the way a read that left before the transition does, so the
 * toast can only come from the frame.
 */
import { act, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { Route, Routes } from 'react-router';
import type { ReactElement } from 'react';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { runAuthResets } from '@/lib/authResets';
import { renderWithProviders } from '@/test/utils';
import type { Computer } from '@/types/api';
import { computersApi } from '@/pages/ChatAgent/components/__tests__/computersDialogApi';

const h = vi.hoisted(() => ({
  brandName: 'Dalton',
  isDaltonAuth: true,
}));

vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  get BRAND_NAME() {
    return h.brandName;
  },
  isDaltonBuild: true,
  isCoreProfileBuild: false,
  DALTON_PROFILE: 'everflow' as const,
  faviconHref: () => '/logo-favicon.svg',
}));

vi.mock('@/config/daltonAuth', () => ({
  get isDaltonAuth() {
    return h.isDaltonAuth;
  },
  isDaltonLocalAuth: true,
  isDaltonSupabaseAuth: false,
}));

vi.mock('@/config/hostMode', () => ({
  isPlatformMode: false,
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({
    user: { id: 'u-1', email: 'analyst@dalton.test', name: 'Analyst', timezone: 'Asia/Shanghai' },
    isLoading: false,
    isError: false,
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: { risk_preference: {}, investment_preference: {}, model_preference: {}, other_preference: {} },
    isLoading: false,
    isLoaded: true,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['prefs'],
  useUpdatePreferences: () => ({ mutateAsync: vi.fn(async () => ({})), isPending: false }),
  useClearPreferences: () => ({ mutateAsync: vi.fn(async () => undefined), isPending: false }),
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useCapabilities')>()),
  useCapabilities: () => ({
    profile: 'everflow' as const,
    capabilities: new Set(),
    hasCapability: () => false,
    brandName: h.brandName,
    fingerprint: null,
    tickerLogosEnabled: false,
    isLoading: false,
    isLoaded: true,
    isError: false,
    refetch: vi.fn(async () => undefined),
  }),
}));

vi.mock('@/hooks/useCallerIdentity', () => ({
  useCallerIdentity: () => ({
    tier: 'internal',
    status: 'ready',
    authMode: 'local',
    isPlatform: false,
    isKnown: true,
    isConfirming: false,
    isMultiUserAuth: true,
    isSingleUserOss: false,
    isAdmin: false,
    isInternal: true,
    isExternal: false,
    canActAsOwner: true,
    canPublishPublic: false,
    canInvite: true,
    isTeamCredentialsShared: true,
    canManageCredentials: false,
    isPm: false,
    colleagueSharingEnabled: false,
    getLatestIdentity: vi.fn(),
    isConfirmingNow: vi.fn(() => false),
    revalidate: vi.fn(async () => undefined),
  }),
}));

vi.mock('@/hooks/useTeamCredentials', () => ({
  TEAM_SHARED_CREDENTIALS_HINT: '',
  fetchTeamCredentialsStatus: vi.fn(async () => ({
    sharing: 'team',
    enabled: true,
    holder_user_id: null,
    credential_user_id: 'u-1',
    is_pm: false,
    can_disconnect: false,
    can_invite: true,
    caller_tier: 'internal',
    colleague_sharing_enabled: true,
    auth_mode: 'local',
  })),
  useTeamCredentials: () => ({
    isShared: true,
    canManage: false,
    canDisconnect: false,
    isPm: false,
    canInvite: true,
    callerTier: 'internal',
    callerTierPending: false,
    callerTierError: false,
  }),
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({ data: { enabled: false }, isLoading: false, isError: false, refetch: vi.fn() }),
  useDaltonCoverageUnitDetail: () => ({ data: undefined, isLoading: false, isError: false }),
  splitCoverageUnits: () => ({ sectors: [], standaloneCompanies: [] }),
  coverageUnitErrorCode: () => null,
  coverageUnitBlockingAutomations: () => null,
}));

vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [] } }),
}));

vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [], total: 0 } }),
}));

vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));

type StatusFrame = { status: string; stage?: string };

/** The status frames each machine's stream hands the page, by machine. */
const streams = vi.hoisted(() => new Map<string, (frame: { status: string; stage?: string }) => void>());

vi.mock('@/pages/ChatAgent/utils/api', async () => ({
  ...(await computersApi()),
  // A stream stays open until the page stops listening, as a real one does.
  streamComputerEvents: vi.fn(
    (computerId: string, onFrame: (frame: StatusFrame) => void, signal: AbortSignal) =>
      new Promise<void>((resolve) => {
        streams.set(computerId, onFrame);
        signal.addEventListener('abort', () => resolve(), { once: true });
      }),
  ),
}));

import { toast } from '@/components/ui/use-toast';
import { FallbackComputersHost } from '@/components/CreateWorkspaceHost';
import { computerOutcomeWatch } from '@/pages/ChatAgent/hooks/computerOutcomes';
import { getComputers } from '@/pages/ChatAgent/utils/api';
import Settings from '../Settings';

const SRC_DIR = resolve(__dirname, '..', '..', '..');

/** Every `.tsx` under `src` but the tests, as `[path from src, source]`. */
function componentSources(dir = SRC_DIR, out: [string, string][] = []): [string, string][] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry !== '__tests__') componentSources(full, out);
    } else if (entry.endsWith('.tsx')) {
      out.push([full.slice(SRC_DIR.length + 1), readFileSync(full, 'utf8')]);
    }
  }
  return out;
}

/** A machine partway through being prepared. */
const PREPARING: Computer = {
  computer_id: '11111111-1111-1111-1111-111111111111',
  user_id: 'u-1',
  kind: 'daytona',
  name: 'Alpha Research',
  status: 'starting',
  resource_tier: 'standard',
  is_always_on: false,
  is_primary: true,
  workspace_count: 0,
  root_dir: '/home/workspace',
  provider_ref: null,
  ssh: null,
};

/** A page under the app shell, whose fallback host stands aside for a page that mounts its own. */
function renderUnderShell(page: ReactElement, route: string) {
  return renderWithProviders(
    <>
      <Routes>
        <Route path={route} element={page} />
      </Routes>
      <FallbackComputersHost />
    </>,
    { route },
  );
}

describe('a computer that finishes preparing while Settings is open', () => {
  beforeEach(() => {
    runAuthResets();
    streams.clear();
    vi.mocked(toast).mockReset();
    // Every read still says the machine is being prepared.
    vi.mocked(getComputers).mockReset();
    vi.mocked(getComputers).mockResolvedValue({ computers: [{ ...PREPARING }], total: 1 });
  });

  it('is announced when its status frame says running, with no list read to wait for', async () => {
    renderUnderShell(<Settings />, '/settings');

    // The page's own host has read the list and seen the machine being prepared...
    await waitFor(() => expect(computerOutcomeWatch.get().preparing.has(PREPARING.computer_id)).toBe(true));
    // ...and hears the machine out while it is, since nothing else on this page does.
    await waitFor(() => expect(streams.get(PREPARING.computer_id)).toBeTypeOf('function'));

    const frame = streams.get(PREPARING.computer_id)!;
    act(() => frame({ status: 'running' }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: expect.stringContaining('Alpha Research') }),
      );
    });
    expect(vi.mocked(toast).mock.calls[0]?.[0]).not.toHaveProperty('variant', 'destructive');
  });

  it('keeps quiet while the machine is still being prepared', async () => {
    renderUnderShell(<Settings />, '/settings');
    await waitFor(() => expect(streams.get(PREPARING.computer_id)).toBeTypeOf('function'));

    act(() => streams.get(PREPARING.computer_id)!({ status: 'starting', stage: 'installing' }));

    expect(toast).not.toHaveBeenCalled();
    expect(screen.queryByTestId('settings-permission-denied')).not.toBeInTheDocument();
  });

  // The control the first test is read against: the same shell and the same
  // list on a page that mounts no host of its own, where the fallback host
  // listens to the machine.
  it('is heard out by the shell on a page that mounts no host of its own', async () => {
    // The fallback host mounts for a machine the page before has seen being
    // prepared, which is what its host records before it unmounts.
    computerOutcomeWatch.set({
      preparing: new Set([PREPARING.computer_id]),
      ready: new Set(),
      specChanges: new Map(),
    });
    renderUnderShell(<div>Dashboard</div>, '/dashboard');

    await waitFor(() => expect(streams.get(PREPARING.computer_id)).toBeTypeOf('function'));

    act(() => streams.get(PREPARING.computer_id)!({ status: 'running' }));
    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: expect.stringContaining('Alpha Research') }),
      );
    });
  });

  // The page that brought its own fan-out above its host is the one exception,
  // and the next page that mounts a host is to ask for one rather than be
  // found by a machine that stays "Starting" until the window is refocused.
  it('is heard out by every page that mounts a host, ChatAgent aside, which hears it out above', () => {
    const mounts = componentSources().flatMap(([file, source]) =>
      [...source.matchAll(/<ComputersDialogHost\b([^>]*?)\/?>/g)].map(([, attrs]) => ({ file, attrs })),
    );
    expect(mounts.length).toBeGreaterThanOrEqual(3);

    const deaf = mounts.filter(
      ({ file, attrs }) =>
        file !== join('pages', 'ChatAgent', 'ChatAgent.tsx') &&
        !/\bfallback\b/.test(attrs) &&
        !/\bstatusFanout\b/.test(attrs),
    );
    expect(deaf).toEqual([]);

    const chatAgent = readFileSync(join(SRC_DIR, 'pages', 'ChatAgent', 'ChatAgent.tsx'), 'utf8');
    expect(chatAgent).toMatch(/\buseComputerStatusFanout\(\)/);
  });
});

