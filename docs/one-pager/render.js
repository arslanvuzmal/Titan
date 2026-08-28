// Render the one-pager to a single-page A4 PDF.
//
// Run inside the browser-worker container, which already carries Chromium:
//   docker cp docs/one-pager titan-browser-worker-1:/tmp/op
//   docker exec titan-browser-worker-1 node /tmp/op/render.js
//
// The page is deliberately fixed at 297mm and the render asserts one page:
// a two-page "one-pager" is a different document, and finding that out from a
// recipient is the wrong way to find out.
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const dir = __dirname;
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + path.join(dir, 'one-pager.html'), { waitUntil: 'networkidle' });
  const out = path.join(dir, 'one-pager.pdf');
  await page.pdf({
    path: out,
    format: 'A4',
    printBackground: true,
    margin: { top: '0', right: '0', bottom: '0', left: '0' },
    preferCSSPageSize: true,
  });
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await browser.close();
  console.log('rendered', out, '| content height', height, 'px');
})();
