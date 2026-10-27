import { isPlatformMode } from '@/config/hostMode';
import { PAGE_INTROS } from './pageIntros';
import { GETTING_STARTED_TASKS } from './gettingStarted';
import type { GettingStartedTaskDef, PageIntroDef } from './types';

export { ANNOUNCEMENTS } from './announcements';
export { PAGE_INTROS, GETTING_STARTED_TASKS };
export * from './types';

/** Tasks offered in this deployment — channel integrations are platform-hosted. */
export const DEPLOYED_TASKS: GettingStartedTaskDef[] = GETTING_STARTED_TASKS.filter(
  (t) => !t.platformOnly || isPlatformMode
);

/** Intros as this deployment shows them: platform-only steps drop off a
 *  self-hosted build. A first step cannot be platform-only, so none empties. */
export const DEPLOYED_INTROS: PageIntroDef[] = PAGE_INTROS.map(
  ({ steps: [first, ...rest], ...intro }) => ({
    ...intro,
    steps: [first, ...rest.filter((s) => !s.platformOnly || isPlatformMode)],
  })
);
