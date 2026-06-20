/* ─────────────────────────────────────────────────────────────────────────
   duck_capture.js — opens https://duck.ai/ in CloakBrowser, sends a prompt,
   and captures the full POST to /duckchat/v1/chat (URL, method, all request
   headers — including the dynamic x-vqd-hash-1 / x-fe-signals — and the JSON
   body). Optionally captures the streamed (text/event-stream) response too.

   Usage:
     node duck_capture.js                       → uses default prompt
     node duck_capture.js "your prompt here"     → custom prompt
     node duck_capture.js --headful "prompt"     → show the browser window
     node duck_capture.js --out=capture.json     → where to save (default below)

   The whole point of driving a real (stealth) browser is that duck.ai's
   obfuscated client JS computes x-vqd-hash-1 from a fingerprint challenge.
   We let the page generate it, then intercept the outgoing request to read it.
   ───────────────────────────────────────────────────────────────────────── */
'use strict';

const path = require('path');
const fs   = require('fs');

/* CloakBrowser is ESM-only, so we load it with a dynamic import() inside the
   async wrapper below. Fall back to plain playwright if it's not installed. */
async function getLauncher() {
  try {
    const mod = await import('cloakbrowser');
    return { launch: mod.launch, usingCloak: true };
  } catch (e) {
    console.warn('[duck] cloakbrowser unavailable, falling back to playwright:', e.message);
    const { chromium } = require('playwright');
    return { launch: (opts) => chromium.launch(opts), usingCloak: false };
  }
}

/* ── args ─────────────────────────────────────────────────────────────── */
const argv    = process.argv.slice(2);
const headful = argv.includes('--headful');
const outArg  = argv.find(a => a.startsWith('--out='));
const OUT_FILE = outArg ? outArg.slice('--out='.length)
                        : path.join(__dirname, 'duck_capture.json');
const modelArg = argv.find(a => a.startsWith('--model='));
// model is selected in the UI by its visible label, e.g. "Claude Haiku 4.5".
// empty = keep whatever model duck.ai defaults to (GPT-5 mini).
const MODEL_LABEL = modelArg ? modelArg.slice('--model='.length).trim()
                             : (process.env.DUCK_MODEL_LABEL || '').trim();
const prompt = argv.filter(a => !a.startsWith('--')).join(' ').trim()
            || 'Say hello in one short sentence.';

/* identity (UA + high-entropy Sec-CH-UA-* client hints) — passed in by Python
   via CHROME_IDENTITY env var as JSON so the browser session matches the
   curl-cffi calls that follow. Default Win11 Chrome 145 for manual runs. */
const DEFAULT_IDENTITY = {
  label: 'win11-24h2-145.7400.85-default',
  ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
  sec_ch_ua: '"Google Chrome";v="145", "Chromium";v="145", "Not?A_Brand";v="24"',
  sec_ch_ua_full_version_list: '"Google Chrome";v="145.0.7400.85", "Chromium";v="145.0.7400.85", "Not?A_Brand";v="24.0.0.0"',
  sec_ch_ua_mobile: '?0',
  sec_ch_ua_platform: '"Windows"',
  sec_ch_ua_platform_version: '"15.0.0"',
  sec_ch_ua_arch: '"x86"',
  sec_ch_ua_bitness: '"64"',
  sec_ch_ua_wow64: '?0',
  sec_ch_ua_model: '""',
};
let IDENTITY = DEFAULT_IDENTITY;
try {
  if (process.env.CHROME_IDENTITY) IDENTITY = JSON.parse(process.env.CHROME_IDENTITY);
} catch (e) {
  console.warn('[duck] CHROME_IDENTITY parse failed, using default:', e.message);
}
function identityExtraHeaders(id) {
  return {
    'Sec-CH-UA':                   id.sec_ch_ua,
    'Sec-CH-UA-Full-Version-List': id.sec_ch_ua_full_version_list,
    'Sec-CH-UA-Mobile':            id.sec_ch_ua_mobile,
    'Sec-CH-UA-Platform':          id.sec_ch_ua_platform,
    'Sec-CH-UA-Platform-Version':  id.sec_ch_ua_platform_version,
    'Sec-CH-UA-Arch':              id.sec_ch_ua_arch,
    'Sec-CH-UA-Bitness':           id.sec_ch_ua_bitness,
    'Sec-CH-UA-Wow64':             id.sec_ch_ua_wow64,
    'Sec-CH-UA-Model':             id.sec_ch_ua_model,
  };
}

