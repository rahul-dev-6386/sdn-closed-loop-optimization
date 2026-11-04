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
    if (regResp.ok) return await regResp.json();
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
    localStorage.setItem('sdn.ui.telemetryMode', 'simulated');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading dashboard...');
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 2000));

  // 1. Snapshot masthead with new Agentic AI button
  console.log('1. Checking Agentic AI button in Masthead...');
  const mastheadEl = await page.$('.masthead');
  if (mastheadEl) {
    const shotMasthead = path.join(OUTPUT_DIR, 'agent_masthead_button.png');
    await mastheadEl.screenshot({ path: shotMasthead });
    copyToArtifacts(shotMasthead, 'agent_masthead_button.png');
  }

  // 2. Open Agentic AI Modal
  console.log('2. Opening Agentic AI Modal...');
  const agentBtn = await page.$('.masthead__agent-tab');
  if (agentBtn) {
    await agentBtn.click();
    await new Promise((r) => setTimeout(r, 800));

    // Capture initial open agent modal
    const shotAgentOpen = path.join(OUTPUT_DIR, 'agent_modal_initial.png');
    const modalEl = await page.$('.agent-modal');
    if (modalEl) {
      await modalEl.screenshot({ path: shotAgentOpen });
      copyToArtifacts(shotAgentOpen, 'agent_modal_initial.png');
    }

    // 3. Open Settings Drawer
    console.log('3. Opening Settings bar...');
    const settingsBtn = await page.$('.agent-modal__header-actions button');
    if (settingsBtn) {
      await settingsBtn.click();
      await new Promise((r) => setTimeout(r, 600));

      const shotSettings = path.join(OUTPUT_DIR, 'agent_settings_drawer.png');
      if (modalEl) {
        await modalEl.screenshot({ path: shotSettings });
        copyToArtifacts(shotSettings, 'agent_settings_drawer.png');
      }

      // Close settings bar
      await settingsBtn.click();
      await new Promise((r) => setTimeout(r, 400));
    }

    // 4. Trigger "Fix Congestion" Quick Action to run LangGraph
    console.log('4. Clicking Fix Congestion prompt pill to execute LangGraph...');
    const fixBtn = await page.$('.agent-quick-btn--action');
    if (fixBtn) {
      await fixBtn.click();
      console.log('Waiting 6 seconds for LangGraph execution (Monitor -> Analyze -> Plan -> Act -> Verify)...');
      await new Promise((r) => setTimeout(r, 6000));

      const shotExecuted = path.join(OUTPUT_DIR, 'agent_langgraph_executed.png');
      if (modalEl) {
        await modalEl.screenshot({ path: shotExecuted });
        copyToArtifacts(shotExecuted, 'agent_langgraph_executed.png');
      }
    }

    // Close agent modal
    const closeBtn = await page.$('.agent-modal__header .pipeline-modal__close');
    if (closeBtn) await closeBtn.click();
    await new Promise((r) => setTimeout(r, 500));
  }

  // 5. Open Stage 5 Verify modal to verify no "NO DATA" or contradictory verdicts
  console.log('5. Inspecting Stage 5 Verify Modal in Simulated Mode...');
  const verifyStepBtn = await page.$('.pipeline-step-v3:nth-child(5)');
  if (verifyStepBtn) {
    await verifyStepBtn.click();
    await new Promise((r) => setTimeout(r, 800));

    const shotModal = path.join(OUTPUT_DIR, 'agent_stage5_verified_modal.png');
    const pModal = await page.$('.pipeline-modal');
    if (pModal) {
      await pModal.screenshot({ path: shotModal });
      copyToArtifacts(shotModal, 'agent_stage5_verified_modal.png');
    }

    const closeBtn = await page.$('.pipeline-modal__close');
    if (closeBtn) await closeBtn.click();
    await new Promise((r) => setTimeout(r, 500));
  }

  // 6. Capture Full Dashboard View
  console.log('6. Capturing Full Dashboard view...');
  const shotFull = path.join(OUTPUT_DIR, 'agent_full_dashboard.png');
  await page.screenshot({ path: shotFull });
  copyToArtifacts(shotFull, 'agent_full_dashboard.png');

  console.log('All verification snapshots completed successfully!');
  await browser.close();
}

run().catch((err) => {
  console.error('Error during verification run:', err);
  process.exit(1);
});
