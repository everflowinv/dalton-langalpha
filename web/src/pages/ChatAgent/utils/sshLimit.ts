/**
 * The refusal for one SSH computer too many. It is a deployment setting, not a
 * plan quota, so it reads the same in every host mode and stays out of
 * `entitlementErrorMessage`.
 */
import type { useTranslation } from 'react-i18next';

import { apiErrorCoded } from './api/errors';

type Translate = ReturnType<typeof useTranslation>['t'];

/** Our sentence for a 409 `ssh_computer_limit`, or null for any other error. */
export function sshLimitMessage(err: unknown, t: Translate): string | null {
  const detail = apiErrorCoded(err, 'ssh_computer_limit');
  if (!detail) return null;
  const count = typeof detail.limit === 'number' ? detail.limit : 0;
  return t('computer.ssh.limitReached', {
    count,
    defaultValue_one: 'You can have only {{count}} SSH computer. Remove it to add another.',
    defaultValue_other: 'You can have up to {{count}} SSH computers. Remove one to add another.',
  });
}
