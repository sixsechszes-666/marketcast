/* ─────────────────────────────────────────────────────────────────────────
   record.js — turns a Polymarket dashboard (HTML) into a 1:1 MP4 video.

   What it does:
     Opens one of four self-contained dashboard HTML files (under ./dashboards)
     in a headless Chromium via Playwright, lets the deck play itself once,
     captures it at 1080x1080, and re-encodes a clean MP4 (metadata stripped)
     into ./videos. Optionally muxes a background-music bed (see addmusic.js).

   The dashboards are driven entirely through the URL query string and a global:
     ?event=<slug>   classic / grid event decks
     ?addr=<wallet>  classic / grid trader decks
     ?hide=<id,id>   skip those charts/panels by id
     window.__aiCopy LLM-written { hook, verdict, analysis } injected pre-mount
   They signal progress back via window.__rec.{ready,finished} and window.__recError.

   Usage:
     node record.js <url>                       → record one event (classic)
     node record.js <url1> <url2> ...           → batch
     node record.js --grid <url>                → record the event grid version
     node record.js --trader <wallet>           → record the trader grid version
     node record.js --trader-classic <wallet>   → record the classic trader deck
     node record.js --hide=id1,id2 <url>        → skip those charts/panels by id
     node record.js --copy=copy.json <url>      → inject window.__aiCopy
     node record.js --no-music <url>            → skip the music mux step

   Dashboard mapping:
     (default) → dashboards/EventDashboard.html
     --grid    → dashboards/EventDashboardGrid.html
     --trader  → dashboards/DashboardGrid.html
     --trader-classic → dashboards/Dashboard.html

   Requires ffmpeg on PATH and Chromium (`npx playwright install chromium`).
   Normally invoked by the Python pipeline (marketcast.cli) as
   `node record.js <flags> <url>`.
   ───────────────────────────────────────────────────────────────────────── */
'use strict';

const path     = require('path');
const fs       = require('fs');
const os       = require('os');
const readline = require('readline');
const { spawnSync } = require('child_process');
const { chromium } = require('playwright');
const addmusic = require('./addmusic');

const OUT_DIR   = path.join(__dirname, 'videos');
const SIZE      = 1080;   // square — 1:1
const FPS       = 60;
const HOLD_MS   = 5000;   // how long to linger on the closing card

/* pull the event slug out of a pasted URL or a bare slug */
function slugFromInput(raw) {
  const s = (raw || '').trim();
  const m = s.match(/event\/([a-z0-9-]+)/i);
  if (m) return m[1].toLowerCase();
  if (/^[a-z0-9-]+$/i.test(s) && s.includes('-')) return s.toLowerCase();
  return null;
}

/* pull a wallet address out of a pasted profile URL or a bare 0x address */
function addrFromInput(raw) {
  const s = (raw || '').trim();
  const m = s.match(/profile\/(0x[a-fA-F0-9]{40})/i);
  if (m) return m[1].toLowerCase();
  if (/^0x[a-fA-F0-9]{40}$/i.test(s)) return s.toLowerCase();
  return null;
}

function ask(question) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise(res => rl.question(question, a => { rl.close(); res(a); }));
}

function checkFfmpeg() {
  const r = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' });
  if (r.error) {
    console.error('✗ ffmpeg not found on PATH — install it first.');
    process.exit(1);
  }
}

