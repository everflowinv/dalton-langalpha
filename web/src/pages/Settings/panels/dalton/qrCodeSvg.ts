/**
 * Zero-dependency ISO/IEC 18004 QR Code Model 2 encoder (8-bit Byte mode,
 * Error Correction Level M, Versions 1–20 up to 666 UTF-8 bytes).
 *
 * Used by DaltonWeixinBindCard when the WeChat iLink gateway returns a QR
 * payload URL that should be rendered directly as a scannable QR graphic.
 */

interface QrVersionSpec {
  version: number;
  size: number;
  dataCodewords: number;
  ecPerBlock: number;
  blocks: ReadonlyArray<readonly [count: number, dataWords: number]>;
  alignment: readonly number[];
}

// ISO/IEC 18004 Table 9 (EC Level M) & Table E.1 (alignment center coordinates)
const VERSION_SPECS_M: readonly QrVersionSpec[] = [
  { version: 1, size: 21, dataCodewords: 16, ecPerBlock: 10, blocks: [[1, 16]], alignment: [] },
  { version: 2, size: 25, dataCodewords: 28, ecPerBlock: 16, blocks: [[1, 28]], alignment: [6, 18] },
  { version: 3, size: 29, dataCodewords: 44, ecPerBlock: 26, blocks: [[1, 44]], alignment: [6, 22] },
  { version: 4, size: 33, dataCodewords: 64, ecPerBlock: 18, blocks: [[2, 32]], alignment: [6, 26] },
  { version: 5, size: 37, dataCodewords: 86, ecPerBlock: 24, blocks: [[2, 43]], alignment: [6, 30] },
  { version: 6, size: 41, dataCodewords: 108, ecPerBlock: 16, blocks: [[4, 27]], alignment: [6, 34] },
  { version: 7, size: 45, dataCodewords: 124, ecPerBlock: 18, blocks: [[4, 31]], alignment: [6, 22, 38] },
  { version: 8, size: 49, dataCodewords: 154, ecPerBlock: 22, blocks: [[2, 38], [2, 39]], alignment: [6, 24, 42] },
  { version: 9, size: 53, dataCodewords: 182, ecPerBlock: 22, blocks: [[3, 36], [2, 37]], alignment: [6, 26, 46] },
  { version: 10, size: 57, dataCodewords: 216, ecPerBlock: 26, blocks: [[4, 43], [1, 44]], alignment: [6, 28, 50] },
  { version: 11, size: 61, dataCodewords: 254, ecPerBlock: 30, blocks: [[1, 50], [4, 51]], alignment: [6, 30, 54] },
  { version: 12, size: 65, dataCodewords: 290, ecPerBlock: 22, blocks: [[6, 36], [2, 37]], alignment: [6, 32, 58] },
  { version: 13, size: 69, dataCodewords: 334, ecPerBlock: 22, blocks: [[8, 37], [1, 38]], alignment: [6, 34, 62] },
  { version: 14, size: 73, dataCodewords: 365, ecPerBlock: 24, blocks: [[4, 40], [5, 41]], alignment: [6, 26, 46, 66] },
  { version: 15, size: 77, dataCodewords: 415, ecPerBlock: 24, blocks: [[5, 41], [5, 42]], alignment: [6, 26, 48, 70] },
  { version: 16, size: 81, dataCodewords: 453, ecPerBlock: 28, blocks: [[7, 45], [3, 46]], alignment: [6, 26, 50, 74] },
  { version: 17, size: 85, dataCodewords: 507, ecPerBlock: 28, blocks: [[10, 46], [1, 47]], alignment: [6, 30, 54, 78] },
  { version: 18, size: 89, dataCodewords: 563, ecPerBlock: 26, blocks: [[9, 43], [4, 44]], alignment: [6, 30, 56, 82] },
  { version: 19, size: 93, dataCodewords: 627, ecPerBlock: 26, blocks: [[3, 44], [11, 45]], alignment: [6, 30, 58, 86] },
  { version: 20, size: 97, dataCodewords: 669, ecPerBlock: 26, blocks: [[3, 41], [13, 42]], alignment: [6, 34, 62, 90] },
];

const GF_EXP = new Uint8Array(512);
const GF_LOG = new Uint8Array(256);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i += 1) {
    GF_EXP[i] = x;
    GF_LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i += 1) {
    GF_EXP[i] = GF_EXP[i - 255];
  }
})();

function gfMul(a: number, b: number): number {
  if (a === 0 || b === 0) return 0;
  return GF_EXP[GF_LOG[a] + GF_LOG[b]];
}

