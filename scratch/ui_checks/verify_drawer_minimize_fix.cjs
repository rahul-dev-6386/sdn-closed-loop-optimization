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
    localStorage.setItem('sdn.ui.drawerOpen', 'true');
    localStorage.setItem('sdn.ui.telemetryMode', 'simulated');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading dashboard...');
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 2000));

  // Inspect header bounding rect
  const headerInfo = await page.evaluate(() => {
    const header = document.querySelector('.bottom-drawer__header');
    const toggleBtn = document.querySelector('.drawer-toggle-btn');
    const canvas = document.querySelector('.canvas-main-area');
    const cell = document.querySelector('.drawer-grid-cell');
    const hRect = header ? header.getBoundingClientRect() : null;
    const cRect = canvas ? canvas.getBoundingClientRect() : null;
    const bRect = toggleBtn ? toggleBtn.getBoundingClientRect() : null;
    const cellRect = cell ? cell.getBoundingClientRect() : null;
    return {
      canvasBottom: cRect ? cRect.bottom : null,
      drawerTop: cellRect ? cellRect.top : null,
      headerTop: hRect ? hRect.top : null,
      headerHeight: hRect ? hRect.height : null,
      btnTop: bRect ? bRect.top : null,
      btnHeight: bRect ? bRect.height : null,
      btnText: toggleBtn ? toggleBtn.innerText : null,
      isOverlapping: cRect && hRect ? (cRect.bottom > hRect.top) : false,
    };
  });
  console.log('Header verification metrics:', JSON.stringify(headerInfo, null, 2));

  // 1. Snapshot Drawer Open (Showing FULL header, Minimize button, Filter dropdown)
  const shotOpen = path.join(OUTPUT_DIR, 'fixed_drawer_open_clean.png');
  const drawerCell = await page.$('.drawer-grid-cell');
  if (drawerCell) {
    await drawerCell.screenshot({ path: shotOpen });
    copyToArtifacts(shotOpen, 'fixed_drawer_open_clean.png');
  }

  // 2. Click Filter Dropdown in bottom drawer
  console.log('Opening drawer filter dropdown...');
  const filterTrigger = await page.$('.drawer-custom-select .custom-select-trigger');
  if (filterTrigger) {
    await filterTrigger.click();
    await new Promise((r) => setTimeout(r, 600));

    // Capture area around drawer and popover
    const shotDropdown = path.join(OUTPUT_DIR, 'fixed_drawer_dropdown_open.png');
    await page.screenshot({ path: shotDropdown });
    copyToArtifacts(shotDropdown, 'fixed_drawer_dropdown_open.png');

    // Close dropdown
    await filterTrigger.click();
    await new Promise((r) => setTimeout(r, 300));
  }

  // 3. Click Minimize button to collapse drawer
  console.log('Clicking Minimize button...');
  const toggleBtn = await page.$('.drawer-toggle-btn');
  if (toggleBtn) {
    await toggleBtn.click();
    await new Promise((r) => setTimeout(r, 600));

    const shotCollapsed = path.join(OUTPUT_DIR, 'fixed_drawer_collapsed.png');
    if (drawerCell) {
      await drawerCell.screenshot({ path: shotCollapsed });
      copyToArtifacts(shotCollapsed, 'fixed_drawer_collapsed.png');
    }

    // Re-click to Expand
    console.log('Clicking Expand button...');
    await toggleBtn.click();
    await new Promise((r) => setTimeout(r, 600));
  }

  // Full screenshot
  const shotFull = path.join(OUTPUT_DIR, 'fixed_full_view_with_drawer.png');
  await page.screenshot({ path: shotFull });
  copyToArtifacts(shotFull, 'fixed_full_view_with_drawer.png');

  console.log('Verification completed successfully!');
  await browser.close();
}

run().catch((err) => {
  console.error('Error running drawer verification:', err);
  process.exit(1);
});
