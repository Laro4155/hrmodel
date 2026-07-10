// visit_site.js — LAIro v10 Headless Auto-Visit
//
// Opens the live GitHub Pages site in a headless browser so that
// autoGenerateOnLoad() (refreshSlate() -> runModel() -> runAutoLock()/
// updateTrueTop10()) fires exactly as it would if you opened the tab
// yourself — without needing a human to actually be there.
//
// Run on a schedule via GitHub Actions (see .github/workflows/auto-visit-site.yml).
// Reuses the real client-side model/logic as-is; nothing is duplicated in Python.

const { chromium } = require('playwright');

const SITE_URL = 'https://laro4155.github.io/hrmodel/';
const WAIT_MS = 100000; // time to let refreshSlate()+runModel()+runAutoLock() fully complete

(async () => {
  console.log('=== LAIro Headless Auto-Visit ===');
  console.log('Launching headless browser...');
  const browser = await chromium.launch();
  const page = await browser.newPage();

  // Surface the app's own console/plog output in the Action log for debugging
  page.on('console', (msg) => console.log('[page]', msg.text()));
  page.on('pageerror', (err) => console.log('[page error]', err.message));

  console.log(`Navigating to ${SITE_URL} ...`);
  try {
    await page.goto(SITE_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (e) {
    console.error('Navigation failed:', e.message);
    await browser.close();
    process.exit(1);
  }

  console.log(`Page loaded — waiting ${WAIT_MS / 1000}s for auto-generate pipeline to complete...`);
  await page.waitForTimeout(WAIT_MS);

  console.log('Done — closing browser.');
  await browser.close();
})();
