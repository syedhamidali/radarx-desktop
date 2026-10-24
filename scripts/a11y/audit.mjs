// Accessibility audit of the web build with axe-core in headless Chrome (no npm dependency:
// Chrome DevTools protocol over Node's built-in WebSocket).
//
//   npm pack axe-core            # once, anywhere; unpack it, or point AXE_CORE at axe.min.js
//   RADARX_AUTH_DISABLED=1 npm run dev:web      # local dev server only (see docs/ACCESSIBILITY.md)
//   AXE_CORE=/path/to/axe.min.js node scripts/a11y/audit.mjs [--out DIR] [--url URL]
//
// For every screen in SCREENS and for both colour schemes it loads the page, optionally runs a
// few steps (click by visible text or CSS selector, press a key), runs axe-core and writes
//   DIR/<scheme>-<screen>.png      a screenshot
//   DIR/report.json                all violations (rule, impact, count, targets)
// and prints a summary table. Exit code 1 when a serious or critical violation is left.
//
// One Chrome at a time: it is started here and killed at the end.
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : dflt;
};
const URL_ = opt('--url', 'http://localhost:1490/');
const OUT = opt('--out', 'a11y-report');
const PORT = Number(opt('--port', '9333'));
const CHROME =
  process.env.CHROME ||
  (process.platform === 'darwin'
    ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    : process.platform === 'win32'
      ? 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
      : 'google-chrome');
const AXE_PATH = process.env.AXE_CORE;
if (!AXE_PATH) {
  console.error('Set AXE_CORE to the path of axe.min.js (npm pack axe-core).');
  process.exit(2);
}
const AXE = readFileSync(AXE_PATH, 'utf8');

// Steps: { click: 'text' | { css } } | { key: 'Escape' } | { wait: ms } | { eval: 'js' }
const SCREENS = JSON.parse(process.env.A11Y_SCREENS || 'null') || [
  { name: 'start', steps: [] },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdpConnect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = () => rej(new Error('CDP connection failed'));
  });
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    }
  };
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const n = ++id;
      pending.set(n, { resolve, reject });
      ws.send(JSON.stringify({ id: n, method, params }));
    });
  return { send, close: () => ws.close() };
}

async function evaluate(cdp, expression) {
  const r = await cdp.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'evaluate failed');
  return r.result.value;
}

const CLICK = (target) => `(() => {
  const t = ${JSON.stringify(target)};
  let el = null;
  if (typeof t === 'object') el = document.querySelector(t.css);
  else {
    const all = [...document.querySelectorAll('button, [role=button], a, [role=tab], [role=menuitem], summary, label')];
    el = all.find((e) => (e.getAttribute('aria-label') || e.textContent || '').trim().toLowerCase().includes(t.toLowerCase()));
  }
  if (!el) return false;
  el.click();
  return true;
})()`;

async function runScreen(cdp, scheme, screen) {
  await cdp.send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-color-scheme', value: scheme }],
  });
  // "auto" follows the emulated colour scheme above (the app's default is always dark)
  await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
    source: `try { if (!localStorage.getItem('xradar-settings')) localStorage.setItem('xradar-settings', JSON.stringify({ theme: { mode: 'auto' } })); } catch (e) {}`,
  });
  await cdp.send('Page.navigate', { url: URL_ });
  await sleep(3500);
  for (const step of screen.steps) {
    if (step.click !== undefined) {
      const ok = await evaluate(cdp, CLICK(step.click));
      if (!ok && step.click !== 'Skip') console.warn(`  [${scheme}/${screen.name}] nothing to click for`, JSON.stringify(step.click));
      await sleep(step.wait ?? 700);
    } else if (step.key) {
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: step.key, code: step.key });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: step.key, code: step.key });
      await sleep(step.wait ?? 500);
    } else if (step.eval) {
      await evaluate(cdp, step.eval);
      await sleep(step.wait ?? 500);
    } else if (step.wait) {
      await sleep(step.wait);
    }
  }
  await evaluate(cdp, AXE);
  const result = await evaluate(
    cdp,
    `axe.run(document, { resultTypes: ['violations'] }).then((r) => JSON.stringify(r.violations.map((v) => ({
      id: v.id, impact: v.impact, help: v.help, helpUrl: v.helpUrl,
      nodes: v.nodes.map((n) => ({ target: n.target.join(' '), summary: (n.failureSummary || '').split('\\n').slice(1, 3).join(' | '), html: n.html.slice(0, 160) })),
    }))))`,
  );
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(OUT, `${scheme}-${screen.name}.png`), Buffer.from(shot.data, 'base64'));
  return JSON.parse(result);
}

