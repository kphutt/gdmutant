// A DOM stand-in just rich enough to run the report page's real inlined script in Node, so the
// page's *behaviour* — stepping, filtering, keyboard access, deep links, done marks — is tested
// rather than assumed.
//
// The script is a plain inline <script>, not a module, so it exports nothing; it is observed the
// way a reader observes it, through what it writes to the page. Every element is a recording stub
// looked up by selector and cached, so a value written through `$('#pos')` is still there when the
// assertion reads it back.
//
// `open_()` builds ONE tab. The `store` handed to it outlives the tab, which is the whole point:
// "close it, open it again, the marks are still on the right findings" is a claim this harness can
// actually check rather than reason about. Handing two tabs the same store models regenerating the
// report over the same `--html` path; handing them different stores models a copy that travelled.
//
// Usage: node harness.js <page> <rerun> <multi-file> <unscored>  ->  one JSON line of observations.

const fs = require('fs');
const vm = require('vm');

// The renderer is the last <script>; the first is the report's application/json data block.
function scriptOf(file) {
  const page = fs.readFileSync(file, 'utf8');
  const scripts = [...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  return scripts[scripts.length - 1];
}

const OPS = JSON.parse(process.env.HARNESS_OPS || '[]');

// Where the report file pretends to live. Done marks are scoped to this, so every tab that claims
// this location shares a bucket and one that claims another does not.
const HERE = 'file:///reports/report.html';

function openTab(file, store, hash, opts) {
  const options = opts || {};
  const cache = new Map();

  function stub(dataset = {}) {
    const classes = new Set();
    const el = {
      dataset,
      style: {},
      disabled: false,
      _html: '',
      _text: '',
      classList: {
        add: c => classes.add(c),
        remove: c => classes.delete(c),
        contains: c => classes.has(c),
        toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
      },
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

  // Every stub answers `closest` for the selector it was looked up (or fabricated) under, because
  // the page now finds its controls by delegation — `e.target.closest('#next')` — and a stub that
  // cannot be `closest`-ed is a control the harness can never really click.
  function target(sel, dataset) {
    const e = stub(dataset || {});
    e.closest = s => (s === sel ? e : null);
    return e;
  }

  function byId(sel) {
    if (!cache.has(sel)) cache.set(sel, target(sel));
    return cache.get(sel);
  }

  // The source pane hands back a fresh row for whatever line the selection asks about, and no
  // marks (mark painting is cosmetic; nothing under test reads it back).
  const src = byId('#src');
  src.querySelector = sel => (sel.startsWith('.row[data-line=') ? stub() : null);
  src.querySelectorAll = () => [];

  const chips = {
    '[data-filter]': ['survived', 'caught', 'all'].map(f => target('[data-filter]', { filter: f })),
    '[data-op]': ['all', ...OPS].map(o => target('[data-op]', { op: o })),
  };

  let keydown = null;
  const document = {
    // The real page ships `<html data-theme="light">`, and the toggle reads that value back
    // before writing the other one. A blank dataset made the toggle look like it worked from
    // `undefined`, which is not the state any reader starts in.
    documentElement: stub({ theme: 'light' }),
    querySelector: byId,
    querySelectorAll: sel => chips[sel] || [],
    createElement: () => stub(),
    addEventListener: (kind, fn) => { if (kind === 'keydown') keydown = fn; },
  };

  const at = options.at || HERE;
  const location = {
    href: at + (hash || ''),
    pathname: at.slice(at.indexOf('///') + 2),
    search: '',
    hash: hash || '',
  };
  let onhash = null;
  const window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener: (kind, fn) => { if (kind === 'hashchange') onhash = fn; },
  };
  const history = {
    replaceState: (a, b, url) => {
      const at = String(url).indexOf('#');
      location.hash = at < 0 ? '' : String(url).slice(at);
    },
  };
  // `broken` is the private-window / quota-exceeded / storage-disabled case, which must cost the
  // marks and nothing else.
  const localStorage = options.broken
    ? { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } }
    : {
        getItem: k => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
      };

  const context = vm.createContext({
    document, window, location, history, localStorage, console, JSON,
  });
  vm.runInContext(scriptOf(file), context);

  const card = () => byId('#aside').innerHTML;

  // A REAL click: handed to the page's one delegated handler on `#body`, exactly as a browser
  // would. Nothing here calls `step()` or an element's own `onclick`, because that is precisely
  // the shortcut that let the ← / → arrows ship unwired while the tests stayed green.
  const hit = el => byId('#body').onclick({ target: el });

  return {
    pos: () => byId('#pos').textContent,
    done: () => byId('#done').textContent,
    hash: () => location.hash,
    card,
    theme: () => document.documentElement.dataset.theme,
    // '' · 'done' · 'recheck', read off the control the reader actually sees.
    state: () => (/class="donebtn done"/.test(card()) ? 'done'
      : /class="donebtn recheck"/.test(card()) ? 'recheck' : ''),
    press: key => keydown({ key, target: { tagName: 'BODY' } }),
    // Click a control by the selector the page looks for it under.
    clickSel: sel => hit(target(sel)),
    clickChip: (group, value) => {
      hit(chips[group].find(c => (c.dataset.filter || c.dataset.op) === value));
    },
    // The index's rows and the source pane's marks are drawn as innerHTML, so the harness
    // fabricates the element the click lands on, carrying the data attributes the page reads.
    clickRow: i => hit(target('.frow', { file: String(i) })),
    clickMark: ids => hit(target('.mark', { ids })),
    clickRef: fid => hit(target('.refbtn', { ref: fid })),
    clickTheme: () => byId('#theme').onclick(),
    next: () => byId('#next'),
    prev: () => byId('#prev'),
    // The legend is rebuilt from the marks the pane just drew, so it is an observation of the
    // pane, not of the template — which is the whole point of testing it here.
    legend: () => byId('#legend').innerHTML,
    body: () => byId('#body').innerHTML,
    source: () => byId('#src').innerHTML,
    // Someone pasting a link into the address bar of an already-open report.
    paste: h => { location.hash = h; if (onhash) onhash(); },
  };
}

// ---- the observations the Python test asserts on -------------------------------------------

const PAGE = process.argv[2];      // the report
const RERUN = process.argv[3];     // the same file re-run: one survivor is now caught
const MULTI = process.argv[4];     // a two-file report, for the index rows and the back button
const UNSCORED = process.argv[5];  // a report holding an ignored, an invalid and an errored mutant
const KEYS = JSON.parse(process.env.HARNESS_KEYS || '[]');

const out = { load: null, forward: [], backward: [], filters: {} };

// ---- stepping, filtering, the keyboard ------------------------------------------------------

const s = openTab(PAGE, {}, '');
out.load = s.pos();

// Walk the whole list forward: one extra press proves it clamps instead of wrapping.
for (let i = 0; i < 40; i++) { out.forward.push(s.pos()); s.press('ArrowRight'); }
out.forwardEnd = { pos: s.pos(), nextDisabled: s.next().disabled };
for (let i = 0; i < 40; i++) { out.backward.push(s.pos()); s.press('ArrowLeft'); }
out.backwardEnd = { pos: s.pos(), prevDisabled: s.prev().disabled };

for (const f of ['all', 'caught', 'survived']) {
  s.clickChip('[data-filter]', f);
  out.filters[f] = s.pos();
  // Under every filter, the selection the stepper reports must be one it can reach.
  s.press('ArrowRight');
  out.filters[f + ':after-step'] = s.pos();
}

// A selection made under "survived" must survive the switch to "all", which is a superset.
s.clickChip('[data-filter]', 'survived');
s.press('ArrowRight');
s.press('ArrowRight');
const held = s.pos();
s.clickChip('[data-filter]', 'all');
out.kept = { under_survived: held, under_all: s.pos() };

// ---- deep links -----------------------------------------------------------------------------

const d = openTab(PAGE, {}, '');
out.deep = { onLoad: d.hash() };
d.press('ArrowRight');
d.press('ArrowRight');
out.deep.link = d.hash();
out.deep.posAtLink = d.pos();

// Paste that link into a fresh tab — the reload, and the "look at this survivor" case.
const restored = openTab(PAGE, {}, out.deep.link);
out.deep.restoredPos = restored.pos();
out.deep.restoredHash = restored.hash();

// A link to a CAUGHT finding: it resolves, and the page widens the filter so it is actually on
// screen rather than resolving correctly into an empty pane.
const caught = openTab(PAGE, {}, '#' + KEYS[5]);
out.deep.caughtPos = caught.pos();
out.deep.caughtHash = caught.hash();

// The source moved: the finding id no longer matches. The FILE it named is still the best answer.
const moved = openTab(PAGE, {}, '#a.gd:999:1:2:comparison');
out.deep.movedPos = moved.pos();
out.deep.movedHash = moved.hash();

// A file this run did not cover: all the way back to the default view.
const gone = openTab(PAGE, {}, '#nowhere/else.gd:1:1:2:comparison');
out.deep.gonePos = gone.pos();

// Garbage, including a percent-escape that throws inside decodeURIComponent.
const junk = openTab(PAGE, {}, '#%%%not-a-key');
out.deep.junkPos = junk.pos();

// Pasting a link into an ALREADY-OPEN report: same document, so nothing reloads on its own.
const live = openTab(PAGE, {}, '');
const firstLink = live.hash();
live.press('ArrowRight');
live.press('ArrowRight');
out.deep.beforePaste = live.pos();
live.paste(firstLink);
out.deep.afterPaste = live.pos();

// ---- done marks -----------------------------------------------------------------------------

const store = {};
const m = openTab(PAGE, store, '');
out.marks = { start: m.done() };
m.press('d');                                   // finding 1
out.marks.afterOne = m.done();
m.press('ArrowRight');
m.press('d');                                   // finding 2
out.marks.afterTwo = m.done();
m.press('d');                                   // …and off again
out.marks.afterUnmark = m.done();
m.press('ArrowRight');
m.press('d');                                   // finding 3
out.marks.beforeReload = m.done();

// Reload: same report, same location, so the marks come back — and on the same findings.
const m2 = openTab(PAGE, store, '');
out.marks.afterReload = m2.done();
out.marks.states = [];
for (let i = 0; i < 4; i++) { out.marks.states.push(m2.state()); m2.press('ArrowRight'); }

// A copy that travelled (mailed, downloaded, archived) opens unmarked — SAME browser storage,
// different report location, so it cannot inherit progress that was never about it.
out.marks.elsewhere = openTab(PAGE, store, '', { at: 'file:///Downloads/report.html' }).done();

// Storage that refuses must cost the marks and nothing else.
const broken = openTab(PAGE, {}, '', { broken: true });
out.marks.brokenPos = broken.pos();
broken.press('d');
out.marks.brokenDone = broken.done();

// ---- the stale mark -------------------------------------------------------------------------
//
// Re-run over the same path: one of the marked survivors is now caught, so the file's stamp
// changed. The mark that is STILL SURVIVING must be called out, never counted as progress.

const re = openTab(RERUN, store, '');
out.stale = { done: re.done(), states: [] };
for (let i = 0; i < 3; i++) { out.stale.states.push(re.state()); re.press('ArrowRight'); }

// Acknowledging a re-check is one keypress, and it sticks.
const ack = openTab(RERUN, store, '');
for (let i = 0; i < 3 && ack.state() !== 'recheck'; i++) ack.press('ArrowRight');
out.stale.ackFound = ack.state();
ack.press('d');
out.stale.afterAck = ack.done();
out.stale.afterAckReload = openTab(RERUN, store, '').done();

// ---- the click audit --------------------------------------------------------------------------
//
// EVERY control on the page, clicked as a browser would click it. The arrows shipped decorative
// once — drawn, labelled, disabled-state maintained, wired to nothing — because the harness only
// ever pressed keys. So each control here is exercised through a real click on the real element,
// and the assertion is that something changed.

const c = openTab(PAGE, {}, '');
out.clicks = { start: c.pos() };

c.clickSel('#next');
out.clicks.next = c.pos();
c.clickSel('#next');
out.clicks.nextAgain = c.pos();
c.clickSel('#prev');
out.clicks.prev = c.pos();

c.clickChip('[data-filter]', 'all');
out.clicks.filterAll = c.pos();
c.clickChip('[data-op]', 'comparison');
out.clicks.opComparison = c.pos();
c.clickChip('[data-op]', 'all');
c.clickChip('[data-filter]', 'survived');

// A mark in the source pane, clicked by the ids it carries.
c.clickMark(KEYS[3].split(':').slice(1).join(':'));      // the `numeric` finding
out.clicks.mark = c.pos();

// The done control on the card, and the reference disclosure beside it.
out.clicks.doneBefore = c.done();
c.clickSel('.donebtn');
out.clicks.doneAfter = c.done();
out.clicks.refBefore = /class="ref"/.test(c.card());
c.clickRef(KEYS[3].split(':').slice(1).join(':'));
out.clicks.refAfter = /class="ref"/.test(c.card());

// The theme toggle lives in the masthead, outside `#body`, so it keeps its own handler.
out.clicks.themeBefore = c.theme();
c.clickTheme();
out.clicks.themeAfter = c.theme();

// The index: its rows and its back button only exist on a multi-file report.
// Which VIEW is on screen, read off `#body` itself. `#pos` is a cached stub here and keeps its
// last text after the index replaces the markup that owned it, so it cannot answer this.
const isIndex = t => /Most survivors first/.test(t.body());

const mf = openTab(MULTI, {}, '');
out.index = { openHash: mf.hash(), openIndex: isIndex(mf) };
mf.clickRow(1);
out.index.afterRow = { pos: mf.pos(), hash: mf.hash(), index: isIndex(mf) };
mf.clickSel('#back');
out.index.afterBack = { hash: mf.hash(), index: isIndex(mf) };
// Escape is the keyboard route back, and must agree with the button.
mf.clickRow(0);
const viaRow = mf.hash();
mf.press('Escape');
out.index.afterEscape = { hash: mf.hash(), reachedFile: viaRow };

// ---- the legend ------------------------------------------------------------------------------
//
// It is built from the marks the pane just drew, so it is read back per filter, and from a report
// that actually contains the rare states as well as one that does not.

const lg = openTab(PAGE, {}, '');
out.legend = { survived: lg.legend() };
lg.clickChip('[data-filter]', 'caught');
out.legend.caught = lg.legend();
lg.clickChip('[data-filter]', 'all');
out.legend.all = lg.legend();
// The multi mark's own copy, straight out of the pane's markup.
out.legend.multiMark = /title="[^"]*?(\d+ findings here[^"]*)"/.exec(lg.source());
out.legend.multiMark = out.legend.multiMark ? out.legend.multiMark[1] : null;
// The badge's count is `data-n` on the multi mark — the number CSS draws in the corner.
const badge = /class="mark [^"]*\bmulti"[^>]*\sdata-n="(\d+)"/.exec(lg.source());
out.legend.multiBadge = badge ? badge[1] : null;

const us = openTab(UNSCORED, {}, '');
us.clickChip('[data-filter]', 'all');
out.legend.unscored = us.legend();

console.log(JSON.stringify(out));
