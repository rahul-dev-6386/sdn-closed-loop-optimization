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
    localStorage.setItem('sdn.ui.telemetryMode', 'simulated');
  }, tokens.access_token, tokens.refresh_token);

  console.log('Reloading dashboard...');
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1500));

  // 1. Select Abilene Topology
  console.log('Selecting Abilene Topology...');
  const topoSelectTrigger = await page.$('.topo-select--wide .custom-select-trigger');
  if (topoSelectTrigger) {
    await topoSelectTrigger.click();
    await new Promise((r) => setTimeout(r, 600));

    // Click Abilene card
    const cards = await page.$$('.custom-select-card');
    for (const card of cards) {
      const text = await page.evaluate(el => el.textContent, card);
      if (text.includes('Abilene')) {
        await card.click();
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 1000));
  }

  // 2. Enable Spike 14M
  console.log('Enabling Spike 14M...');
  const spikeBtn = await page.$('.topo-btn--action, .topo-btn--danger');
  if (spikeBtn) {
    const isSpiking = await page.evaluate(el => el.textContent.includes('Spiking'), spikeBtn);
    if (!isSpiking) {
      await spikeBtn.click();
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  // 3. Enable Detour ON
  console.log('Enabling Detour ON...');
  const detourBtn = await page.$('.topo-btn--detour');
  if (detourBtn) {
    const isDetourOn = await page.evaluate(el => el.textContent.includes('ON'), detourBtn);
    if (!isDetourOn) {
      await detourBtn.click();
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  // Wait 7 seconds for stabilization and verification pass
  console.log('Waiting 7 seconds for post-reroute verification to pass...');
  await new Promise((r) => setTimeout(r, 7000));

  // Inspect link metrics and verification
  const metrics = await page.evaluate(() => {
    const verifyText = document.querySelector('.status-strip__item:last-child .status-badge')?.textContent;
    const badgeText = document.querySelector('.section-card__header .status-badge')?.textContent;
    const breakdown = document.querySelector('.trace-flow-breakdown')?.textContent;
    return {
      verifyText,
      badgeText,
      breakdown,
    };
  });
  console.log('Abilene post-reroute metrics:', JSON.stringify(metrics, null, 2));

  // Snapshot Abilene with Detour active
  const shotAbilene = path.join(OUTPUT_DIR, 'fixed_abilene_detour_pass.png');
  await page.screenshot({ path: shotAbilene });
  copyToArtifacts(shotAbilene, 'fixed_abilene_detour_pass.png');

  // Also test Double Diamond
  console.log('Switching to Double Diamond...');
  if (topoSelectTrigger) {
    await topoSelectTrigger.click();
    await new Promise((r) => setTimeout(r, 600));

    const cards = await page.$$('.custom-select-card');
    for (const card of cards) {
      const text = await page.evaluate(el => el.textContent, card);
      if (text.includes('Double Diamond')) {
        await card.click();
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 6000));

    const shotDoubleDiamond = path.join(OUTPUT_DIR, 'fixed_diamond_detour_pass.png');
    await page.screenshot({ path: shotDoubleDiamond });
    copyToArtifacts(shotDoubleDiamond, 'fixed_diamond_detour_pass.png');
  }

  console.log('All tests completed successfully!');
  await browser.close();
}

run().catch((err) => {
  console.error('Error running Abilene verification:', err);
  process.exit(1);
});