// Focus checks of modal dialogs: focus moves in, Tab never leaves, Escape closes and focus comes back
// to the control that opened it. DIALOGS is JSON: [{ "name": "settings", "trigger": "settings" }].
const DIALOGS = JSON.parse(process.env.A11Y_DIALOGS || '[]');

async function key(cdp, k, code, vk, shift = false) {
  const base = { key: k, code, windowsVirtualKeyCode: vk, modifiers: shift ? 8 : 0 };
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...base });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
}

async function checkDialog(cdp, d) {
  await cdp.send('Page.navigate', { url: URL_ });
  await sleep(3000);
  await evaluate(cdp, CLICK('Skip'));
  await sleep(500);
  const marked = await evaluate(
    cdp,
    `(() => { const t = ${JSON.stringify(d.trigger)};
      const el = [...document.querySelectorAll('button')].find((e) => (e.getAttribute('aria-label') || e.title || e.textContent || '').toLowerCase().includes(t.toLowerCase()));
      if (!el) return false; el.setAttribute('data-a11y-trigger', '1'); el.focus(); el.click(); return true; })()`,
  );
  if (!marked) return { dialog: d.name, error: 'trigger not found' };
  await sleep(900);
  const inside = `(() => { const dlg = document.querySelector('[role=dialog][aria-modal=true], [role=dialog]'); return !!dlg && dlg.contains(document.activeElement); })()`;
  const out = { dialog: d.name, opens: await evaluate(cdp, `!!document.querySelector('[role=dialog]')`) };
  out.focusMovesIn = await evaluate(cdp, inside);
  out.tabStaysInside = true;
  for (let i = 0; i < 40 && out.tabStaysInside; i++) {
    await key(cdp, 'Tab', 'Tab', 9, i % 7 === 6);
    out.tabStaysInside = await evaluate(cdp, inside);
  }
  await key(cdp, 'Escape', 'Escape', 27);
  await sleep(700);
  out.closesOnEscape = await evaluate(cdp, `!document.querySelector('[role=dialog]')`);
  out.focusReturns = await evaluate(cdp, `document.activeElement === document.querySelector('[data-a11y-trigger]')`);
  return out;
}

mkdirSync(OUT, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), 'a11y-chrome-'));
const chrome = spawn(
  CHROME,
  [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--window-size=1500,950', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-gpu-sandbox', 'about:blank',
  ],
  { stdio: 'ignore' },
);
const stop = () => {
  try { chrome.kill('SIGKILL'); } catch { /* already gone */ }
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* best effort */ }
};
process.on('exit', stop);

try {
  let targets = null;
  for (let i = 0; i < 50 && !targets; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json`);
      targets = await r.json();
    } catch { await sleep(200); }
  }
  if (!targets) throw new Error('Chrome did not start');
  const page = targets.find((t) => t.type === 'page');
  const cdp = await cdpConnect(page.webSocketDebuggerUrl);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  const report = {};
  for (const scheme of ['light', 'dark']) {
    for (const screen of SCREENS) {
      const key = `${scheme}/${screen.name}`;
      try {
        report[key] = await runScreen(cdp, scheme, screen);
      } catch (err) {
        report[key] = { error: String(err) };
        console.error(`  ${key}: ${err}`);
      }
    }
  }
  const focus = [];
  for (const d of DIALOGS) {
    try { focus.push(await checkDialog(cdp, d)); } catch (err) { focus.push({ dialog: d.name, error: String(err) }); }
  }
  report.focus = focus;
  writeFileSync(join(OUT, 'report.json'), JSON.stringify(report, null, 2));
  if (focus.length) {
    console.log('\ndialog focus checks');
    for (const f of focus) console.log(' ', JSON.stringify(f));
    if (focus.some((f) => f.error || !f.focusMovesIn || !f.tabStaysInside || !f.closesOnEscape || !f.focusReturns)) serious++;
  }
  let serious = 0;
  console.log('\nscreen                         rule                      impact     nodes');
  for (const [key, list] of Object.entries(report)) {
    if (key === 'focus') continue;
    if (!Array.isArray(list)) { console.log(`${key.padEnd(30)} ERROR ${list.error}`); continue; }
    if (list.length === 0) console.log(`${key.padEnd(30)} (no violations)`);
    for (const v of list) {
      if (v.impact === 'serious' || v.impact === 'critical') serious++;
      console.log(`${key.padEnd(30)} ${v.id.padEnd(25)} ${String(v.impact).padEnd(10)} ${v.nodes.length}`);
    }
  }
  cdp.close();
  stop();
  process.exitCode = serious ? 1 : 0;
} catch (err) {
  console.error(String(err));
  stop();
  process.exitCode = 2;
}