function rsGeneratorPoly(degree: number): Uint8Array {
  let poly: Uint8Array<ArrayBufferLike> = new Uint8Array([1]);
  for (let i = 0; i < degree; i += 1) {
    const next = new Uint8Array(poly.length + 1);
    const root = GF_EXP[i];
    for (let j = 0; j < poly.length; j += 1) {
      next[j] ^= poly[j];
      next[j + 1] ^= gfMul(poly[j], root);
    }
    poly = next;
  }
  return poly;
}

function rsComputeEcc(data: Uint8Array, ecLen: number): Uint8Array {
  const gen = rsGeneratorPoly(ecLen);
  const rem = new Uint8Array(ecLen);
  for (let i = 0; i < data.length; i += 1) {
    const factor = data[i] ^ rem[0];
    rem.copyWithin(0, 1);
    rem[ecLen - 1] = 0;
    if (factor !== 0) {
      for (let j = 0; j < ecLen; j += 1) {
        rem[j] ^= gfMul(gen[j + 1], factor);
      }
    }
  }
  return rem;
}

function selectVersion(byteLen: number): QrVersionSpec | null {
  for (const spec of VERSION_SPECS_M) {
    const countBits = spec.version <= 9 ? 8 : 16;
    const totalBitsNeeded = 4 + countBits + byteLen * 8;
    if (totalBitsNeeded <= spec.dataCodewords * 8) {
      return spec;
    }
  }
  return null;
}

function encodeDataCodewords(bytes: Uint8Array, spec: QrVersionSpec): Uint8Array {
  const bits: number[] = [];
  const pushBits = (val: number, len: number) => {
    for (let i = len - 1; i >= 0; i -= 1) {
      bits.push((val >>> i) & 1);
    }
  };
  // 8-bit Byte mode indicator (0100)
  pushBits(0b0100, 4);
  pushBits(bytes.length, spec.version <= 9 ? 8 : 16);
  for (let i = 0; i < bytes.length; i += 1) {
    pushBits(bytes[i], 8);
  }
  const capacityBits = spec.dataCodewords * 8;
  const termLen = Math.min(4, capacityBits - bits.length);
  pushBits(0, termLen);
  while (bits.length % 8 !== 0) bits.push(0);

  const out = new Uint8Array(spec.dataCodewords);
  const dataByteLen = bits.length / 8;
  for (let i = 0; i < dataByteLen; i += 1) {
    let b = 0;
    for (let j = 0; j < 8; j += 1) {
      b = (b << 1) | bits[i * 8 + j];
    }
    out[i] = b;
  }
  const padBytes = [0xec, 0x11];
  for (let i = dataByteLen; i < spec.dataCodewords; i += 1) {
    out[i] = padBytes[(i - dataByteLen) % 2];
  }
  return out;
}

function interleaveCodewords(data: Uint8Array, spec: QrVersionSpec): Uint8Array {
  const dataBlocks: Uint8Array[] = [];
  const ecBlocks: Uint8Array[] = [];
  let offset = 0;
  for (const [count, words] of spec.blocks) {
    for (let b = 0; b < count; b += 1) {
      const slice = data.subarray(offset, offset + words);
      offset += words;
      dataBlocks.push(slice);
      ecBlocks.push(rsComputeEcc(slice, spec.ecPerBlock));
    }
  }
  const maxDataLen = Math.max(...dataBlocks.map((b) => b.length));
  const totalLen =
    dataBlocks.reduce((acc, b) => acc + b.length, 0) +
    ecBlocks.length * spec.ecPerBlock;
  const result = new Uint8Array(totalLen);
  let pos = 0;
  for (let i = 0; i < maxDataLen; i += 1) {
    for (const block of dataBlocks) {
      if (i < block.length) result[pos++] = block[i];
    }
  }
  for (let i = 0; i < spec.ecPerBlock; i += 1) {
    for (const block of ecBlocks) {
      result[pos++] = block[i];
    }
  }
  return result;
}

function computeFormatBits(mask: number): number {
  // EC Level M = 0b00
  const data = (0b00 << 3) | (mask & 0x7);
  let rem = data << 10;
  for (let i = 4; i >= 0; i -= 1) {
    if (rem & (1 << (i + 10))) {
      rem ^= 0x537 << i;
    }
  }
  return ((data << 10) | rem) ^ 0x5412;
}

function computeVersionBits(version: number): number {
  let rem = version << 12;
  for (let i = 5; i >= 0; i -= 1) {
    if (rem & (1 << (i + 12))) {
      rem ^= 0x1f25 << i;
    }
  }
  return (version << 12) | rem;
}

