// clipboard + hidpi + reconnect test for xpra html5
const { chromium } = require('playwright');
const url = process.argv[2]; const prefix = process.argv[3];
(async () => {
  const browser = await chromium.launch({ headless: false, args: ['--window-size=1620,1050'] });
  // --- 1. clipboard: browser -> app. Grant permissions, write text to the browser clipboard, paste into KiCad (Ctrl+V)
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 }, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await ctx.newPage();
  await page.goto(url); await page.waitForTimeout(4000);
  await page.evaluate(() => navigator.clipboard.writeText('hello-from-browser-clipboard'));
  await page.mouse.click(830, 500); await page.keyboard.press('Escape');
  // open the text tool dialog? simpler: use the footprint chooser search field and paste into it
  await page.keyboard.press('a'); await page.waitForTimeout(6000);
  await page.keyboard.press('Control+v'); await page.waitForTimeout(1500);
  await page.screenshot({ path: `${prefix}-30-paste-into-chooser.png` });
  // app -> browser: select the text in the search field and copy
  await page.keyboard.press('Control+a'); await page.keyboard.press('Control+c'); await page.waitForTimeout(1500);
  const txt = await page.evaluate(() => navigator.clipboard.readText().catch(e => 'ERR ' + e));
  console.log('browser clipboard after Ctrl+C in app:', JSON.stringify(txt));
  await page.keyboard.press('Escape'); await page.waitForTimeout(800); await page.keyboard.press('Escape');
  await page.close(); await ctx.close();
  // --- 2. reconnect: new context, app should still be there with state
  const ctx2 = await browser.newContext({ viewport: { width: 1600, height: 950 } });
  const p2 = await ctx2.newPage(); await p2.goto(url); await p2.waitForTimeout(4000);
  await p2.screenshot({ path: `${prefix}-31-reconnect.png` }); await ctx2.close();
  // --- 3. HiDPI: deviceScaleFactor 2
  const ctx3 = await browser.newContext({ viewport: { width: 1600, height: 950 }, deviceScaleFactor: 2 });
  const p3 = await ctx3.newPage(); await p3.goto(url); await p3.waitForTimeout(5000);
  console.log('hidpi eval:', JSON.stringify(await p3.evaluate(() => ({ dpr: devicePixelRatio, canvases: [...document.querySelectorAll('canvas')].map(c => c.width + 'x' + c.height + ' css ' + c.clientWidth + 'x' + c.clientHeight) }))));
  await p3.screenshot({ path: `${prefix}-32-hidpi2x.png` });
  await p3.screenshot({ path: `${prefix}-33-hidpi2x-crop.png`, clip: { x: 0, y: 0, width: 500, height: 200 } });
  await ctx3.close();
  await browser.close();
})();
