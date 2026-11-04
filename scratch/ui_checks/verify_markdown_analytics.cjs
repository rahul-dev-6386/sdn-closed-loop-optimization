const path = require('path');
const puppeteer = require(path.resolve(__dirname, '../future/dashboard/react-app/node_modules/puppeteer-core'));

const BACKEND_URL = 'http://127.0.0.1:8000';
const FRONTEND_URL = 'http://localhost:5173';
const CHROME_PATH = '/usr/bin/chromium';
const ARTIFACT_DIR = '/home/rahul/.gemini/antigravity-cli/brain/731116c9-58c5-4603-8686-e5a24568a360';

async function login() {
  const resp = await fetch(`${BACKEND_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'adminuser', password: 'adminpassword123' }),
  });
  if (!resp.ok) {
    const regResp = await fetch(`${BACKEND_URL}/api/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'adminuser', password: 'adminpassword123' }),
    });
    if (regResp.ok) return await regResp.json();
    throw new Error(`Login failed: ${resp.status}`);
  }
  return await resp.json();
}

(async () => {
  console.log('Logging in...');
  const tokens = await login();
  console.log('Token acquired.');

  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--window-size=1440,900'],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  console.log('Navigating to frontend...');
  await page.goto(FRONTEND_URL, { waitUntil: 'domcontentloaded' });

  await page.evaluate((access, refresh) => {
    localStorage.setItem('sdn.access', access);
    localStorage.setItem('sdn.refresh', refresh);
    localStorage.setItem('sdn.ui.leftState', 'expanded');
    localStorage.setItem('sdn.ui.rightState', 'expanded');
    localStorage.setItem('sdn.ui.drawerOpen', 'false');
    localStorage.setItem('sdn.ui.telemetryMode', 'simulated');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading dashboard...');
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1500));

  // Click on Analytics view tab in header masthead
  console.log('Clicking Analytics view tab...');
  await page.evaluate(() => {
    const tabs = Array.from(document.querySelectorAll('.masthead__view-tab'));
    const t = tabs.find(el => el.textContent.includes('Analytics'));
    if (t) t.click();
  });
  await new Promise((r) => setTimeout(r, 2000));

  // 1. Snapshot: Visual Publication Figures with Markdown Callouts & Formulas
  console.log('Taking snapshot of upgraded Visual Publication Figures...');
  const outPath1 = path.join(ARTIFACT_DIR, 'analytics_markdown_figures_view.png');
  await page.screenshot({ path: outPath1 });
  console.log('Saved:', outPath1);

  // Scroll down to inspect Figure 3 & Figure 4 with their rich markdown
  await page.evaluate(() => {
    const el = document.getElementById('fig-3');
    if (el) el.scrollIntoView();
  });
  await new Promise((r) => setTimeout(r, 1000));
  const outPathFig3 = path.join(ARTIFACT_DIR, 'analytics_markdown_fig3_fig4.png');
  await page.screenshot({ path: outPathFig3 });
  console.log('Saved:', outPathFig3);

  // 2. Click Mode Tab: "Empirical Markdown Report (RESULTS.md)"
  console.log('Switching to Empirical Markdown Report tab...');
  const tabClicked = await page.evaluate(() => {
    const tabs = Array.from(document.querySelectorAll('.analytics-mode-tab'));
    const mdTab = tabs.find(el => el.textContent.includes('Markdown Report'));
    if (mdTab) {
      mdTab.click();
      return true;
    }
    return false;
  });
  console.log('Clicked Markdown Report tab:', tabClicked);
  await new Promise((r) => setTimeout(r, 1500));

  // Snapshot: Rendered RESULTS.md document with badges and tables
  const outPath2 = path.join(ARTIFACT_DIR, 'analytics_markdown_report_tab.png');
  await page.screenshot({ path: outPath2 });
  console.log('Saved:', outPath2);

  await browser.close();
  console.log('All verification snapshots completed successfully.');
})();