async function record(input, dashboard, tag, mode, hide, aiCopy) {
  const trader = mode === 'trader';
  const id = trader ? addrFromInput(input) : slugFromInput(input);
  if (!id) { console.error(`  ✗ "${input}" is not a valid ${trader ? 'wallet/profile' : 'event link/slug'} — skipped\n`); return; }

  console.log(`▶ ${trader ? id.slice(0,6)+'…'+id.slice(-4) : id}${tag ? '  ('+tag.slice(1)+')' : ''}`);
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pmrec-'));

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport:    { width: SIZE, height: SIZE },
    recordVideo: { dir: tmpDir, size: { width: SIZE, height: SIZE } },
  });
  const page = await ctx.newPage();

  // AI-written hook/verdict/analysis copy — injected before any page script so
  // the dashboard can read window.__aiCopy as it mounts.
  if (aiCopy) {
    await page.addInitScript(
      'window.__aiCopy = ' + JSON.stringify(aiCopy) + ';'
    );
  }

  let rawVideo, err = null;
  try {
    const url = 'file://' + dashboard.replace(/\\/g, '/') +
      '?' + (trader ? 'addr' : 'event') + '=' + encodeURIComponent(id) +
      (hide ? '&hide=' + encodeURIComponent(hide) : '');
    const tGoto = Date.now();
    await page.goto(url);

    // wait until the deck mounts (or the dashboard reports a load error)
    await page.waitForFunction(
      () => (window.__rec && window.__rec.ready) || window.__recError,
      null, { timeout: 60000 });
    const recErr = await page.evaluate(() => window.__recError || null);
    if (recErr) throw new Error(recErr);
    const tReady = Date.now();
    console.log('  · deck started, recording…');

    // wait until it reaches the closing card, then linger a moment
    await page.waitForFunction(
      () => window.__rec && window.__rec.finished,
      null, { timeout: 240000 });
    await page.waitForTimeout(HOLD_MS);

    rawVideo = page.video();
    // seconds of loading screen to trim off the front of the capture
    var leadSec = Math.max(0, (tReady - tGoto) / 1000 - 0.3);
  } catch (e) {
    err = e;
  }

  await ctx.close();          // finalizes the .webm
  await browser.close();

  if (err) {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    console.error(`  ✗ ${err.message}\n`);
    return;
  }

  const rawPath = await rawVideo.path();
  if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
  const out = path.join(OUT_DIR, `${trader ? id.slice(0,10) : id}${tag}_${stamp}.mp4`);

  console.log('  · encoding MP4…');
  const ff = spawnSync('ffmpeg', [
    '-y',
    '-ss', leadSec.toFixed(2),
    '-i', rawPath,
    '-vf', `scale=${SIZE}:${SIZE}:force_original_aspect_ratio=increase,` +
           `crop=${SIZE}:${SIZE},fps=${FPS}`,
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
    '-preset', 'medium', '-crf', '18',
    // strip capture fingerprints: drop inherited creation_time/metadata, and
    // suppress ffmpeg's "encoder=Lavf…" tag + x264's version string in the SEI
    '-map_metadata', '-1',
    '-fflags', '+bitexact', '-flags:v', '+bitexact',
    '-movflags', '+faststart',
    out,
  ], { stdio: ['ignore', 'ignore', 'inherit'] });

  fs.rmSync(tmpDir, { recursive: true, force: true });

  if (ff.status !== 0) { console.error('  ✗ ffmpeg failed\n'); return; }
  console.log(`  ✓ saved → ${out}`);

  // auto-score with music if ./music has tracks and --no-music wasn't passed
  if (!global.__noMusic && addmusic.listTracks().length) {
    addmusic.mux(out);
  }
  console.log('');
}

async function main() {
  checkFfmpeg();

  const args   = process.argv.slice(2);
  const grid          = args.includes('--grid');
  const trader        = args.includes('--trader');
  const traderClassic = args.includes('--trader-classic');
  global.__noMusic    = args.includes('--no-music');
  const hide   = (args.find(a => a.startsWith('--hide=')) || '').slice('--hide='.length);
  const copyPath = (args.find(a => a.startsWith('--copy=')) || '').slice('--copy='.length);
  let aiCopy = null;
  if (copyPath) {
    try { aiCopy = JSON.parse(fs.readFileSync(copyPath, 'utf8')); }
    catch (e) { console.error(`  ! could not read --copy file: ${e.message}`); }
  }
  const inputs = args.filter(a => !a.startsWith('-'));
  const file   = traderClassic ? 'Dashboard.html'
              : trader         ? 'DashboardGrid.html'
              : grid           ? 'EventDashboardGrid.html'
              :                  'EventDashboard.html';
  const dashboard = path.join(__dirname, 'dashboards', file);
  const tag    = traderClassic ? '_trader_classic'
              : trader         ? '_trader'
              : grid           ? '_grid'
              :                  '';
  const mode   = (trader || traderClassic) ? 'trader' : 'event';

  if (!fs.existsSync(dashboard)) {
    console.error(`✗ ${file} not found in ./dashboards`);
    process.exit(1);
  }

  if (inputs.length) {
    for (const a of inputs) await record(a, dashboard, tag, mode, hide, aiCopy);
    return;
  }

  const what = trader ? 'wallet' : 'event';
  console.log(`Polymarket ${what} → 1:1 video recorder${trader ? '  [trader grid]' : grid ? '  [event grid]' : ''}`);
  console.log(`Paste a ${trader ? 'wallet address' : 'event URL'} and press Enter. Empty line to quit.\n`);
  let line;
  while ((line = (await ask(`${what} ▸ `)).trim())) {
    await record(line, dashboard, tag, mode, hide, aiCopy);
  }
  console.log('done.');
}

main().catch(e => { console.error(e); process.exit(1); });
