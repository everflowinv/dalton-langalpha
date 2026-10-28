// DALTON-HOOK(onboarding-tasks): Dalton does not ship the desktop app, so its
// download task never reaches the getting-started checklist; in a browser the
// list (and with it the count and the progress ring) is six tasks.
import { describe, expect, it } from 'vitest';
import { DEPLOYED_TASKS, GETTING_STARTED_TASKS } from '../registry';

describe('Dalton getting-started tasks', () => {
  it('hides the desktop-app download task', () => {
    expect(GETTING_STARTED_TASKS.some((t) => t.id === 'desktopApp')).toBe(true);
    expect(DEPLOYED_TASKS.some((t) => t.id === 'desktopApp')).toBe(false);
  });

  it('offers six tasks in a browser', () => {
    expect(DEPLOYED_TASKS.map((t) => t.id)).toEqual([
      'dashboard',
      'market',
      'profile',
      'createWorkspace',
      'firstChat',
      'models',
    ]);
  });
});