function maskBit(mask: number, r: number, c: number): boolean {
  switch (mask) {
    case 0: return (r + c) % 2 === 0;
    case 1: return r % 2 === 0;
    case 2: return c % 3 === 0;
    case 3: return (r + c) % 3 === 0;
    case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
    case 5: return ((r * c) % 2) + ((r * c) % 3) === 0;
    case 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
    default: return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0;
  }
}

function penaltyScore(modules: Uint8Array, size: number): number {
  let score = 0;
  const at = (r: number, c: number) => modules[r * size + c];

  // Rule 1: adjacent runs >= 5
  for (let r = 0; r < size; r += 1) {
    let runColor = at(r, 0);
    let runLen = 1;
    for (let c = 1; c < size; c += 1) {
      const v = at(r, c);
      if (v === runColor) {
        runLen += 1;
        if (runLen === 5) score += 3;
        else if (runLen > 5) score += 1;
      } else {
        runColor = v;
        runLen = 1;
      }
    }
  }
  for (let c = 0; c < size; c += 1) {
    let runColor = at(0, c);
    let runLen = 1;
    for (let r = 1; r < size; r += 1) {
      const v = at(r, c);
      if (v === runColor) {
        runLen += 1;
        if (runLen === 5) score += 3;
        else if (runLen > 5) score += 1;
      } else {
        runColor = v;
        runLen = 1;
      }
    }
  }

  // Rule 2: 2x2 blocks
  for (let r = 0; r < size - 1; r += 1) {
    for (let c = 0; c < size - 1; c += 1) {
      const v = at(r, c);
      if (v === at(r, c + 1) && v === at(r + 1, c) && v === at(r + 1, c + 1)) {
        score += 3;
      }
    }
  }

  // Rule 4: dark/light balance
  let dark = 0;
  for (let i = 0; i < modules.length; i += 1) {
    if (modules[i]) dark += 1;
  }
  const percentDiff = Math.abs((dark * 100) / modules.length - 50);
  score += Math.floor(percentDiff / 5) * 10;

  return score;
}

export interface QrMatrixResult {
  /** ViewBox dimension including 4-module quiet zone on each side. */
  size: number;
  /** Raw QR matrix width/height excluding quiet zone. */
  matrixSize: number;
  /** Selected QR version (1..20). */
  version: number;
  /** Selected mask pattern (0..7). */
  mask: number;
  modules: Uint8Array;
  svgPath: string;
}

export interface EncodeQrCodeOptions {
  /** Optional test-only mask override (0..7) for deterministic fixture comparison. */
  forceMask?: number;
}

/**
 * Encode a UTF-8 string into an ISO/IEC 18004 QR Code matrix + SVG path,
 * or null if the payload exceeds Version 20 Level M capacity (666 bytes).
 */
