// Minimal RFC 6455 WebSocket server on top of node:http (no dependencies): text/binary/ping/pong/close,
// masked client frames, unmasked server frames, no extensions (permessage-deflate is refused on purpose:
// the real helper must not negotiate it either, see docs/DISPLAY.md 7.2).
import { createHash } from 'node:crypto';
import { EventEmitter } from 'node:events';

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

export class WebSocketConnection extends EventEmitter {
  constructor(socket) {
    super();
    this.socket = socket;
    this.buf = Buffer.alloc(0);
    this.closed = false;
    this.fragments = null;
    socket.on('data', (d) => this.onData(d));
    socket.on('close', () => { this.closed = true; this.emit('close'); });
    socket.on('error', (e) => { this.emit('error', e); });
  }
  onData(d) {
    this.buf = this.buf.length ? Buffer.concat([this.buf, d]) : d;
    for (;;) {
      if (this.buf.length < 2) { return; }
      const b0 = this.buf[0], b1 = this.buf[1];
      const fin = (b0 & 0x80) !== 0, op = b0 & 0x0f, masked = (b1 & 0x80) !== 0;
      let len = b1 & 0x7f, off = 2;
      if (len === 126) { if (this.buf.length < 4) { return; } len = this.buf.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (this.buf.length < 10) { return; } len = Number(this.buf.readBigUInt64BE(2)); off = 10; }
      const maskLen = masked ? 4 : 0;
      if (this.buf.length < off + maskLen + len) { return; }
      let payload = this.buf.subarray(off + maskLen, off + maskLen + len);
      if (masked) { const mask = this.buf.subarray(off, off + 4); const out = Buffer.allocUnsafe(len); for (let i = 0; i < len; i++) { out[i] = payload[i] ^ mask[i & 3]; } payload = out; }
      this.buf = this.buf.subarray(off + maskLen + len);
      this.onFrame(fin, op, payload);
    }
  }
  onFrame(fin, op, payload) {
    if (op === 0x8) { this.close(1000); return; }
    if (op === 0x9) { this.sendFrame(0xA, payload); return; }
    if (op === 0xA) { return; }
    if (op === 0x0) { if (this.fragments) { this.fragments.parts.push(payload); if (fin) { const f = this.fragments; this.fragments = null; this.deliver(f.op, Buffer.concat(f.parts)); } } return; }
    if (!fin) { this.fragments = { op, parts: [payload] }; return; }
    this.deliver(op, payload);
  }
  deliver(op, payload) { this.emit('message', op === 0x1 ? payload.toString('utf8') : payload, op === 0x2); }
  sendFrame(op, payload) {
    if (this.closed || this.socket.destroyed) { return false; }
    const len = payload.length;
    let header;
    if (len < 126) { header = Buffer.from([0x80 | op, len]); }
    else if (len < 65536) { header = Buffer.alloc(4); header[0] = 0x80 | op; header[1] = 126; header.writeUInt16BE(len, 2); }
    else { header = Buffer.alloc(10); header[0] = 0x80 | op; header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2); }
    this.socket.write(Buffer.concat([header, payload]));
    return true;
  }
  sendText(s) { return this.sendFrame(0x1, Buffer.from(s, 'utf8')); }
  sendBinary(b) { return this.sendFrame(0x2, Buffer.isBuffer(b) ? b : Buffer.from(b.buffer, b.byteOffset, b.byteLength)); }
  get bufferedAmount() { return this.socket.writableLength; }
  close(code = 1000, reason = '') {
    if (this.closed) { return; }
    const r = Buffer.from(reason, 'utf8'); const p = Buffer.alloc(2 + r.length); p.writeUInt16BE(code, 0); r.copy(p, 2);
    try { this.sendFrame(0x8, p); } catch { /* ignore */ }
    this.closed = true;
    this.socket.end(); setTimeout(() => this.socket.destroy(), 200).unref();
  }
}

/** Accept an HTTP upgrade; returns the connection or null (and answers 4xx) when it is not a valid handshake. */
export function acceptUpgrade(req, socket, head) {
  const key = req.headers['sec-websocket-key'];
  if (!key || String(req.headers.upgrade ?? '').toLowerCase() !== 'websocket') { socket.end('HTTP/1.1 400 Bad Request\r\n\r\n'); return null; }
  const accept = createHash('sha1').update(key + GUID).digest('base64');
  socket.write(['HTTP/1.1 101 Switching Protocols', 'Upgrade: websocket', 'Connection: Upgrade', `Sec-WebSocket-Accept: ${accept}`, '', ''].join('\r\n'));
  const conn = new WebSocketConnection(socket);
  if (head && head.length) { conn.onData(head); }
  return conn;
}
