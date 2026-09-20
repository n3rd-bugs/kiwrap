// Small baseline JPEG encoder (8-bit, YCbCr 4:4:4, standard Annex K tables) so the fake helper can stream
// real JPEG frames without any dependency. Written for the tests: clarity over speed.

const ZIGZAG = [
  0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
  35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
];
const LUMA_Q = [
  16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
  18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92, 49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
];
const CHROMA_Q = [
  17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99, 24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
  99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
];
// counts of codes per length 1..16, then the symbol values (JPEG Annex K.3)
const DC_L_N = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0], DC_L_V = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
const DC_C_N = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0], DC_C_V = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
const AC_L_N = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 125];
const AC_L_V = [
  0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
  0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0, 0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
  0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
  0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
  0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
  0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
  0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
];
const AC_C_N = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 119];
const AC_C_V = [
  0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
  0xa1, 0xb1, 0xc1, 0x09, 0x23, 0x33, 0x52, 0xf0, 0x15, 0x62, 0x72, 0xd1, 0x0a, 0x16, 0x24, 0x34, 0xe1, 0x25, 0xf1, 0x17, 0x18, 0x19, 0x1a, 0x26,
  0x27, 0x28, 0x29, 0x2a, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
  0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
  0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4,
  0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda,
  0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
];

function huffTable(counts, values) {
  const table = new Array(256);
  let code = 0, k = 0;
  for (let len = 1; len <= 16; len++) {
    for (let i = 0; i < counts[len - 1]; i++) { table[values[k++]] = [code, len]; code++; }
    code <<= 1;
  }
  return table;
}
const DC_L = huffTable(DC_L_N, DC_L_V), DC_C = huffTable(DC_C_N, DC_C_V), AC_L = huffTable(AC_L_N, AC_L_V), AC_C = huffTable(AC_C_N, AC_C_V);

const COS = new Float64Array(64);
for (let u = 0; u < 8; u++) { for (let x = 0; x < 8; x++) { COS[u * 8 + x] = Math.cos(((2 * x + 1) * u * Math.PI) / 16); } }
const CU = [Math.SQRT1_2, 1, 1, 1, 1, 1, 1, 1];
const TMP = new Float64Array(64);

function scaledQuant(base, quality) {
  const q = Math.max(1, Math.min(100, quality | 0));
  const s = q < 50 ? 5000 / q : 200 - 2 * q;
  return base.map((v) => Math.max(1, Math.min(255, Math.floor((v * s + 50) / 100))));
}

class BitWriter {
  constructor() { this.bytes = []; this.acc = 0; this.n = 0; }
  write(code, len) {
    for (let i = len - 1; i >= 0; i--) {
      this.acc = (this.acc << 1) | ((code >> i) & 1); this.n++;
      if (this.n === 8) { this.bytes.push(this.acc); if (this.acc === 0xff) { this.bytes.push(0); } this.acc = 0; this.n = 0; }
    }
  }
  flush() { while (this.n) { this.write(1, 1); } }
}

function category(v) { let a = Math.abs(v), c = 0; while (a) { c++; a >>= 1; } return c; }
function bitsOf(v, cat) { return v >= 0 ? v : (v + (1 << cat) - 1); }