export function encodeQrCodeMatrix(
  text: string,
  options?: EncodeQrCodeOptions,
): QrMatrixResult | null {
  const bytes = new TextEncoder().encode(text);
  const spec = selectVersion(bytes.length);
  if (!spec) return null;

  const { size, version } = spec;
  const base = new Uint8Array(size * size);
  const isFunc = new Uint8Array(size * size);

  const setFunc = (r: number, c: number, dark: boolean) => {
    if (r < 0 || c < 0 || r >= size || c >= size) return;
    base[r * size + c] = dark ? 1 : 0;
    isFunc[r * size + c] = 1;
  };

  // Finder patterns + separators
  const drawFinder = (top: number, left: number) => {
    for (let dr = -1; dr <= 7; dr += 1) {
      for (let dc = -1; dc <= 7; dc += 1) {
        const r = top + dr;
        const c = left + dc;
        if (r < 0 || c < 0 || r >= size || c >= size) continue;
        const inOuter = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6;
        const onBorder = dr === 0 || dr === 6 || dc === 0 || dc === 6;
        const inCore = dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4;
        setFunc(r, c, inOuter && (onBorder || inCore));
      }
    }
  };
  drawFinder(0, 0);
  drawFinder(0, size - 7);
  drawFinder(size - 7, 0);

  // Timing patterns
  for (let i = 8; i < size - 8; i += 1) {
    setFunc(6, i, i % 2 === 0);
    setFunc(i, 6, i % 2 === 0);
  }

  // Alignment patterns: skip only the 3 finder corners so patterns intersecting
  // timing row/col 6 on V7+ are always placed.
  const alignLen = spec.alignment.length;
  for (let ri = 0; ri < alignLen; ri += 1) {
    for (let ci = 0; ci < alignLen; ci += 1) {
      const isTopLeft = ri === 0 && ci === 0;
      const isTopRight = ri === 0 && ci === alignLen - 1;
      const isBottomLeft = ri === alignLen - 1 && ci === 0;
      if (isTopLeft || isTopRight || isBottomLeft) continue;
      const r = spec.alignment[ri];
      const c = spec.alignment[ci];
      for (let dr = -2; dr <= 2; dr += 1) {
        for (let dc = -2; dc <= 2; dc += 1) {
          const dark = Math.max(Math.abs(dr), Math.abs(dc)) !== 1;
          setFunc(r + dr, c + dc, dark);
        }
      }
    }
  }

  // Reserve format info areas + dark module
  for (let i = 0; i <= 8; i += 1) {
    if (i !== 6) {
      setFunc(8, i, false);
      setFunc(i, 8, false);
    }
  }
  for (let i = 0; i < 8; i += 1) {
    setFunc(8, size - 1 - i, false);
  }
  for (let i = 0; i < 7; i += 1) {
    setFunc(size - 1 - i, 8, false);
  }
  setFunc(4 * version + 9, 8, true);

  // Version info blocks (V7+)
  if (version >= 7) {
    const vBits = computeVersionBits(version);
    for (let i = 0; i < 18; i += 1) {
      const dark = Boolean((vBits >>> i) & 1);
      const a = size - 11 + (i % 3);
      const b = Math.floor(i / 3);
      setFunc(b, a, dark);
      setFunc(a, b, dark);
    }
  }

  // Place data + EC bits (remaining bits stay 0)
  const codewords = interleaveCodewords(encodeDataCodewords(bytes, spec), spec);
  let bitIdx = 0;
  const totalBits = codewords.length * 8;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    const upward = ((right + 1) & 2) === 0;
    for (let step = 0; step < size; step += 1) {
      const r = upward ? size - 1 - step : step;
      for (let j = 0; j < 2; j += 1) {
        const c = right - j;
        if (isFunc[r * size + c]) continue;
        let dark = false;
        if (bitIdx < totalBits) {
          dark = Boolean((codewords[bitIdx >>> 3] >>> (7 - (bitIdx & 7))) & 1);
          bitIdx += 1;
        }
        base[r * size + c] = dark ? 1 : 0;
      }
    }
  }

  const applyFormat = (grid: Uint8Array, mask: number) => {
    const fBits = computeFormatBits(mask);
    for (let i = 0; i <= 5; i += 1) grid[i * size + 8] = (fBits >>> i) & 1;
    grid[7 * size + 8] = (fBits >>> 6) & 1;
    grid[8 * size + 8] = (fBits >>> 7) & 1;
    grid[8 * size + 7] = (fBits >>> 8) & 1;
    for (let i = 9; i < 15; i += 1) grid[8 * size + (14 - i)] = (fBits >>> i) & 1;

    for (let i = 0; i < 8; i += 1) grid[8 * size + (size - 1 - i)] = (fBits >>> i) & 1;
    for (let i = 8; i < 15; i += 1) grid[(size - 15 + i) * size + 8] = (fBits >>> i) & 1;
  };

  const buildMaskedGrid = (mask: number): Uint8Array<ArrayBuffer> => {
    const candidate = new Uint8Array(base.length);
    candidate.set(base);
    for (let r = 0; r < size; r += 1) {
      for (let c = 0; c < size; c += 1) {
        if (!isFunc[r * size + c] && maskBit(mask, r, c)) {
          candidate[r * size + c] ^= 1;
        }
      }
    }
    applyFormat(candidate, mask);
    return candidate;
  };

  let bestGrid: Uint8Array<ArrayBuffer> = base;
  let bestMask = 0;
  const forced = options?.forceMask;
  if (typeof forced === 'number' && Number.isInteger(forced) && forced >= 0 && forced <= 7) {
    bestMask = forced;
    bestGrid = buildMaskedGrid(forced);
  } else {
    let bestScore = Number.POSITIVE_INFINITY;
    for (let mask = 0; mask < 8; mask += 1) {
      const candidate = buildMaskedGrid(mask);
      const sc = penaltyScore(candidate, size);
      if (sc < bestScore) {
        bestScore = sc;
        bestMask = mask;
        bestGrid = candidate;
      }
    }
  }

  // Build compact SVG path (quiet zone = 4 modules)
  const quiet = 4;
  const parts: string[] = [];
  for (let r = 0; r < size; r += 1) {
    for (let c = 0; c < size; c += 1) {
      if (bestGrid[r * size + c]) {
        parts.push(`M${c + quiet},${r + quiet}h1v1h-1z`);
      }
    }
  }
  return {
    size: size + quiet * 2,
    matrixSize: size,
    version,
    mask: bestMask,
    modules: bestGrid,
    svgPath: parts.join(''),
  };
}
