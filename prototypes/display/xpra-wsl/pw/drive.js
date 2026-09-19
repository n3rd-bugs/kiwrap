// Generic Playwright driver: node drive.js <url> <outprefix> <steps-json>
const { chromium } = require('playwright');
const fs = require('fs');
const [,, url, prefix, stepsFile] = process.argv;
const steps = JSON.parse(fs.readFileSync(stepsFile, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: false, args: ['--window-size=1620,1050'] });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error') console.log('console.error:', m.text().slice(0, 200)); });
  page.on('pageerror', e => console.log('pageerror:', String(e).slice(0, 200)));
  const cdp = await ctx.newCDPSession(page);
  let frames = []; let castOn = false;
  cdp.on('Page.screencastFrame', async (f) => { frames.push({ t: performance.now(), ts: f.metadata.timestamp }); try { await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }); } catch {} });
  async function cast(on) { if (on && !castOn) { await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 30, maxWidth: 800, maxHeight: 500, everyNthFrame: 1 }); castOn = true; } if (!on && castOn) { await cdp.send('Page.stopScreencast'); castOn = false; } }
  const t0 = Date.now();
  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  console.log('loaded in', Date.now() - t0, 'ms');
  for (const s of steps) {
    const ts = Date.now();
    try {
      switch (s.act) {
        case 'wait': await page.waitForTimeout(s.ms); break;
        case 'shot': await page.screenshot({ path: `${prefix}-${s.name}.png` }); break;
        case 'move': await page.mouse.move(s.x, s.y, { steps: s.steps || 5 }); break;
        case 'click': await page.mouse.click(s.x, s.y, { button: s.button || 'left', clickCount: s.count || 1 }); break;
        case 'dblclick': await page.mouse.dblclick(s.x, s.y); break;
        case 'down': await page.mouse.move(s.x, s.y); await page.mouse.down({ button: s.button || 'left' }); break;
        case 'up': await page.mouse.up({ button: s.button || 'left' }); break;
        case 'drag': await page.mouse.move(s.x1, s.y1); await page.mouse.down({ button: s.button || 'left' });
          await page.mouse.move(s.x2, s.y2, { steps: s.steps || 20 }); await page.mouse.up({ button: s.button || 'left' }); break;
        case 'wheel': await page.mouse.move(s.x, s.y); await page.mouse.wheel(0, s.dy); break;
        case 'key': await page.keyboard.press(s.key); break;
        case 'type': await page.keyboard.type(s.text, { delay: s.delay || 30 }); break;
        case 'eval': console.log('eval:', JSON.stringify(await page.evaluate(s.js))); break;
        case 'fpsdrag': {
          // continuous drag for s.ms milliseconds; count screencast frames (= visible updates) during it
          await cast(true); await page.waitForTimeout(300); frames = [];
          await page.mouse.move(s.x1, s.y1); await page.mouse.down({ button: s.button || 'left' });
          const start = performance.now(); let i = 0;
          while (performance.now() - start < s.ms) { i++; const f = (i % 40) / 40; await page.mouse.move(s.x1 + (s.x2 - s.x1) * f, s.y1 + (s.y2 - s.y1) * f); await page.waitForTimeout(8); }
          await page.mouse.up({ button: s.button || 'left' });
          const dur = performance.now() - start; await page.waitForTimeout(300);
          console.log(`fpsdrag: ${frames.length} screencast frames in ${dur.toFixed(0)} ms => ${(frames.length * 1000 / dur).toFixed(1)} fps; ${i} mouse moves sent`);
          await cast(false); break;
        }
        case 'latency': {
          // wheel zoom N times; per event measure time until first screencast frame arrives
          await cast(true); await page.mouse.move(s.x, s.y); await page.waitForTimeout(500);
          const res = [];
          for (let k = 0; k < (s.n || 5); k++) {
            frames = []; const t = performance.now();
            await page.mouse.wheel(0, (k % 2 ? 1 : -1) * (s.dy || 240));
            const deadline = t + 3000; while (frames.length === 0 && performance.now() < deadline) await page.waitForTimeout(5);
            res.push(frames.length ? Math.round(frames[0].t - t) : -1); await page.waitForTimeout(700);
          }
          console.log('latency (wheel -> first changed frame, ms):', res.join(', '));
          await cast(false); break;
        }
        case 'keylatency': {
          await cast(true); await page.waitForTimeout(500);
          const res = [];
          for (let k = 0; k < (s.n || 5); k++) {
            frames = []; const t = performance.now();
            await page.keyboard.press(s.key);
            const deadline = t + 3000; while (frames.length === 0 && performance.now() < deadline) await page.waitForTimeout(5);
            res.push(frames.length ? Math.round(frames[0].t - t) : -1); await page.waitForTimeout(700);
          }
          console.log(`keylatency ${s.key} (key -> first changed frame, ms):`, res.join(', '));
          await cast(false); break;
        }
      }
    } catch (e) { console.log('step failed', JSON.stringify(s), String(e).slice(0, 300)); }
    console.log(`  [${s.act}${s.name ? ' ' + s.name : ''}${s.key ? ' ' + s.key : ''}] ${Date.now() - ts} ms`);
  }
  await browser.close();
})();
