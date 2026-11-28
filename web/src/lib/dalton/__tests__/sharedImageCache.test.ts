// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  beginScopedImageRead,
  buildSharedImageScope,
  commitScopedImageUrl,
  finishScopedImageRead,
  getScopedImageUrl,
  getSharedImageScopeEpoch,
  onSharedImageScopeRevoked,
  parseSharedImageScopeFromKey,
  revokeSharedImageScope,
  shouldCommitScopedImageRead,
} from '../sharedImageCache';

describe('sharedImageCache', () => {
  const originalRevoke = URL.revokeObjectURL;
  let revokeMock: ReturnType<typeof vi.fn<typeof URL.revokeObjectURL>>;

  beforeEach(() => {
    revokeMock = vi.fn<typeof URL.revokeObjectURL>();
    URL.revokeObjectURL = revokeMock;
  });

  afterEach(() => {
    revokeSharedImageScope('tok-a');
    revokeSharedImageScope('tok-b');
    URL.revokeObjectURL = originalRevoke;
  });

  it('builds distinct scopes per token and allowFiles mode and parses composite keys', () => {
    const filesScope = buildSharedImageScope('tok-a', true);
    const textScope = buildSharedImageScope('tok-a', false);
    const otherScope = buildSharedImageScope('tok-b', true);

    expect(filesScope).not.toBe(textScope);
    expect(filesScope).not.toBe(otherScope);
    expect(parseSharedImageScopeFromKey(`${filesScope}:charts/rev.png`)).toBe(filesScope);
    expect(parseSharedImageScopeFromKey(`${textScope}:charts/rev.png`)).toBe(textScope);
    expect(parseSharedImageScopeFromKey('ws-123:charts/rev.png')).toBeNull();
  });

  it('advances epochs monotonically on revoke and rejects stale reads from backfilling', () => {
    const scope = buildSharedImageScope('tok-a', true);
    const first = beginScopedImageRead(scope);
    expect(shouldCommitScopedImageRead(scope, first.epoch)).toBe(true);
    expect(first.signal.aborted).toBe(false);

    revokeSharedImageScope('tok-a');

    expect(first.signal.aborted).toBe(true);
    expect(shouldCommitScopedImageRead(scope, first.epoch)).toBe(false);
    expect(getSharedImageScopeEpoch(scope)).toBe(first.epoch + 1);

    // Late slow request from old epoch must not backfill and must release its object URL.
    const committed = commitScopedImageUrl(scope, 'https://api.test/media/1', first.epoch, 'blob:stale-1');
    expect(committed).toBe(false);
    expect(getScopedImageUrl(scope, 'https://api.test/media/1')).toBeNull();
    expect(revokeMock).toHaveBeenCalledWith('blob:stale-1');

    // A fresh read in the new epoch succeeds until revoked again.
    const second = beginScopedImageRead(scope);
    expect(second.epoch).toBe(first.epoch + 1);
    expect(shouldCommitScopedImageRead(scope, second.epoch)).toBe(true);
    expect(commitScopedImageUrl(scope, 'https://api.test/media/1', second.epoch, 'blob:fresh-2')).toBe(true);
    expect(getScopedImageUrl(scope, 'https://api.test/media/1')).toBe('blob:fresh-2');
    finishScopedImageRead(scope, second.signal);
  });

  it('aborts in-flight controllers, revokes cached object URLs, and notifies listeners for target token only', () => {
    const scopeA = buildSharedImageScope('tok-a', true);
    const scopeAText = buildSharedImageScope('tok-a', false);
    const scopeB = buildSharedImageScope('tok-b', true);

    const readA = beginScopedImageRead(scopeA);
    const readAText = beginScopedImageRead(scopeAText);
    const readB = beginScopedImageRead(scopeB);

    expect(commitScopedImageUrl(scopeA, 'img-1', readA.epoch, 'blob:a-1')).toBe(true);
    expect(commitScopedImageUrl(scopeA, 'img-2', readA.epoch, 'blob:a-2')).toBe(true);
    expect(commitScopedImageUrl(scopeB, 'img-1', readB.epoch, 'blob:b-1')).toBe(true);

    const onRevoke = vi.fn<(token: string) => void>();
    const unsubscribe = onSharedImageScopeRevoked(onRevoke);

    revokeSharedImageScope('tok-a');
    unsubscribe();

    expect(readA.signal.aborted).toBe(true);
    expect(readAText.signal.aborted).toBe(true);
    expect(readB.signal.aborted).toBe(false);

    expect(revokeMock).toHaveBeenCalledWith('blob:a-1');
    expect(revokeMock).toHaveBeenCalledWith('blob:a-2');
    expect(revokeMock).not.toHaveBeenCalledWith('blob:b-1');

    expect(getScopedImageUrl(scopeA, 'img-1')).toBeNull();
    expect(getScopedImageUrl(scopeA, 'img-2')).toBeNull();
    expect(getScopedImageUrl(scopeB, 'img-1')).toBe('blob:b-1');
    expect(onRevoke).toHaveBeenCalledWith('tok-a');
  });
});