function encodeBlock(bw, block, qt, dc, ac, prevDc) {
  // separable 2-D DCT-II: rows then columns
  const tmp = TMP;
  for (let y = 0; y < 8; y++) {
    for (let u = 0; u < 8; u++) {
      let s = 0;
      for (let x = 0; x < 8; x++) { s += block[y * 8 + x] * COS[u * 8 + x]; }
      tmp[y * 8 + u] = s;
    }
  }
  const coef = new Int32Array(64);
  for (let u = 0; u < 8; u++) {
    for (let v = 0; v < 8; v++) {
      let s = 0;
      for (let y = 0; y < 8; y++) { s += tmp[y * 8 + u] * COS[v * 8 + y]; }
      coef[v * 8 + u] = Math.round((0.25 * CU[u] * CU[v] * s) / qt[v * 8 + u]);
    }
  }
  const zz = new Int32Array(64);
  for (let i = 0; i < 64; i++) { zz[i] = coef[ZIGZAG[i]]; }
  const diff = zz[0] - prevDc;
  const dcat = category(diff);
  bw.write(dc[dcat][0], dc[dcat][1]);
  if (dcat) { bw.write(bitsOf(diff, dcat), dcat); }
  let run = 0;
  for (let i = 1; i < 64; i++) {
    const v = zz[i];
    if (v === 0) { run++; continue; }
    while (run > 15) { bw.write(ac[0xf0][0], ac[0xf0][1]); run -= 16; }
    const cat = category(v);
    const sym = (run << 4) | cat;
    bw.write(ac[sym][0], ac[sym][1]); bw.write(bitsOf(v, cat), cat);
    run = 0;
  }
  if (run) { bw.write(ac[0][0], ac[0][1]); }
  return zz[0];
}

function marker(id, payload) { const b = Buffer.alloc(4 + payload.length); b[0] = 0xff; b[1] = id; b.writeUInt16BE(payload.length + 2, 2); payload.copy(b, 4); return b; }
function dht(cls, id, counts, values) { return Buffer.from([(cls << 4) | id, ...counts, ...values]); }

/** Encode RGBA (Uint8Array, w*h*4) as a baseline JPEG; returns a Buffer. */
export function encodeJpeg(rgba, w, h, quality = 70) {
  const ql = scaledQuant(LUMA_Q, quality), qc = scaledQuant(CHROMA_Q, quality);
  const zzq = (q) => { const o = new Array(64); for (let i = 0; i < 64; i++) { o[i] = q[ZIGZAG[i]]; } return o; };
  const parts = [Buffer.from([0xff, 0xd8])];
  parts.push(marker(0xe0, Buffer.from([0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0])));
  parts.push(marker(0xdb, Buffer.from([0, ...zzq(ql), 1, ...zzq(qc)])));
  const sof = Buffer.alloc(15); sof[0] = 8; sof.writeUInt16BE(h, 1); sof.writeUInt16BE(w, 3); sof[5] = 3;
  sof.set([1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1], 6);
  parts.push(marker(0xc0, sof));
  parts.push(marker(0xc4, Buffer.concat([dht(0, 0, DC_L_N, DC_L_V), dht(1, 0, AC_L_N, AC_L_V), dht(0, 1, DC_C_N, DC_C_V), dht(1, 1, AC_C_N, AC_C_V)])));
  parts.push(marker(0xda, Buffer.from([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0])));
  const bw = new BitWriter();
  const Y = new Float64Array(64), Cb = new Float64Array(64), Cr = new Float64Array(64);
  let pY = 0, pCb = 0, pCr = 0;
  for (let by = 0; by < h; by += 8) {
    for (let bx = 0; bx < w; bx += 8) {
      for (let y = 0; y < 8; y++) {
        const sy = Math.min(h - 1, by + y);
        for (let x = 0; x < 8; x++) {
          const sx = Math.min(w - 1, bx + x);
          const i = (sy * w + sx) * 4, r = rgba[i], g = rgba[i + 1], b = rgba[i + 2];
          Y[y * 8 + x] = 0.299 * r + 0.587 * g + 0.114 * b - 128;
          Cb[y * 8 + x] = -0.168736 * r - 0.331264 * g + 0.5 * b;
          Cr[y * 8 + x] = 0.5 * r - 0.418688 * g - 0.081312 * b;
        }
      }
      pY = encodeBlock(bw, Y, ql, DC_L, AC_L, pY);
      pCb = encodeBlock(bw, Cb, qc, DC_C, AC_C, pCb);
      pCr = encodeBlock(bw, Cr, qc, DC_C, AC_C, pCr);
    }
  }
  bw.flush();
  parts.push(Buffer.from(bw.bytes));
  parts.push(Buffer.from([0xff, 0xd9]));
  return Buffer.concat(parts);
}