const CHAT_PATH = '/duckchat/v1/chat';

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const { launch, usingCloak } = await getLauncher();
  console.log(`[duck] launching ${usingCloak ? 'CloakBrowser' : 'Playwright/chromium'} (headless=${!headful})`);

  const browser = await launch({
    headless: !headful,
    // cloakbrowser-specific flags are ignored by plain playwright:
    humanize: true,
    geoip: true,
  });

  /* apply the chosen identity to the context: UA via newContext option (so
     navigator.userAgent JS-side matches), Sec-CH-UA-* via extraHTTPHeaders
     (so every outbound request — including the chat POST — carries them). */
  const context = await browser.newContext({
    userAgent: IDENTITY.ua,
    extraHTTPHeaders: identityExtraHeaders(IDENTITY),
  });
  console.log(`[duck] identity: ${IDENTITY.label || 'custom'} (UA Chrome/145)`);
  const page = await context.newPage();

  /* capture container, resolved when we see the chat POST */
  let captured = null;
  let resolveReq;
  const reqSeen = new Promise(r => { resolveReq = r; });

  /* 1) intercept the request — gives us method, url, headers, body */
  page.on('request', async (req) => {
    if (!req.url().includes(CHAT_PATH) || req.method() !== 'POST') return;
    try {
      const headers = await req.allHeaders();
      captured = {
        capturedAt: new Date().toISOString(),
        url: req.url(),
        method: req.method(),
        prompt,
        requestHeaders: headers,
        // postData is the raw JSON string the page sent
        body: req.postData(),
      };
      // try to parse body for convenience
      try { captured.bodyParsed = JSON.parse(captured.body); } catch (_) {}
      console.log(`[duck] >>> captured request to ${CHAT_PATH}`);
      resolveReq();
    } catch (e) {
      console.warn('[duck] request capture failed:', e.message);
    }
  });

  /* 2) also grab the streamed response text once it finishes (best-effort) */
  page.on('response', async (res) => {
    if (!res.url().includes(CHAT_PATH)) return;
    try {
      const respHeaders = await res.allHeaders();
      const text = await res.text(); // resolves when the stream completes
      if (captured) {
        captured.status = res.status();
        captured.responseHeaders = respHeaders;
        captured.responseStream = text;
      }
      console.log(`[duck] <<< response ${res.status()} (${text.length} bytes)`);
    } catch (e) {
      console.warn('[duck] response capture failed:', e.message);
    }
  });

  /* 3) navigate */
  console.log('[duck] navigating to https://duck.ai/');
  await page.goto('https://duck.ai/', { waitUntil: 'domcontentloaded' });
  await sleep(1500);

  /* 4) click through any onboarding / consent buttons (best-effort) */
  const consentLabels = [
    // Russian locale (duck.ai serves localized UI)
    'Принять и продолжить', 'Принять', 'Продолжить', 'Согласиться',
    // English fallbacks
    'Accept and continue', 'Get Started', 'Get started', 'I Agree',
    'I agree', 'Agree', 'Accept', 'Got it', 'Next', 'Continue',
  ];
  for (const label of consentLabels) {
    try {
      const btn = page.getByRole('button', { name: label, exact: false });
      if (await btn.first().isVisible({ timeout: 500 }).catch(() => false)) {
        await btn.first().click({ timeout: 1000 }).catch(() => {});
        console.log(`[duck] clicked consent button: "${label}"`);
        await sleep(800);
      }
    } catch (_) {}
  }

  /* 4b) pick the chat model in the UI (must be done at chat creation — the
     model can't be swapped via the request body). The picked model's real API
     id then lands in the captured request body. */
  if (MODEL_LABEL) {
    try {
      const picker = page.locator('[data-testid="model-select-button"]').first();
      if (await picker.isVisible({ timeout: 3000 }).catch(() => false)) {
        await picker.click().catch(() => {});
        await sleep(800);
        const opt = page.getByText(MODEL_LABEL, { exact: false }).first();
        if (await opt.isVisible({ timeout: 2000 }).catch(() => false)) {
          await opt.click().catch(() => {});
          await sleep(400);
          // confirm with "Начать чат" / "Start chat"
          for (const b of ['Начать чат', 'Start chat', 'Начать', 'Start']) {
            const btn = page.getByRole('button', { name: b, exact: false }).first();
            if (await btn.isVisible({ timeout: 600 }).catch(() => false)) {
              await btn.click().catch(() => {});
              break;
            }
          }
          await sleep(800);
          console.log(`[duck] selected model: "${MODEL_LABEL}"`);
        } else {
          console.warn(`[duck] model "${MODEL_LABEL}" not in picker; keeping default`);
          await page.keyboard.press('Escape').catch(() => {});
        }
      } else {
        console.warn('[duck] model-select-button not found; keeping default model');
      }
    } catch (e) {
      console.warn('[duck] model selection failed:', e.message);
    }
  }

  /* 5) find the prompt input and type */
  console.log(`[duck] typing prompt: ${JSON.stringify(prompt)}`);
  const inputCandidates = [
    'textarea',
    '[contenteditable="true"]',
    'input[type="text"]',
    '[role="textbox"]',
  ];
  let input = null;
  for (const sel of inputCandidates) {
    const loc = page.locator(sel).first();
    if (await loc.isVisible({ timeout: 1500 }).catch(() => false)) {
      input = loc;
      console.log(`[duck] using input selector: ${sel}`);
      break;
    }
  }
  if (!input) {
    console.error('[duck] could not find a prompt input. Re-run with --headful to inspect the page.');
    await browser.close();
    process.exit(1);
  }

  await input.click();
  await input.fill('').catch(() => {});
  await input.type(prompt, { delay: 30 }).catch(async () => {
    // contenteditable may not support type(); fall back to keyboard
    await page.keyboard.type(prompt, { delay: 30 });
  });
  await sleep(400);

  /* 6) submit. duck.ai's composer no longer sends on Enter — it needs the
     "Ask"/send button (a type=submit next to the input). Try the button across
     locales, then fall back to Enter for older UIs. */
  const sendSelectors = [
    'button[type="submit"]',
    'button[aria-label="Ask"]',
    'button[aria-label="Отправить"]',
    'button[aria-label*="Send" i]',
    'button[aria-label*="Отправ" i]',
  ];
  let sent = false;
  for (const sel of sendSelectors) {
    const btn = page.locator(sel).first();
    if (await btn.isVisible({ timeout: 800 }).catch(() => false)
        && !(await btn.isDisabled().catch(() => false))) {
      await btn.click({ timeout: 1500 }).catch(() => {});
      console.log(`[duck] clicked send button: ${sel}`);
      sent = true;
      break;
    }
  }
  if (!sent) {
    console.log('[duck] no send button found — falling back to Enter');
    await page.keyboard.press('Enter');
  }

  /* 7) wait for the request (with a timeout) */
  console.log('[duck] waiting for chat request…');
  const timeout = sleep(30000).then(() => 'timeout');
  const winner = await Promise.race([reqSeen.then(() => 'ok'), timeout]);

  if (winner === 'timeout' || !captured) {
    console.error('[duck] timed out waiting for the chat POST. Re-run with --headful to debug.');
    await browser.close();
    process.exit(2);
  }

  /* give the response stream a moment to finish so we can capture it too */
  await sleep(4000);

  /* remember which model label was selected, so a later refresh re-picks it */
  captured.modelLabel = MODEL_LABEL;
  /* and remember the identity used — Python's curl-cffi path reads this so the
     follow-up requests send the exact same UA + Sec-CH-UA-* the browser did. */
  captured.identity = IDENTITY;

  /* also export cookies (duck.ai chat is hash-auth'd, but keep them anyway) */
  try { captured.cookies = await context.cookies(); } catch (_) { captured.cookies = []; }

  fs.writeFileSync(OUT_FILE, JSON.stringify(captured, null, 2), 'utf8');
  console.log(`[duck] saved capture → ${OUT_FILE}`);

  /* quick summary to stdout */
  console.log('\n=== KEY HEADERS ===');
  for (const k of ['x-vqd-hash-1', 'x-fe-signals', 'x-fe-version', 'x-ddg-journey-id', 'user-agent', 'cookie']) {
    const v = captured.requestHeaders[k];
    if (v) console.log(`${k}: ${v.length > 80 ? v.slice(0, 80) + '…' : v}`);
  }

  await browser.close();
  console.log('[duck] done.');
})().catch(err => {
  console.error('[duck] fatal:', err);
  process.exit(1);
});
