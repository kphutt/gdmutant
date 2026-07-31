// A DOM stand-in just rich enough to run the report page's real inlined script in Node, so the
// page's *behaviour* — stepping, filtering, keyboard access — is tested rather than assumed.
//
// The script is a plain inline <script>, not a module, so it exports nothing; it is observed the
// way a reader observes it, through what it writes to the page. Every element is a recording stub
// looked up by selector and cached, so a value written through `$('#pos')` is still there when the
// assertion reads it back.
//
// Usage: node harness.js <page.html>   ->   prints one JSON line of observations.

const fs = require('fs');
const vm = require('vm');

const page = fs.readFileSync(process.argv[2], 'utf8');
// The renderer is the last <script>; the first is the report's application/json data block.
const scripts = [...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = scripts[scripts.length - 1];

function stub(dataset = {}) {
  const el = {
    dataset,
    style: {},
    disabled: false,
    _html: '',
    _text: '',
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    remove() {},
    after() {},
    append() {},
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; },
    get textContent() { return this._text; },
    set textContent(v) { this._text = v; },
  };
  return el;
}

const cache = new Map();
function byId(sel) {
  if (!cache.has(sel)) cache.set(sel, stub());
  return cache.get(sel);
}

// The source pane hands back a fresh row for whatever line the selection asks about, and no marks
// (marking is cosmetic; nothing under test reads it back).
const src = byId('#src');
src.querySelector = sel => (sel.startsWith('.row[data-line=') ? stub() : null);
src.querySelectorAll = () => [];

const OPS = JSON.parse(process.env.HARNESS_OPS || '[]');
const chips = {
  '[data-filter]': ['survived', 'caught', 'all'].map(f => stub({ filter: f })),
  '[data-op]': ['all', ...OPS].map(o => stub({ op: o })),
};

let keydown = null;
const document = {
  documentElement: stub(),
  querySelector: byId,
  querySelectorAll: sel => chips[sel] || [],
  createElement: () => stub(),
  addEventListener: (kind, fn) => { if (kind === 'keydown') keydown = fn; },
};
const window = {
  matchMedia: () => ({ matches: false, addEventListener() {} }),
};

const context = vm.createContext({ document, window, console, JSON });
vm.runInContext(code, context);

// ---- the observations the Python test asserts on -------------------------------------------

const pos = () => byId('#pos').textContent;
const press = key => keydown({ key, target: { tagName: 'BODY' } });
const click = (group, value) => {
  const el = chips[group].find(c => (c.dataset.filter || c.dataset.op) === value);
  el.onclick();
};

const out = { load: pos(), forward: [], backward: [], filters: {} };

// Walk the whole list forward: one extra press proves it clamps instead of wrapping.
for (let i = 0; i < 40; i++) { out.forward.push(pos()); press('ArrowRight'); }
out.forwardEnd = { pos: pos(), nextDisabled: byId('#next').disabled };
for (let i = 0; i < 40; i++) { out.backward.push(pos()); press('ArrowLeft'); }
out.backwardEnd = { pos: pos(), prevDisabled: byId('#prev').disabled };

for (const f of ['all', 'caught', 'survived']) {
  click('[data-filter]', f);
  out.filters[f] = pos();
  // Under every filter, the selection the stepper reports must be one it can reach.
  press('ArrowRight');
  out.filters[f + ':after-step'] = pos();
}

// A selection made under "survived" must survive the switch to "all", which is a superset.
click('[data-filter]', 'survived');
press('ArrowRight');
press('ArrowRight');
const held = pos();
click('[data-filter]', 'all');
out.kept = { under_survived: held, under_all: pos() };

console.log(JSON.stringify(out));
