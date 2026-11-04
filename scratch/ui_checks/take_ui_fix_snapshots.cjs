const path = require('path');
const puppeteer = require(path.resolve(__dirname, '../future/dashboard/react-app/node_modules/puppeteer-core'));
const fs = require('fs');

const BACKEND_URL = 'http://127.0.0.1:8000';
const FRONTEND_URL = 'http://localhost:5173';
const CHROME_PATH = '/usr/bin/chromium';
const OUTPUT_DIR = path.resolve(__dirname, '../results/snapshots_ui_fixes');
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
    if (regResp.ok) {
      return await regResp.json();
    }
    throw new Error(`Login failed: ${resp.status}`);
  }
  return await resp.json();
}

function copyToArtifacts(srcPath, destName) {
  try {
    const destPath = path.join(ARTIFACT_DIR, destName);
    fs.copyFileSync(srcPath, destPath);
    console.log(`Copied to artifact: ${destPath}`);
  } catch (err) {
    console.warn(`Artifact copy error: ${err.message}`);
  }
}

async function run() {
  console.log('Logging in...');
  const tokens = await login();
  console.log('Token acquired.');

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  console.log('Launching Chromium...');
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
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });

  console.log('Navigating to frontend...');
  await page.goto(FRONTEND_URL, { waitUntil: 'domcontentloaded' });

  await page.evaluate((access, refresh) => {
    localStorage.setItem('sdn.access', access);
    localStorage.setItem('sdn.refresh', refresh);
    localStorage.setItem('sdn.ui.leftState', 'expanded');
    localStorage.setItem('sdn.ui.rightState', 'expanded');
    localStorage.setItem('sdn.ui.drawerOpen', 'false');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading dashboard...');
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 2000));

  // Snapshot 1: Dashboard with clean header, panel headers, and collapse buttons
  const shot1 = path.join(OUTPUT_DIR, '01_dashboard_fixed_ui.png');
  await page.screenshot({ path: shot1 });
  console.log(`Saved: ${shot1}`);
  copyToArtifacts(shot1, '01_dashboard_fixed_ui.png');

  // Snapshot 2: Strategy dropdown open
  console.log('Clicking Strategy dropdown...');
  const stratTrigger = await page.$('.strategy-custom-select .custom-select-trigger');
  if (stratTrigger) {
    await stratTrigger.click();
    await new Promise((r) => setTimeout(r, 600));
    const shot2 = path.join(OUTPUT_DIR, '02_strategy_dropdown_open.png');
    await page.screenshot({ path: shot2 });
    console.log(`Saved: ${shot2}`);
    copyToArtifacts(shot2, '02_strategy_dropdown_open.png');
    // Close it
    await stratTrigger.click();
    await new Promise((r) => setTimeout(r, 300));
  }

  // Snapshot 3: Topology Card Grid dropdown open
  console.log('Clicking Topology selector...');
  const topoTrigger = await page.$('.topo-select--wide .custom-select-trigger');
  if (topoTrigger) {
    await topoTrigger.click();
    await new Promise((r) => setTimeout(r, 600));
    const shot3 = path.join(OUTPUT_DIR, '03_topology_dropdown_open.png');
    await page.screenshot({ path: shot3 });
    console.log(`Saved: ${shot3}`);
    copyToArtifacts(shot3, '03_topology_dropdown_open.png');
    // Close it
    await topoTrigger.click();
    await new Promise((r) => setTimeout(r, 300));
  }

  // Snapshot 4: Trigger congestion spike and verify detour passes
  console.log('Clicking Spike button to test detour and verification...');
  const spikeBtn = await page.$('.topo-btn--action');
  if (spikeBtn) {
    await spikeBtn.click();
    // Wait 7 seconds for detour and dynamic verify engine evaluation
    console.log('Waiting 7 seconds for closed-loop detour evaluation...');
    await new Promise((r) => setTimeout(r, 7000));
    const shot4 = path.join(OUTPUT_DIR, '04_detour_verify_pass.png');
    await page.screenshot({ path: shot4 });
    console.log(`Saved: ${shot4}`);
    copyToArtifacts(shot4, '04_detour_verify_pass.png');
  }

  await browser.close();
  console.log('All snapshots captured successfully!');
}

run().catch((err) => {
  console.error('Error during snapshot run:', err);
  process.exit(1);
});
