import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { encodeQrCodeMatrix } from '../qrCodeSvg';

const FIXTURES_DIR = path.resolve(__dirname, '../__fixtures__/qr');

/**
 * Extract the 3-bit mask number (0..7) directly from the QR matrix's
 * top-left format information bits (ISO/IEC 18004 §7.9).
 */
function readMaskFromMatrix(matrix: Uint8Array, size: number): number {
  const at = (r: number, c: number) => matrix[r * size + c] & 1;
  // Reconstruct the 15-bit format word (bit 14 at (8,0) down to bit 0 at (0,8))
  let formatBits = 0;
  for (let i = 0; i <= 5; i += 1) formatBits |= at(i, 8) << i;
  formatBits |= at(7, 8) << 6;
  formatBits |= at(8, 8) << 7;
  formatBits |= at(8, 7) << 8;
  for (let i = 9; i < 15; i += 1) formatBits |= at(8, 14 - i) << i;
  const unmasked = formatBits ^ 0x5412;
  return (unmasked >>> 10) & 0x7;
}

function loadFixtureMatrix(filename: string): { size: number; modules: Uint8Array } {
  const lines = fs
    .readFileSync(path.join(FIXTURES_DIR, filename), 'utf8')
    .trim()
    .split(/\r?\n/);
  const size = lines.length;
  const modules = new Uint8Array(size * size);
  for (let r = 0; r < size; r += 1) {
    const row = lines[r];
    if (row.length !== size) {
      throw new Error(`Expected square matrix in ${filename}, row ${r} has len ${row.length} != ${size}`);
    }
    for (let c = 0; c < size; c += 1) {
      modules[r * size + c] = row[c] === '1' ? 1 : 0;
    }
  }
  return { size, modules };
}

describe('qrCodeSvg ISO/IEC 18004 reference matrix verification', () => {
  const cases: ReadonlyArray<{
    n: number;
    expectedVersion: number;
    expectedSize: number;
  }> = [
    { n: 40, expectedVersion: 3, expectedSize: 29 },
    { n: 110, expectedVersion: 7, expectedSize: 45 },
    { n: 200, expectedVersion: 10, expectedSize: 57 },
    { n: 300, expectedVersion: 13, expectedSize: 69 },
  ];

  for (const { n, expectedVersion, expectedSize } of cases) {
    it(`matches qrencode -l M -8 -m 0 module-for-module for payload_${n}.txt (V${expectedVersion})`, () => {
      const payload = fs.readFileSync(path.join(FIXTURES_DIR, `payload_${n}.txt`), 'utf8');
      const ref = loadFixtureMatrix(`matrix_${n}.txt`);
      expect(ref.size).toBe(expectedSize);

      const refMask = readMaskFromMatrix(ref.modules, ref.size);
      const encoded = encodeQrCodeMatrix(payload, { forceMask: refMask });
      expect(encoded).not.toBeNull();
      expect(encoded!.version).toBe(expectedVersion);
      expect(encoded!.matrixSize).toBe(expectedSize);
      expect(encoded!.mask).toBe(refMask);
      expect(Array.from(encoded!.modules)).toEqual(Array.from(ref.modules));
    });
  }

  it('encodes up to Version 20 Level M capacity (666 bytes) and returns null above limit', () => {
    const maxPayload = 'a'.repeat(666);
    const maxRes = encodeQrCodeMatrix(maxPayload);
    expect(maxRes).not.toBeNull();
    expect(maxRes!.version).toBe(20);
    expect(maxRes!.matrixSize).toBe(97);

    const overPayload = 'a'.repeat(667);
    expect(encodeQrCodeMatrix(overPayload)).toBeNull();
  });
});
