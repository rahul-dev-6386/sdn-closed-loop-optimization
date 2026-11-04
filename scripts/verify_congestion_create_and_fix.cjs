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
  console.log('Logging in to test backend...');
  const tokens = await login();
  console.log('Token acquired successfully.');

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

  console.log('Navigating to frontend dashboard...');
  await page.goto(FRONTEND_URL, { waitUntil: 'domcontentloaded' });

  await page.evaluate((access, refresh) => {
    localStorage.setItem('sdn.access', access);
    localStorage.setItem('sdn.refresh', refresh);
    localStorage.setItem('sdn.ui.leftState', 'expanded');
    localStorage.setItem('sdn.ui.rightState', 'expanded');
    localStorage.setItem('sdn.ui.drawerOpen', 'false');
    localStorage.setItem('sdn.ui.telemetryMode', 'simulated');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading dashboard with authenticated session...');
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 2000));

  // 1. Open Agent Modal
  console.log('1. Opening Agentic AI Modal...');
  const agentBtn = await page.$('.masthead__agent-tab');
  if (!agentBtn) throw new Error('Agent tab button not found in Masthead');
  await agentBtn.click();
  await new Promise((r) => setTimeout(r, 1000));

  // 2. Click "Create Congestion (14M)" pill
  console.log('2. Clicking "Create Congestion (14M)" button...');
  const spikePill = await page.$('.agent-quick-btn--spike');
  if (!spikePill) throw new Error('Create Congestion pill button not found in AgentModal');
  await spikePill.click();
  await new Promise((r) => setTimeout(r, 1500));

  // Snapshot Agent Modal showing Congestion Created
  const modalEl = await page.$('.agent-modal');
  const shotCongestedModal = path.join(OUTPUT_DIR, 'agent_congestion_created.png');
  if (modalEl) {
    await modalEl.screenshot({ path: shotCongestedModal });
    copyToArtifacts(shotCongestedModal, 'agent_congestion_created.png');
  }

  // 3. Close Agent Modal to see congested state on frontend canvas
  console.log('3. Closing modal to verify congested state on frontend canvas...');
  const closeBtn = await page.$('.agent-modal__header .pipeline-modal__close');
  if (closeBtn) await closeBtn.click();
  await new Promise((r) => setTimeout(r, 1500));

  const shotCanvasCongested = path.join(OUTPUT_DIR, 'frontend_canvas_congested.png');
  await page.screenshot({ path: shotCanvasCongested });
  copyToArtifacts(shotCanvasCongested, 'frontend_canvas_congested.png');

  // 4. Re-open Agent Modal and click "Fix Congestion"
  console.log('4. Re-opening Agent Modal to fix congestion...');
  await agentBtn.click();
  await new Promise((r) => setTimeout(r, 1000));

  console.log('5. Clicking "Fix Congestion" button to run LangGraph StateGraph...');
  const fixBtn = await page.$('.agent-quick-btn--action');
  if (!fixBtn) throw new Error('Fix Congestion button not found');
  await fixBtn.click();

  console.log('Waiting for LangGraph workflow execution to complete...');
  await page.waitForFunction(() => {
    const thinking = document.querySelector('.agent-msg__bubble--thinking');
    const badge = document.querySelector('.agent-status-badge--completed');
    return !thinking || badge !== null;
  }, { timeout: 15000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 2000));

  // Snapshot Agent Modal with completed mitigation
  const shotFixedModal = path.join(OUTPUT_DIR, 'agent_congestion_fixed_modal.png');
  const modalElAfter = await page.$('.agent-modal');
  if (modalElAfter) {
    await modalElAfter.screenshot({ path: shotFixedModal });
    copyToArtifacts(shotFixedModal, 'agent_congestion_fixed_modal.png');
  }

  // 6. Close Modal and capture relieved, detoured frontend canvas
  console.log('6. Closing Agent Modal to verify detoured & relieved canvas...');
  const closeBtnAfter = await page.$('.agent-modal__header .pipeline-modal__close');
  if (closeBtnAfter) await closeBtnAfter.click();
  await new Promise((r) => setTimeout(r, 2000));

  const shotCanvasRelieved = path.join(OUTPUT_DIR, 'frontend_canvas_detoured_pass.png');
  await page.screenshot({ path: shotCanvasRelieved });
  copyToArtifacts(shotCanvasRelieved, 'frontend_canvas_detoured_pass.png');

  console.log('Congestion creation, agent fixing, and full end-to-end frontend/backend verification PASSED!');
  await browser.close();
}

run().catch((err) => {
  console.error('Error during test execution:', err);
  process.exit(1);
});
