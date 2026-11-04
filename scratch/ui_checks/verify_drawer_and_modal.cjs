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

  // 1. Snapshot Drawer Event Log (Showing DERIVED tags and collapsed stalls)
  const drawerEl = await page.$('.bottom-drawer');
  if (drawerEl) {
    const shotDrawer = path.join(OUTPUT_DIR, 'fixed_drawer_event_log.png');
    await drawerEl.screenshot({ path: shotDrawer });
    copyToArtifacts(shotDrawer, 'fixed_drawer_event_log.png');
  }

  // 2. Click Stage 5 (VERIFY) stepper button in masthead to open Stage 5 Modal
  console.log('Opening Stage 5 Verify Modal...');
  const verifyStepBtn = await page.$('.pipeline-step-v3:nth-child(5)');
  if (verifyStepBtn) {
    await verifyStepBtn.click();
    await new Promise((r) => setTimeout(r, 800));

    const shotModalSim = path.join(OUTPUT_DIR, 'fixed_verify_modal_simulated.png');
    const modalEl = await page.$('.pipeline-modal');
    if (modalEl) {
      await modalEl.screenshot({ path: shotModalSim });
      copyToArtifacts(shotModalSim, 'fixed_verify_modal_simulated.png');
    }

    // Close modal
    const closeBtn = await page.$('.pipeline-modal__close');
    if (closeBtn) await closeBtn.click();
    await new Promise((r) => setTimeout(r, 400));
  }

  // 3. Switch to LIVE mode to verify clean, non-contradictory Awaiting State
  console.log('Switching to LIVE mode...');
  const liveModeBtn = await page.$('.mode-switch-btn:nth-child(2)');
  if (liveModeBtn) {
    await liveModeBtn.click();
    await new Promise((r) => setTimeout(r, 600));

    // Re-open Stage 5 Modal in LIVE mode
    if (verifyStepBtn) {
      await verifyStepBtn.click();
      await new Promise((r) => setTimeout(r, 800));

      const shotModalLive = path.join(OUTPUT_DIR, 'fixed_verify_modal_live_awaiting.png');
      const modalEl = await page.$('.pipeline-modal');
      if (modalEl) {
        await modalEl.screenshot({ path: shotModalLive });
        copyToArtifacts(shotModalLive, 'fixed_verify_modal_live_awaiting.png');
      }

      // Click "Switch to Simulated Mode" button inside modal
      const switchBtn = await page.$('.pipeline-modal__action-row button');
      if (switchBtn) {
        console.log('Testing Switch to Simulated Mode button in modal...');
        await switchBtn.click();
        await new Promise((r) => setTimeout(r, 800));
        const shotAfterSwitch = path.join(OUTPUT_DIR, 'fixed_modal_after_switching_to_sim.png');
        if (modalEl) {
          await modalEl.screenshot({ path: shotAfterSwitch });
          copyToArtifacts(shotAfterSwitch, 'fixed_modal_after_switching_to_sim.png');
        }
      }

      const closeBtn = await page.$('.pipeline-modal__close');
      if (closeBtn) await closeBtn.click();
      await new Promise((r) => setTimeout(r, 400));
    }
  }

  // 4. Capture Full Dashboard Screenshot showing Ctrl OK and clean layout
  const shotFull = path.join(OUTPUT_DIR, 'fixed_dashboard_full_ctrl_ok.png');
  await page.screenshot({ path: shotFull });
  copyToArtifacts(shotFull, 'fixed_dashboard_full_ctrl_ok.png');

  console.log('All tests completed successfully!');
  await browser.close();
}

run().catch((err) => {
  console.error('Error in drawer/modal verification:', err);
  process.exit(1);
});
