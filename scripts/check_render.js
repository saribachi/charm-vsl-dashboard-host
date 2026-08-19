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
const REQUIRED = ['kpis', 'funnel', 'fcompare', 'csflex', 'details'];

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
// The page fetches RB2B and posts verdicts. Neither should run here, and neither is
// allowed to fail the check — only synchronous render errors matter.
global.fetch = () => Promise.reject(new Error('network disabled in check_render'));
global.window.fetch = global.fetch;

try {
  eval(m[1]);
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

if (failed) {
  console.error('\ncheck_render: a container rendered empty — do not deploy');
  process.exit(1);
}
console.log('\ncheck_render: all containers populated');
