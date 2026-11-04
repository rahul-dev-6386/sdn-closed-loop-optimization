/**
 * Automated dashboard snapshot script for Round 3.
 * Captures comprehensive screenshots across viewports and panel configurations:
 * 1. 1920px Full HD Perspective / 2D View
 * 2. 1280px Standard Viewport
 * 3. Structured OpenFlow Drawer with parsed flow rules
 * 4. Pinned Events stream with consolidated ticking stall
 * 5. Topology Selection Card Grid
 * 6. Focus Mode (F shortcut) HUD
 */
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const BACKEND_URL = 'http://127.0.0.1:8000';
const FRONTEND_URL = 'http://127.0.0.1:5173';
const CHROME_PATH = '/usr/bin/google-chrome-stable';
const OUTPUT_DIR = path.resolve(__dirname, '../results/snapshots_ui_r3');
const ARTIFACT_DIR = '/home/rahul/.gemini/antigravity-cli/brain/3d25a916-0fd2-4678-b4dc-620362dd57e0';

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
    if (regResp.ok) {
      return await regResp.json();
    }
    throw new Error(`Login failed with status ${resp.status}`);
  }
  return await resp.json();
}

async function copyToArtifacts(srcPath, destName) {
  try {
    const destPath = path.join(ARTIFACT_DIR, destName);
    fs.copyFileSync(srcPath, destPath);
    console.log(`Copied snapshot to artifact: ${destPath}`);
  } catch (err) {
    console.warn(`Artifact copy error: ${err.message}`);
  }
}

async function main() {
  console.log('Logging in to backend...');
  const tokens = await login();
  console.log('Obtained JWT access token successfully.');

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  console.log('Launching headless Chrome...');
  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: 'new',
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage',
      '--no-first-run',
    ],
  });

  const page = await browser.newPage();

  // 1. Capture 1920px Full HD View
  await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  console.log('Navigating to frontend...');
  await page.goto(FRONTEND_URL, { waitUntil: 'domcontentloaded' });

  await page.evaluate((access, refresh) => {
    localStorage.setItem('sdn.access', access);
    localStorage.setItem('sdn.refresh', refresh);
    localStorage.setItem('sdn.ui.leftState', 'expanded');
    localStorage.setItem('sdn.ui.rightState', 'expanded');
    localStorage.setItem('sdn.ui.drawerOpen', 'false');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading authenticated dashboard (1920x1080)...');
  await page.goto(FRONTEND_URL, { waitUntil: 'domcontentloaded' });
  await new Promise((r) => setTimeout(r, 2500));

  const shot1920 = path.join(OUTPUT_DIR, 'dashboard_r3_1920.png');
  await page.screenshot({ path: shot1920 });
  console.log(`Saved 1920px screenshot: ${shot1920}`);
  await copyToArtifacts(shot1920, 'dashboard_r3_1920.png');

  // 2. Open Topology CustomSelect Card Grid
  try {
    console.log('Opening Topology card grid popover...');
    const topoSelectTrigger = await page.$('.topo-select--wide .custom-select-trigger');
    if (topoSelectTrigger) {
      await topoSelectTrigger.click();
      await new Promise((r) => setTimeout(r, 800));
      const shotTopoGrid = path.join(OUTPUT_DIR, 'dashboard_r3_topology_grid.png');
      await page.screenshot({ path: shotTopoGrid });
      console.log(`Saved Topology grid screenshot: ${shotTopoGrid}`);
      await copyToArtifacts(shotTopoGrid, 'dashboard_r3_topology_grid.png');
      // Close popover
      await topoSelectTrigger.click();
      await new Promise((r) => setTimeout(r, 400));
    }
  } catch (err) {
    console.warn('Topology grid trigger error:', err.message);
  }

  // 3. Open Bottom Drawer with OpenFlow Tables Tab
  try {
    console.log('Expanding bottom drawer to inspect OpenFlow Tables...');
    await page.evaluate(() => {
      // Toggle drawer open via keyboard shortcut '\'
      window.dispatchEvent(new KeyboardEvent('keydown', { key: '\\', bubbles: true }));
    });
    await new Promise((r) => setTimeout(r, 800));

    // Click OpenFlow tab
    const tabs = await page.$$('.drawer-tab');
    if (tabs.length >= 2) {
      await tabs[1].click(); // OpenFlow tab
      await new Promise((r) => setTimeout(r, 1200));

      const shotOpenFlow = path.join(OUTPUT_DIR, 'dashboard_r3_openflow.png');
      await page.screenshot({ path: shotOpenFlow });
      console.log(`Saved OpenFlow table screenshot: ${shotOpenFlow}`);
      await copyToArtifacts(shotOpenFlow, 'dashboard_r3_openflow.png');

      // Click back to Events tab
      await tabs[0].click();
      await new Promise((r) => setTimeout(r, 1000));
      const shotEvents = path.join(OUTPUT_DIR, 'dashboard_r3_events.png');
      await page.screenshot({ path: shotEvents });
      console.log(`Saved Controller Events screenshot: ${shotEvents}`);
      await copyToArtifacts(shotEvents, 'dashboard_r3_events.png');
    }
  } catch (err) {
    console.warn('Drawer screenshot error:', err.message);
  }

  // 4. Capture 1280px Standard Responsive View
  console.log('Testing 1280x800 viewport...');
  await page.setViewport({ width: 1280, height: 800, deviceScaleFactor: 1 });
  await new Promise((r) => setTimeout(r, 1500));
  const shot1280 = path.join(OUTPUT_DIR, 'dashboard_r3_1280.png');
  await page.screenshot({ path: shot1280 });
  console.log(`Saved 1280px responsive screenshot: ${shot1280}`);
  await copyToArtifacts(shot1280, 'dashboard_r3_1280.png');

  // 5. Focus Mode (F key)
  try {
    console.log('Triggering Focus Mode...');
    await page.evaluate(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', bubbles: true }));
    });
    await new Promise((r) => setTimeout(r, 1000));
    const shotFocus = path.join(OUTPUT_DIR, 'dashboard_r3_focus_mode.png');
    await page.screenshot({ path: shotFocus });
    console.log(`Saved Focus Mode screenshot: ${shotFocus}`);
    await copyToArtifacts(shotFocus, 'dashboard_r3_focus_mode.png');
  } catch (err) {
    console.warn('Focus mode error:', err.message);
  }

  await browser.close();
  console.log('All Round 3 snapshots completed successfully!');
}

main().catch((err) => {
  console.error('Snapshot script failed:', err);
  process.exit(1);
});
