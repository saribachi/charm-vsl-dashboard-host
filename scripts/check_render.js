#!/usr/bin/env node
// Execute the built dashboard's page script against a DOM stub and assert that every
// container actually filled.
//
// Why this exists: the page is one long script, so a single ReferenceError kills every
// render after it and the page ships looking structurally fine — the HTML contains all
// the right divs and the DATA blob is intact, they are just never populated. Grepping
// the output for markers cannot see that. This has now shipped twice, both times a
// temporal-dead-zone error from touching a `const` declared further down the file.
//
//   node scripts/check_render.js [path/to/index.html]
//
// Exits non-zero if the script throws or a required container renders empty.

const fs = require('fs');
const path = require('path');

const file = process.argv[2] || path.join(__dirname, '..', 'dashboard', 'index.html');
// The two pages have different containers. Checking index.html's list against the /vsl
// detail page reported eleven empty containers and "do not deploy" for a page that was
// rendering perfectly — a guard that cries wolf gets ignored, which defeats the point.
const CONTAINERS = {
  // 'kpis' and 'funnel' are gone on purpose: they were a lifetime copy of the funnel
  // sitting under a windowed heading. The windowed spine (win-<offer>) replaced them.
  // 'freshness' is gone: ad-export coverage moved to the Reference tab.
  unified: ['ftabs', 'csflex', 'details', 'engpanel',
            'outcomes',   // the show-up rate panel that replaced the manual queue
            'report', 'qbyad', 'dspend',   // windowed attribution + spend
            // the timeframe control and one windowed pane per registered offer
            'tfsel', 'tfrange', 'panes-extra', 'win-gtm', 'win-cs'],
  detail:  ['tiles', 'rates', 'coverage', 'funnel', 'subs-table', 'appt-table'],
};
const isDetail = /vsl_dashboard\.html$/.test(file);
const REQUIRED = isDetail ? CONTAINERS.detail : CONTAINERS.unified;

const html = fs.readFileSync(file, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/) || html.match(/<script>([\s\S]*)<\/script>/);
if (!m) {
  console.error('check_render: no page script found in ' + file);
  process.exit(1);
}

const els = {};
const mk = (id) =>
  els[id] ||
  (els[id] = {
    id, innerHTML: '', textContent: '', style: {}, dataset: {},
    addEventListener() {}, removeEventListener() {}, appendChild() {}, remove() {},
    setAttribute() {}, getAttribute: () => null,
    querySelector: () => mk(id + '-q'), querySelectorAll: () => [], closest: () => null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  });

global.document = {
  getElementById: mk, querySelector: () => mk('doc-q'), querySelectorAll: () => [],
  addEventListener() {}, createElement: () => mk('tmp'), body: mk('body'), documentElement: mk('html'),
};
global.window = {
  addEventListener() {}, removeEventListener() {}, location: { search: '', reload() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }), setTimeout, clearTimeout,
};
global.navigator = { userAgent: 'node' };
global.localStorage = { _v: {}, getItem(k){ return this._v[k] ?? null; }, setItem(k, v){ this._v[k] = String(v); } };
global.window.localStorage = global.localStorage;
// The page fetches RB2B and posts verdicts. Neither should run here, and neither is
// allowed to fail the check — only synchronous render errors matter.
// The page polls /version on a timer to spot a stale cached copy. A real setInterval
// keeps the node process alive forever, so the check hung instead of failing — stub it,
// and setTimeout with it, since neither should run here.
global.setInterval = () => 0;
global.clearInterval = () => {};
global.window.setInterval = global.setInterval;
global.window.clearInterval = global.clearInterval;
global.fetch = () => Promise.reject(new Error('network disabled in check_render'));
global.window.fetch = global.fetch;

// The page script's top-level consts are scoped to this eval, so anything the checks
// below need has to be handed out from inside it.
try {
  eval(m[1] + '\n;try{global.__probe = {TF, windowedFunnel, DATA};}catch(e){}');
} catch (e) {
  console.error('check_render: page script threw — the dashboard would render blank');
  console.error('  ' + e.message);
  process.exit(1);
}

let failed = false;
for (const id of REQUIRED) {
  const len = (els[id] && els[id].innerHTML || '').length;
  const ok = len > 0;
  if (!ok) failed = true;
  console.log(`  ${ok ? 'OK ' : '!! '} ${id.padEnd(10)} ${String(len).padStart(6)} chars`);
}

// ---- timeframe correctness ----
// Re-aggregating the dated rows over an ALL TIME window must reproduce the totals the
// builder computed server-side. If it does not, a row is stamped with a date field the
// window filter does not read, and every windowed number on the page is quietly short.
// This is the failure mode a container check cannot see: the pane renders fine, it is
// just wrong.
let mismatched = false;
try {
  if (isDetail) throw { skip: true };
  const probe = global.__probe;
  if (!probe) throw new Error('page script did not expose TF/windowedFunnel');
  const all = probe.TF.resolve('all');
  const wf = probe.windowedFunnel;
  const DATA_ = probe.DATA;
  for (const k of (DATA_.offer_order || [])) {
    const o = (DATA_.offers || {})[k];
    if (!o || o.error) continue;
    const W = wf(o, all);
    const checks = [['form fills', W.fills, o.form_fills], ['booked', W.booked, o.booked]];
    for (const [name, got, want] of checks) {
      if (got !== want) {
        mismatched = true;
        console.error(`  !!  ${k} ${name}: all-time window says ${got}, builder says ${want}`);
      }
    }
  }
  if (!mismatched) console.log('  OK  all-time window reproduces every offer total');
} catch (e) {
  if (e && e.skip) {
    console.log('  --  timeframe check skipped (detail page has no offer panes)');
  } else {
    mismatched = true;
    console.error('  !!  timeframe check threw: ' + e.message);
  }
}

if (failed || mismatched) {
  console.error('\ncheck_render: do not deploy');
  process.exit(1);
}
console.log('\ncheck_render: all containers populated');
