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
    localStorage.setItem('sdn.ui.drawerOpen', 'false');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading dashboard...');
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 2000));

  // 1. Snapshot 2D Toolbar & Page
  const shot2D = path.join(OUTPUT_DIR, 'fixed_toolbar_2d.png');
  const toolbarEl = await page.$('.topo-toolbar');
  if (toolbarEl) {
    await toolbarEl.screenshot({ path: shot2D });
    copyToArtifacts(shot2D, 'fixed_toolbar_2d.png');
  }

  const shotFull2D = path.join(OUTPUT_DIR, 'fixed_dashboard_2d.png');
  await page.screenshot({ path: shotFull2D });
  copyToArtifacts(shotFull2D, 'fixed_dashboard_2d.png');

  // 2. Click 3D Toggle Button
  console.log('Switching to 3D mode...');
  const buttons = await page.$$('.segmented-btn');
  let btn3D = null;
  for (const b of buttons) {
    const text = await page.evaluate(el => el.textContent, b);
    if (text.includes('3D')) {
      btn3D = b;
      break;
    }
  }
  if (btn3D) {
    await btn3D.click();
    await new Promise((r) => setTimeout(r, 1500));
  }

  // 3. Snapshot 3D Toolbar & Page
  const shot3D = path.join(OUTPUT_DIR, 'fixed_toolbar_3d.png');
  if (toolbarEl) {
    await toolbarEl.screenshot({ path: shot3D });
    copyToArtifacts(shot3D, 'fixed_toolbar_3d.png');
  }

  const shotFull3D = path.join(OUTPUT_DIR, 'fixed_dashboard_3d.png');
  await page.screenshot({ path: shotFull3D });
  copyToArtifacts(shotFull3D, 'fixed_dashboard_3d.png');

  // 4. Click auto-rotate button
  console.log('Clicking auto-rotate button...');
  const rotateBtn = await page.$('button[aria-label="Toggle camera auto-rotation"]');
  if (rotateBtn) {
    await rotateBtn.click();
    await new Promise((r) => setTimeout(r, 800));
    const shotRotate = path.join(OUTPUT_DIR, 'fixed_toolbar_3d_rotate_active.png');
    await toolbarEl.screenshot({ path: shotRotate });
    copyToArtifacts(shotRotate, 'fixed_toolbar_3d_rotate_active.png');
  }

  // 5. Test 1280px viewport
  await page.setViewport({ width: 1280, height: 800, deviceScaleFactor: 1 });
  await new Promise((r) => setTimeout(r, 800));
  const shot1280 = path.join(OUTPUT_DIR, 'fixed_dashboard_1280_3d.png');
  await page.screenshot({ path: shot1280 });
  copyToArtifacts(shot1280, 'fixed_dashboard_1280_3d.png');

  console.log('Verification completed successfully!');
  await browser.close();
}

run().catch((err) => {
  console.error('Error running verification:', err);
  process.exit(1);
});
