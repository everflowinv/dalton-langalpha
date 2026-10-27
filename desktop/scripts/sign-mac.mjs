/**
 * electron-builder's own macOS signing, except for the remote-control helper.
 *
 * electron-builder signs every Mach-O nested in the bundle with
 * `entitlementsInherit`, which here relaxes W^X for the JIT Electron's own
 * helpers need. The remote-control helper takes logins from the network and
 * needs none of it, so it alone is signed with an empty set. Identity, keychain,
 * hardened runtime and timestamp are left as electron-builder resolved them, for
 * it as for every other file.
 *
 * Empty rather than absent: @electron/osx-sign passes codesign `--entitlements`
 * for every file and, when none is named, falls back to its own defaults, which
 * include JIT and the camera. An empty set grants nothing.
 *
 * Installed as `mac.sign` by scripts/build.mjs, only on a build that carries the
 * helper.
 */
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { HELPER_BINARY } from './launch-agent.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))

export const HELPER_ENTITLEMENTS = path.join(here, '..', 'resources', 'entitlements.helper.plist')

const HELPER_IN_BUNDLE = path.join('Contents', 'MacOS', HELPER_BINARY)

/** The sign options electron-builder built, with the helper's entitlements emptied. */
export function withUnentitledHelper(opts) {
  const forFile = opts.optionsForFile
  return {
    ...opts,
    optionsForFile(file) {
      const options = forFile ? forFile(file) : {}
      if (path.relative(opts.app, file) !== HELPER_IN_BUNDLE) return options
      return { ...options, entitlements: HELPER_ENTITLEMENTS }
    },
  }
}

/**
 * What electron-builder calls in place of its signer. Delegates to that signer,
 * loaded through electron-builder's own dependency so the two cannot drift
 * apart in version, retries included.
 */
export async function sign(opts) {
  const builder = createRequire(import.meta.url).resolve('electron-builder')
  const { sign: signWithDefaults } = createRequire(builder)('app-builder-lib/out/codeSign/macCodeSign')
  return signWithDefaults(withUnentitledHelper(opts))
}
