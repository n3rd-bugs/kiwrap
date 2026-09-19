const { chromium } = require('playwright');
const url = process.argv[2];
(async () => {
  const browser = await chromium.launch({ headless: false, args: ['--window-size=1620,1050'] });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 }, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await ctx.newPage(); await page.goto(url); await page.waitForTimeout(4000);
  await page.evaluate(() => navigator.clipboard.writeText('stale'));
  await page.mouse.click(830, 500); await page.keyboard.press('Escape');
  await page.keyboard.press('a'); await page.waitForTimeout(6000);
  await page.keyboard.type('typed-inside-kicad-' + Date.now()); await page.waitForTimeout(500);
  await page.keyboard.press('Control+a'); await page.keyboard.press('Control+c'); await page.waitForTimeout(2000);
  console.log('browser clipboard after Ctrl+C in app:', JSON.stringify(await page.evaluate(() => navigator.clipboard.readText().catch(e => 'ERR ' + e))));
  await page.keyboard.press('Escape'); await page.waitForTimeout(800); await page.keyboard.press('Escape');
  await browser.close();
})();
