import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setStorageUser } from '@/lib/userStorage';
import { loadHistory, savedFrontTabPath, saveHistory } from '../pageTabsStorage'; // DALTON-HOOK(channel-links): also test savedFrontTabPath open-redirect rejection

// A platform build, where storage is keyed by the signed-in account.
vi.mock('@/config/hostMode', async (original) => ({ ...(await original<object>()), isPlatformMode: true }));
vi.mock('@/config/localUser', async (original) => ({ ...(await original<object>()), hasAuthEnv: true }));

beforeEach(() => {
  sessionStorage.clear();
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
});
