import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const authMode = vi.hoisted(() => ({
  platform: true,
  authEnv: true,
  daltonAuth: false,
  daltonLocalAuth: false,
}));

// A platform build by default, where storage is keyed by the signed-in account.
vi.mock('@/config/hostMode', async (original) => ({
  ...(await original<object>()),
  get isPlatformMode() { return authMode.platform; },
}));
vi.mock('@/config/localUser', async (original) => ({
  ...(await original<object>()),
  LOCAL_USER_ID: 'local-dev-user',
  get hasAuthEnv() { return authMode.authEnv; },
}));
vi.mock('@/config/daltonAuth', async (original) => ({
  ...(await original<object>()),
  get isDaltonAuth() { return authMode.daltonAuth; },
  get isDaltonLocalAuth() { return authMode.daltonLocalAuth; },
}));

import { setStorageUser, storageUser } from '@/lib/userStorage';
import {
  loadHistory,
  loadTabs,
  savedFrontTabPath,
  saveHistory,
  saveTabs,
  tabsRecord,
} from '../pageTabsStorage'; // DALTON-HOOK(channel-links): also test savedFrontTabPath open-redirect rejection

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  authMode.platform = true;
  authMode.authEnv = true;
  authMode.daltonAuth = false;
  authMode.daltonLocalAuth = false;
  setStorageUser('u1');
});
afterEach(() => setStorageUser(null));

describe("the tabs' history kept across a reload", () => {
  it('comes back with its closed tabs, without their route state', () => {
    saveHistory({
      owners: { k1: 'm' },
      closed: [{ tab: { id: 'm', path: '/market', title: 'NVDA', key: 'k1', state: { ws: 'w' } }, index: 1 }],
    });
    expect(loadHistory()).toEqual({
      owners: { k1: 'm' },
      closed: [{ tab: { id: 'm', path: '/market', title: 'NVDA', key: 'k1' }, index: 1 }],
    });
  });

  it('leaves out a repeated or unreadable closed tab, and keeps the owners', () => {
    sessionStorage.setItem('user:u1:app-page-tab-history', JSON.stringify({
      owners: { k1: 'x' },
      closed: [
        { tab: { id: 'x', path: '/b' }, index: 1 },
        { tab: { id: 'x', path: '/c' }, index: 2 },
        { tab: { id: 'y', path: '//elsewhere' }, index: 0 },
        { tab: { id: 'z', path: '/d' }, index: -1 },
      ],
    }));
    const history = loadHistory();
    expect(history.closed.map((c) => c.tab.path)).toEqual(['/b']);
    expect(history.owners).toEqual({ k1: 'x' });
  });

  it('starts from the owners saved before the record, when there is none yet', () => {
    sessionStorage.setItem('user:u1:app-page-tab-entries', JSON.stringify({ k1: 'a', k2: 'b' }));
    expect(loadHistory()).toEqual({ owners: { k1: 'a', k2: 'b' }, closed: [] });
    saveHistory({ owners: { k3: 'c' }, closed: [] });
    expect(loadHistory().owners).toEqual({ k3: 'c' });
  });

  it('is kept per account', () => {
    saveHistory({ owners: {}, closed: [{ tab: { id: 'm', path: '/market' }, index: 0 }] });
    setStorageUser('u2');
    expect(loadHistory().closed).toEqual([]);
  });

  // DALTON-HOOK(channel-links): reject dot-segment, encoded-slash, and control-char open redirects in persisted tabs
  it('drops persisted tabs whose paths fail full inAppPath validation', () => {
    localStorage.setItem('user:u1:app-page-tabs', JSON.stringify({
      activeId: 't1',
      tabs: [
        { id: 't1', path: '/..//evil.example/phish' },
        { id: 't2', path: '/%2F%2Fevil.example/phish' },
        { id: 't3', path: '/\t/evil.example/phish' },
        { id: 't4', path: '/%5Cevil.example/phish' },
      ],
    }));
    expect(savedFrontTabPath('u1')).toBeNull();
    localStorage.removeItem('user:u1:app-page-tabs');
  });

  // DALTON-HOOK(dalton-auth): isolate page tabs per local account and discard legacy shared local-dev-user tabs
  it('isolates page tabs across two local-auth accounts and discards legacy shared local-dev-user tabs', () => {
    authMode.platform = false;
    authMode.authEnv = false;
    authMode.daltonAuth = true;
    authMode.daltonLocalAuth = true;

    // Seed legacy tabs written to the shared local-dev-user key by an earlier build.
    localStorage.setItem('user:local-dev-user:app-page-tabs', JSON.stringify({
      activeId: 'legacy-1',
      tabs: [{ id: 'legacy-1', path: '/chat/t/private-legacy-thread', title: 'Secret Legacy Thread' }],
    }));
    sessionStorage.setItem('user:local-dev-user:app-page-tab-history', JSON.stringify({
      owners: { k0: 'legacy-1' },
      closed: [{ tab: { id: 'legacy-1', path: '/chat/t/private-legacy-thread', title: 'Secret Legacy Thread' }, index: 0 }],
    }));

    // Account A logs in: must not see legacy shared tabs, and legacy shared keys are discarded.
    setStorageUser('acct-a');
    expect(storageUser()).toBe('acct-a');
    const aInitial = loadTabs('/home', storageUser());
    expect(aInitial.tabs.map((t) => t.path)).toEqual(['/home']);
    expect(localStorage.getItem('user:local-dev-user:app-page-tabs')).toBeNull();
    expect(sessionStorage.getItem('user:local-dev-user:app-page-tab-history')).toBeNull();
    expect(loadHistory().closed).toEqual([]);

    // Account A saves a private thread tab.
    saveTabs(tabsRecord({
      activeId: 'tab-a',
      tabs: [{ id: 'tab-a', path: '/chat/t/acct-a-secret', title: 'Account A Secret' }],
    }));
    saveHistory({
      owners: { ka: 'tab-a' },
      closed: [{ tab: { id: 'tab-a-closed', path: '/chat/t/acct-a-closed', title: 'A Closed' }, index: 0 }],
    });
    expect(localStorage.getItem('user:local-dev-user:app-page-tabs')).toBeNull();
    expect(savedFrontTabPath(storageUser())).toBe('/chat/t/acct-a-secret');

    // Switch to Account B: must NOT see Account A's private thread tabs or closed history.
    setStorageUser(null);
    setStorageUser('acct-b');
    expect(storageUser()).toBe('acct-b');
    const bInitial = loadTabs('/home', storageUser());
    expect(bInitial.tabs.map((t) => t.path)).toEqual(['/home']);
    expect(savedFrontTabPath(storageUser())).toBeNull();
    expect(loadHistory().closed).toEqual([]);

    // Account B saves its own tab.
    saveTabs(tabsRecord({
      activeId: 'tab-b',
      tabs: [{ id: 'tab-b', path: '/chat/t/acct-b-thread', title: 'Account B Thread' }],
    }));

    // Switch back to Account A: sees only Account A's own tab, never Account B's.
    setStorageUser('acct-a');
    const aRestored = loadTabs('/home', storageUser());
    expect(aRestored.tabs).toEqual([
      { id: 'tab-a', path: '/chat/t/acct-a-secret', title: 'Account A Secret' },
    ]);
    expect(savedFrontTabPath('local-dev-user')).toBeNull();
  });
});
