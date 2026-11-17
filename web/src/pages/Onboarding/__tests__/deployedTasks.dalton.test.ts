// DALTON-HOOK(onboarding-tasks): Dalton does not ship the desktop app, so its
// download task never reaches the getting-started checklist; in a browser the
// list (and with it the count and the progress ring) is six tasks.
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { DEPLOYED_TASKS, GETTING_STARTED_TASKS } from '../registry';
import type { GettingStartedTaskDef } from '../registry';

describe('Dalton getting-started tasks', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

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

  it('sets desktopApp.to to null and skips rendering null-destination tasks in Dalton build', async () => {
    vi.stubEnv('VITE_BRAND_MARK', 'everflow');
    vi.resetModules();

    const { GETTING_STARTED_TASKS: daltonTasks, DEPLOYED_TASKS: daltonDeployed } = await import('../registry');
    const desktopDef = daltonTasks.find((t) => t.id === 'desktopApp');
    expect(desktopDef?.to).toBeNull();
    expect(daltonDeployed.some((t) => t.id === 'desktopApp')).toBe(false);

    const { GettingStartedSection } = await import('../engine/GettingStarted');
    const onSelect = vi.fn<(def: GettingStartedTaskDef) => void>();
    const onDismiss = vi.fn<() => void>();
    render(
      React.createElement(
        MemoryRouter,
        null,
        React.createElement(GettingStartedSection, {
          tasks: daltonTasks.map((def) => ({ def, done: false })),
          doneCount: 0,
          onSelect,
          onDismiss,
          defaultOpen: true,
        }),
      ),
    );

    expect(screen.queryByRole('button', { name: /desktopApp|Get the desktop app/i })).toBeNull();
  });
});
