const path = require('path');
const puppeteer = require(path.resolve(__dirname, '../future/dashboard/react-app/node_modules/puppeteer-core'));
const fs = require('fs');

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
  return await resp.json();
}

(async () => {
  const tokens = await login();

  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  await page.goto(FRONTEND_URL, { waitUntil: 'domcontentloaded' });
  await page.evaluate((access, refresh) => {
    localStorage.setItem('sdn.access', access);
    localStorage.setItem('sdn.refresh', refresh);
    localStorage.setItem('sdn.ui.leftState', 'expanded');
    localStorage.setItem('sdn.ui.rightState', 'expanded');
    localStorage.setItem('sdn.ui.drawerOpen', 'false');
  }, tokens.access_token, tokens.refresh_token);

  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1000));

  await page.evaluate(() => {
    const tabs = Array.from(document.querySelectorAll('.masthead__view-tab'));
    const t = tabs.find(el => el.textContent.includes('Analytics'));
    if (t) t.click();
  });
  await new Promise((r) => setTimeout(r, 2000));

  // Snapshot 1: Top section (Executive Summary + Figure 1)
  await page.screenshot({
    path: path.join(ARTIFACT_DIR, 'research_analytics_executive_and_fig1.png'),
  });
  console.log('Saved research_analytics_executive_and_fig1.png');

  // Scroll to Figure 4 & Figure 7
  await page.evaluate(() => {
    const el = document.getElementById('fig-4');
    if (el) el.scrollIntoView();
  });
  await new Promise((r) => setTimeout(r, 1000));
  await page.screenshot({
    path: path.join(ARTIFACT_DIR, 'research_analytics_fig4_fig7.png'),
  });
  console.log('Saved research_analytics_fig4_fig7.png');

  // Scroll to Figure 13 (Abilene WAN)
  await page.evaluate(() => {
    const el = document.getElementById('fig-13');
    if (el) el.scrollIntoView();
  });
  await new Promise((r) => setTimeout(r, 1000));
  await page.screenshot({
    path: path.join(ARTIFACT_DIR, 'research_analytics_fig13_abilene_wan.png'),
  });
  console.log('Saved research_analytics_fig13_abilene_wan.png');

  // Scroll to Figure 14 (Pipeline Progress)
  await page.evaluate(() => {
    const el = document.getElementById('fig-14');
    if (el) el.scrollIntoView();
  });
  await new Promise((r) => setTimeout(r, 1000));
  await page.screenshot({
    path: path.join(ARTIFACT_DIR, 'research_analytics_fig14_pipeline_progress.png'),
  });
  console.log('Saved research_analytics_fig14_pipeline_progress.png');

  await browser.close();
  console.log('All research analytics snapshots completed.');
})();
