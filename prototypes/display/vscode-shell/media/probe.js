// Shared input probe. Used by probe.html (webview document), demo.html (page inside an <iframe> served over
// http://localhost) and canvas.html. Every DOM input event is serialised and shipped to the extension host,
// which appends it to the JSONL log for the driver script.
(function () {
  'use strict';
  const inIframe = window.parent !== window;
  const vsapi = (typeof acquireVsCodeApi === 'function') ? acquireVsCodeApi() : null;
  const label = document.body.dataset.probeLabel || (inIframe ? 'iframe' : 'webview');
  const instance = document.body.dataset.instance || String(Math.floor(Math.random() * 1e9));
  const q = [];
  let flushTimer = null;

  function send(msg) {
    msg.src2 = label;
    q.push(msg);
    if (!flushTimer) { flushTimer = setTimeout(flush, 15); }
  }
  function flush() {
    flushTimer = null;
    if (!q.length) { return; }
    const batch = { kind: 'batch', events: q.splice(0, q.length) };
    if (vsapi) { vsapi.postMessage(batch); }
    else if (inIframe) { window.parent.postMessage({ kiwrapProbe: batch }, '*'); }
  }
  window.kiwrapProbeSend = send;

  const logEl = document.getElementById('log');
  const seen = [];
  function show(text) {
    if (!logEl) { return; }
    seen.push(text); if (seen.length > 40) { seen.shift(); }
    logEl.textContent = seen.join('\n');
  }

  function keyRec(e) {
    return { key: e.key, code: e.code, keyCode: e.keyCode, ctrl: e.ctrlKey, shift: e.shiftKey, alt: e.altKey, meta: e.metaKey, repeat: e.repeat, dp: e.defaultPrevented, loc: e.location, focus: document.hasFocus(), ae: (document.activeElement && document.activeElement.tagName) || null };
  }
  // Registered on window in bubble phase AFTER VS Code's own forwarding hook, so `dp` shows whether
  // VS Code's pre-script called preventDefault() on this key (it does for undo/redo/print/find/copy/paste/cut).
  window.addEventListener('keydown', (e) => { const r = keyRec(e); send({ ev: 'keydown', ...r }); if (armed.clipboard) { const tag = armed.clipboard; armed.clipboard = null; clipboardTest(tag); } show('keydown ' + e.key + ' ' + e.code + (e.ctrlKey ? ' ctrl' : '') + (e.shiftKey ? ' shift' : '') + (e.altKey ? ' alt' : '') + (e.defaultPrevented ? ' [dp]' : '')); if (!document.body.dataset.noPrevent) { e.preventDefault(); } });
  window.addEventListener('keyup', (e) => { send({ ev: 'keyup', ...keyRec(e) }); });
  window.addEventListener('keypress', (e) => { send({ ev: 'keypress', key: e.key, charCode: e.charCode }); });

  let moveCount = 0, lastMove = null, moveTimer = null;
  function ptrRec(e) { return { x: e.clientX, y: e.clientY, sx: e.screenX, sy: e.screenY, button: e.button, buttons: e.buttons, ptype: e.pointerType, ctrl: e.ctrlKey, shift: e.shiftKey, alt: e.altKey, mx: e.movementX, my: e.movementY, dp: e.defaultPrevented, locked: document.pointerLockElement !== null }; }
  window.addEventListener('pointerdown', (e) => { send({ ev: 'pointerdown', ...ptrRec(e) }); show('pointerdown b=' + e.button + ' @' + e.clientX + ',' + e.clientY); if (e.button === 1) { e.preventDefault(); } if (armed.pointerLock && e.button === 0) { armed.pointerLock = false; tryPointerLock(); } if (armed.fullscreen && e.button === 0) { armed.fullscreen = false; tryFullscreen(); } if (armed.clipboardClick && e.button === 0) { const tag = armed.clipboardClick; armed.clipboardClick = null; clipboardTest(tag); } });
  window.addEventListener('mousedown', (e) => { if (e.button === 1) { e.preventDefault(); } });
  window.addEventListener('pointerup', (e) => { send({ ev: 'pointerup', ...ptrRec(e) }); });
  window.addEventListener('pointermove', (e) => { moveCount++; lastMove = ptrRec(e); if (!moveTimer) { moveTimer = setTimeout(() => { moveTimer = null; send({ ev: 'pointermove', n: moveCount, ...lastMove }); moveCount = 0; }, 100); } });
  window.addEventListener('click', (e) => { send({ ev: 'click', ...ptrRec(e) }); });
  window.addEventListener('dblclick', (e) => { send({ ev: 'dblclick', ...ptrRec(e) }); });
  window.addEventListener('auxclick', (e) => { send({ ev: 'auxclick', ...ptrRec(e) }); e.preventDefault(); });
  window.addEventListener('contextmenu', (e) => { send({ ev: 'contextmenu', ...ptrRec(e) }); show('contextmenu'); e.preventDefault(); });
  window.addEventListener('wheel', (e) => { send({ ev: 'wheel', dx: e.deltaX, dy: e.deltaY, dz: e.deltaZ, mode: e.deltaMode, ctrl: e.ctrlKey, shift: e.shiftKey, alt: e.altKey, x: e.clientX, y: e.clientY, dp: e.defaultPrevented }); show('wheel dy=' + e.deltaY + (e.ctrlKey ? ' ctrl' : '')); e.preventDefault(); }, { passive: false });
  window.addEventListener('focus', () => send({ ev: 'winfocus', focus: document.hasFocus() }));
  window.addEventListener('blur', () => send({ ev: 'winblur', focus: document.hasFocus() }));
  document.addEventListener('visibilitychange', () => send({ ev: 'visibility', state: document.visibilityState }));
  window.addEventListener('resize', () => send({ ev: 'resize', ...dpiRec() }));
  document.addEventListener('paste', (e) => { const t = e.clipboardData ? e.clipboardData.getData('text/plain') : null; send({ ev: 'paste', text: t }); show('paste event: ' + t); });
  document.addEventListener('copy', (e) => { send({ ev: 'copy' }); });
  document.addEventListener('pointerlockchange', () => send({ ev: 'pointerlockchange', locked: document.pointerLockElement !== null }));
  document.addEventListener('pointerlockerror', () => send({ ev: 'pointerlockerror' }));
  document.addEventListener('fullscreenchange', () => send({ ev: 'fullscreenchange', fs: document.fullscreenElement !== null, ...dpiRec() }));
  document.addEventListener('fullscreenerror', () => send({ ev: 'fullscreenerror' }));

  function dpiRec() {
    return { dpr: window.devicePixelRatio, iw: window.innerWidth, ih: window.innerHeight, ow: window.outerWidth, oh: window.outerHeight, sw: screen.width, sh: screen.height, vvScale: window.visualViewport ? window.visualViewport.scale : null, dppx15: matchMedia('(resolution: 1.5dppx)').matches, dppx2: matchMedia('(resolution: 2dppx)').matches, screenX: window.screenX, screenY: window.screenY };
  }
  const armed = { pointerLock: false, fullscreen: false, clipboard: null, clipboardClick: null };
  function tryPointerLock() {
    const el = document.getElementById('target') || document.body;
    try {
      const p = el.requestPointerLock({ unadjustedMovement: true });
      if (p && p.then) { p.then(() => send({ ev: 'pointerlock', ok: true, locked: document.pointerLockElement !== null }), (err) => send({ ev: 'pointerlock', ok: false, err: String(err) })); }
      else { send({ ev: 'pointerlock', requested: true, locked: document.pointerLockElement !== null }); }
    } catch (err) { send({ ev: 'pointerlock', ok: false, err: String(err) }); }
    setTimeout(() => { send({ ev: 'pointerlock-after', locked: document.pointerLockElement !== null }); }, 400);
  }
  function tryFullscreen() {
    const el = document.getElementById('target') || document.documentElement;
    try {
      const p = el.requestFullscreen();
      if (p && p.then) { p.then(() => send({ ev: 'fullscreen', ok: true, fs: document.fullscreenElement !== null, ...dpiRec() }), (err) => send({ ev: 'fullscreen', ok: false, err: String(err) })); }
    } catch (err) { send({ ev: 'fullscreen', ok: false, err: String(err) }); }
  }
  async function clipboardTest(tag) {
    const text = 'kiwrap-probe-' + tag;
    let w = null, r = null, werr = null, rerr = null;
    try { await navigator.clipboard.writeText(text); w = true; } catch (e) { w = false; werr = String(e); }
    try { r = await navigator.clipboard.readText(); } catch (e) { rerr = String(e); }
    let ec = null; try { ec = document.execCommand('copy'); } catch (e) { ec = String(e); }
    send({ ev: 'clipboard', write: w, werr, read: r, rerr, execCopy: ec, perm: await permQuery('clipboard-read'), permW: await permQuery('clipboard-write') });
  }
  async function permQuery(name) { try { const s = await navigator.permissions.query({ name }); return s.state; } catch (e) { return 'n/a'; } }

  function handleControl(m) {
    if (!m || typeof m !== 'object') { return; }
    if (m.type === 'clipboard') { clipboardTest(m.tag || ''); }
    else if (m.type === 'armClipboard') { armed.clipboard = m.tag || 'k'; send({ ev: 'armed', what: 'clipboard' }); }
    else if (m.type === 'armClipboardClick') { armed.clipboardClick = m.tag || 'c'; send({ ev: 'armed', what: 'clipboardClick' }); }
    else if (m.type === 'armPointerLock') { armed.pointerLock = true; send({ ev: 'armed', what: 'pointerLock' }); }
    else if (m.type === 'armFullscreen') { armed.fullscreen = true; send({ ev: 'armed', what: 'fullscreen' }); }
    else if (m.type === 'exitPointerLock') { try { document.exitPointerLock(); } catch (e) { /* ignore */ } }
    else if (m.type === 'dpi') { send({ ev: 'dpi', ...dpiRec() }); }
    else if (m.type === 'focus') { const el = document.getElementById('target') || document.body; el.focus(); send({ ev: 'focused', focus: document.hasFocus(), ae: document.activeElement && document.activeElement.tagName }); }
    else if (m.type === 'forwardedKey') { send({ ev: 'forwardedKey', key: m.key }); show('forwardedKey ' + m.key); }
    else if (m.type === 'state') { if (vsapi) { const s = vsapi.getState() || {}; s.n = (s.n || 0) + 1; vsapi.setState(s); send({ ev: 'state', state: s }); } }
    else if (m.type === 'open') { send({ ev: 'open', file: m.file }); show('open ' + m.file); }
    if (window.kiwrapOnControl) { window.kiwrapOnControl(m); }
  }
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (d && d.kiwrapProbe) { // relayed from a nested iframe: forward upward
      if (vsapi) { vsapi.postMessage(d.kiwrapProbe); } else if (inIframe) { window.parent.postMessage(d, '*'); }
      return;
    }
    if (d && d.kiwrapControl) { handleControl(d.kiwrapControl); return; }
    handleControl(d);
    // relay control messages into a nested iframe if there is one
    const fr = document.querySelector('iframe');
    if (fr && fr.contentWindow && d && d.type) { fr.contentWindow.postMessage({ kiwrapControl: d }, '*'); }
  });

  const state = vsapi ? vsapi.getState() : null;
  send({ ev: 'loaded', instance, inIframe, origin: location.origin, href: location.href.slice(0, 80), state, ua: navigator.userAgent.slice(0, 120), ...dpiRec(), focus: document.hasFocus() });
  if (vsapi && !state) { vsapi.setState({ created: Date.now(), n: 0 }); }
  const t = document.getElementById('target'); if (t) { t.setAttribute('tabindex', '0'); t.focus(); }
  const info = document.getElementById('info'); if (info) { info.textContent = 'label=' + label + ' instance=' + instance + ' dpr=' + window.devicePixelRatio + ' ' + window.innerWidth + 'x' + window.innerHeight; }
})();
