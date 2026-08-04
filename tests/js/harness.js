// A DOM stand-in just rich enough to run the report page's real inlined script in Node, so the
// page's *behaviour* — stepping, filtering, keyboard access, deep links — is tested rather than
// assumed.
//
// The script is a plain inline <script>, not a module, so it exports nothing; it is observed the
// way a reader observes it, through what it writes to the page. Every element is a recording stub
// looked up by selector and cached, so a value written through `$('#pos')` is still there when the
// assertion reads it back.
//
// Usage: node harness.js <page> <multi-file> <unscored>  ->  one JSON line of observations.

const fs = require('fs');
const vm = require('vm');

// The renderer is the last <script>; the first is the report's application/json data block.
function scriptsOf(file) {
  const page = fs.readFileSync(file, 'utf8');
  return [...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
}
function scriptOf(file) {
  const scripts = scriptsOf(file);
  return scripts[scripts.length - 1];
}
// The embedded report, exactly as the page carries it. The download button reads this block back
// out of the DOM, so the harness has to hold the page's REAL bytes here. A placeholder would let
// a button that downloads the wrong thing pass unnoticed.
function dataOf(file) { return scriptsOf(file)[0]; }

const OPS = JSON.parse(process.env.HARNESS_OPS || '[]');

// Where the report file pretends to live.
const HERE = 'file:///reports/report.html';

function openTab(file, hash) {
  const cache = new Map();

  // What the page handed the browser to download: one entry per click that really downloaded.
  const downloads = [];
  const revoked = [];

  function stub(dataset = {}) {
    const classes = new Set();
    const el = {
      dataset,
      style: {},
      disabled: false,
      // Only an <a> carrying a `download` ever uses this. Recording it is how the harness sees a
      // download actually happen, rather than seeing markup that would cause one.
      click() {
        if (this.download) downloads.push({ name: this.download, url: this.href });
      },
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

  // The source pane hands back a fresh row for whatever line the selection asks about. Marks used
  // to be cosmetic here (nothing under test read them back), so `.mark` queries were stubbed to
  // `[]` -- but `paintSelection()` now reads a mark's own `data-ids` to find every finding sharing
  // its token, so a `[]` stub makes that lookup always fail silently, never exercising the stacked
  // case at all. `paintSource()` still runs for REAL in this harness and writes REAL markup into
  // `src.innerHTML`, so parsing that string (same trick `multiMark`/`multiBadge` already use on
  // `source()`) reflects what the real code actually drew, not a hand-guessed stand-in that could
  // drift from it.
  // The row's own `.after(caretEl)` used to be a no-op stub -- fine while nothing read the caret
  // row back, but it means the stacked-findings markup would otherwise vanish into a detached node
  // the harness can never see again. Capturing it here is the only way to observe it: the page
  // keeps `caretEl` in a plain top-level `let`, which a `vm` context does not expose as a property
  // the way `var`/function declarations are.
  let lastCaret = null;
  const src = byId('#src');
  src.querySelector = sel => {
    if (!sel.startsWith('.row[data-line=')) return null;
    const row = stub();
    row.after = el => { lastCaret = el; };
    return row;
  };
  src.querySelectorAll = sel => {
    if (sel !== '.mark') return [];
    const re = /<button type="button" class="mark[^"]*"\s+data-ids="([^"]*)"/g;
    const out = [];
    let m;
    while ((m = re.exec(src._html))) out.push(stub({ ids: m[1] }));
    return out;
  };

  // The page's own `application/json` block, so what the download button reads back is what the
  // file really carries.
  byId('#mutation-test-report').textContent = dataOf(file);

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

  const location = {
    href: HERE + (hash || ''),
    pathname: HERE.slice(HERE.indexOf('///') + 2),
    search: '',
    hash: hash || '',
  };
  let onhash = null;
  const window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener: (kind, fn) => { if (kind === 'hashchange') onhash = fn; },
  };
  // A real-enough history stack, because "does the browser's own back button work" is now a
  // question the page answers rather than sidesteps. `stack` holds every entry and `cursor` is
  // where the reader stands in it, so a push that grows the stack and a replace that does not are
  // told apart by observing the history, never by watching which method got called.
  const stack = [HERE + (hash || '')];
  let cursor = 0;
  const apply = url => {
    const i = String(url).indexOf('#');
    location.hash = i < 0 ? '' : String(url).slice(i);
  };
  const history = {
    replaceState: (a, b, url) => { stack[cursor] = String(url); apply(url); },
    pushState: (a, b, url) => {
      stack.length = cursor + 1;         // a new branch discards anything ahead, as a browser does
      stack.push(String(url));
      cursor += 1;
      apply(url);
    },
  };

  // Enough of the Blob / object-URL pair to observe a download. Nothing here reaches a network,
  // and that is the point: the bytes handed to `Blob` are ones the page already holds.
  const blobs = new Map();
  let nextBlob = 0;
  class Blob {
    constructor(parts, opts) { this.text = parts.join(''); this.type = (opts || {}).type; }
  }
  const URL = {
    createObjectURL: blob => {
      const url = 'blob:' + (nextBlob += 1);
      blobs.set(url, blob);
      return url;
    },
    revokeObjectURL: url => { revoked.push(url); },
  };

  // The page defers its revoke by a tick so it cannot pull the URL out from under a download that
  // has only just started. Nothing here is about timing, so the queue is drained on demand by
  // `downloads()`, which empties it before reporting.
  const timers = [];
  const setTimeout_ = fn => { timers.push(fn); };
  const drain = () => { while (timers.length) timers.shift()(); };

  const context = vm.createContext({
    document, window, location, history, console, JSON, Blob, URL,
    setTimeout: setTimeout_,
  });
  vm.runInContext(scriptOf(file), context);

  const card = () => byId('#aside').innerHTML;

  // A REAL click: handed to the page's one delegated handler on `#body`, exactly as a browser
  // would. Nothing here calls `step()` or an element's own `onclick`, because that is precisely
  // the shortcut that let the ← / → arrows ship unwired while the tests stayed green.
  const hit = el => byId('#body').onclick({ target: el });

  return {
    pos: () => byId('#pos').textContent,
    hash: () => location.hash,
    card,
    theme: () => document.documentElement.dataset.theme,
    press: key => keydown({ key, target: { tagName: 'BODY' } }),
    // Click a control by the selector the page looks for it under.
    clickSel: sel => hit(target(sel)),
    clickChip: (group, value) => {
      hit(chips[group].find(c => (c.dataset.filter || c.dataset.op) === value));
    },
    // The index's rows and the source pane's marks are drawn as innerHTML, so the harness
    // fabricates the element the click lands on, carrying the data attributes the page reads.
    clickRow: i => hit(target('.frow', { file: String(i) })),
    // A column heading on the file index, and a rare-status count in the header. The heading rides
    // the same delegated handler as everything in `#body`; the header count reaches it through
    // `#head`, which is the wiring that would otherwise be easy to draw and forget to connect.
    clickSort: key => hit(target('[data-sort]', { sort: key })),
    clickHead: value => byId('#head').onclick({ target: target('[data-filter]', { filter: value }) }),
    // The index's file paths in the order it drew them, the whole of what a sort changes, read
    // off the markup rather than off the state variable that produced it.
    rows: () => [...byId('#filelist').innerHTML.matchAll(/class="fpath">([^<]*)</g)].map(m => m[1]),
    // The `data-file` each drawn row carries. A sort reorders the rows; it must NOT renumber them,
    // because that number is the only thing telling a click which file it opened.
    rowIds: () => [...byId('#filelist').innerHTML.matchAll(/data-file="(\d+)"/g)].map(m => m[1]),
    clickMark: ids => hit(target('.mark', { ids })),
    // A finding's own row inside the stacked caret annotation -- `.fg-body` and `.fg-label` both
    // carry the same `data-fid`, so either is a valid click target for switching to that finding.
    clickFg: fid => hit(target('.fg-body', { fid })),
    clickRef: fid => hit(target('.refbtn', { ref: fid })),
    clickTheme: () => byId('#theme').onclick(),
    next: () => byId('#next'),
    prev: () => byId('#prev'),
    // The legend is rebuilt from the marks the pane just drew, so it is an observation of the
    // pane, not of the template — which is the whole point of testing it here.
    legend: () => byId('#legend').innerHTML,
    body: () => byId('#body').innerHTML,
    source: () => byId('#src').innerHTML,
    // The caret row's own markup, or null before any finding has ever been selected (nothing has
    // called `.after()` yet).
    caret: () => (lastCaret ? lastCaret.innerHTML : null),
    // Someone pasting a link into the address bar of an already-open report.
    paste: h => { location.hash = h; if (onhash) onhash(); },
    clickDownload: () => byId('#dl').onclick(),
    // How many entries the page has put in the history, and where the reader stands in them.
    depth: () => stack.length,
    cursor: () => cursor,
    // The browser's OWN back button. Every entry this page creates differs from its neighbour by
    // fragment, so a real browser fires `hashchange` on the move, which is what this does.
    back: () => {
      if (cursor === 0) return false;
      cursor -= 1;
      apply(stack[cursor]);
      if (onhash) onhash();
      return true;
    },
    // What the download button produced, resolved back through the object URL it was handed.
    downloads: () => (drain(), downloads).map(d => {
      const blob = blobs.get(d.url);
      return { name: d.name, type: blob.type, text: blob.text, revoked: revoked.includes(d.url) };
    }),
  };
}

// ---- the observations the Python test asserts on -------------------------------------------

const PAGE = process.argv[2];      // the report
const MULTI = process.argv[3];     // a two-file report, for the index rows and the back button
const UNSCORED = process.argv[4];  // a report holding an ignored, an invalid and an errored mutant
const KEYS = JSON.parse(process.env.HARNESS_KEYS || '[]');

const out = { load: null, forward: [], backward: [], filters: {} };

// ---- stepping, filtering, the keyboard ------------------------------------------------------

const s = openTab(PAGE, '');
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

const d = openTab(PAGE, '');
out.deep = { onLoad: d.hash() };
d.press('ArrowRight');
d.press('ArrowRight');
out.deep.link = d.hash();
out.deep.posAtLink = d.pos();

// Paste that link into a fresh tab — the reload, and the "look at this survivor" case.
const restored = openTab(PAGE, out.deep.link);
out.deep.restoredPos = restored.pos();
out.deep.restoredHash = restored.hash();

// A link to a CAUGHT finding: it resolves, and the page widens the filter so it is actually on
// screen rather than resolving correctly into an empty pane.
const caught = openTab(PAGE, '#' + KEYS[5]);
out.deep.caughtPos = caught.pos();
out.deep.caughtHash = caught.hash();

// The source moved: the finding id no longer matches. The FILE it named is still the best answer.
const moved = openTab(PAGE, '#a.gd:999:1:2:comparison');
out.deep.movedPos = moved.pos();
out.deep.movedHash = moved.hash();

// A file this run did not cover: all the way back to the default view.
const gone = openTab(PAGE, '#nowhere/else.gd:1:1:2:comparison');
out.deep.gonePos = gone.pos();

// Garbage, including a percent-escape that throws inside decodeURIComponent.
const junk = openTab(PAGE, '#%%%not-a-key');
out.deep.junkPos = junk.pos();

// Pasting a link into an ALREADY-OPEN report: same document, so nothing reloads on its own.
const live = openTab(PAGE, '');
const firstLink = live.hash();
live.press('ArrowRight');
live.press('ArrowRight');
out.deep.beforePaste = live.pos();
live.paste(firstLink);
out.deep.afterPaste = live.pos();

// ---- the click audit --------------------------------------------------------------------------
//
// EVERY control on the page, clicked as a browser would click it. The arrows shipped decorative
// once — drawn, labelled, disabled-state maintained, wired to nothing — because the harness only
// ever pressed keys. So each control here is exercised through a real click on the real element,
// and the assertion is that something changed.

const c = openTab(PAGE, '');
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

// The reference disclosure on the card.
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

const mf = openTab(MULTI, '');
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

// ---- stacked findings on one token -------------------------------------------------------------
//
// Line 3 of `_SOURCE` carries the fixture's one multi mark: two `numeric` mutants (one finding, two
// angles) and a `statement-deletion` mutant whose span overlaps them (a second, different finding).
// Selecting either must show BOTH findings stacked in the caret row, not just the selected one --
// that is the whole point of the feature -- and clicking the other one's own row must switch to it.

const sk = openTab(PAGE, '');
sk.clickChip('[data-filter]', 'all');
const stackedIds = /class="mark [^"]*\bmulti"[^>]*\sdata-ids="([^"]*)"/.exec(sk.source())[1];
sk.clickMark(stackedIds);
out.stacked = { ids: stackedIds, firstPick: sk.caret(), firstCard: sk.card() };
// clickMark on a fresh selection always lands on ids[0] (`pick`'s -1-index fallback), so ids[1] is
// unambiguously "the other one" here, not a guess.
const otherFid = stackedIds.split(',')[1];
sk.clickFg(otherFid);
out.stacked.afterSwitch = { caret: sk.caret(), card: sk.card(), hash: sk.hash() };

// ---- the legend ------------------------------------------------------------------------------
//
// It is built from the marks the pane just drew, so it is read back per filter, and from a report
// that actually contains the rare states as well as one that does not.

const lg = openTab(PAGE, '');
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

const us = openTab(UNSCORED, '');
us.clickChip('[data-filter]', 'all');
out.legend.unscored = us.legend();

// ---- the header's rare-status counts ---------------------------------------------------------
//
// They live OUTSIDE `#body`, on `#head`, so a click on one has to travel a wiring the rest of the
// page does not use. Clicked here through that real element, not by setting `filter` by hand.

const rare = openTab(UNSCORED, '');
out.rare = { start: rare.pos() };
for (const status of ['Ignored', 'CompileError', 'RuntimeError']) {
  rare.clickHead('rare:' + status);
  out.rare[status] = rare.pos();
  // Whatever it selected must be a real position in the list it just filtered to, not a dash.
  rare.press('ArrowRight');
  out.rare[status + ':after-step'] = rare.pos();
}
// The three are genuinely different sets. A single count reaching "the unscored ones" lumped
// together would be the failure worth catching here.
rare.clickHead('rare:RuntimeError');
out.rare.runtimeCard = rare.card();

// Every click above leaves the operator chip at its default, so none of them can tell whether the
// status filter is the only thing deciding what a header count reaches. Narrow the operator first,
// to one that holds none of the counted mutants, then click the count. `RuntimeError` lives on
// `numeric` in this report and `comparison` holds an `Ignored`, so the two genuinely disagree.
const stale = openTab(UNSCORED, '');
stale.clickChip('[data-op]', 'comparison');
out.staleOp = { afterOpChip: stale.pos() };
stale.clickHead('rare:RuntimeError');
out.staleOp.afterHeaderCount = stale.pos();
out.staleOp.card = stale.card();

// From the INDEX, where there is no source pane to filter at all: the click has to open a file.
const rx = openTab(MULTI, '');
out.rareIndex = { openedOnIndex: isIndex(rx) };
rx.clickHead('rare:Timeout');
out.rareIndex.afterClick = { index: isIndex(rx), pos: rx.pos() };

// ---- the file index's sortable columns -------------------------------------------------------
//
// Read off the rows the index actually drew, in order, so this observes the sort rather than the
// state variable behind it.

const sortTab = openTab(MULTI, '');
out.sort = { initial: sortTab.rows() };
sortTab.clickSort('survived');            // the same column again flips the direction
out.sort.survivedAsc = sortTab.rows();
sortTab.clickSort('survived');
out.sort.survivedBack = sortTab.rows();
sortTab.clickSort('file');
out.sort.file = sortTab.rows();
sortTab.clickSort('file');
out.sort.fileDesc = sortTab.rows();
sortTab.clickSort('mutants');
out.sort.mutants = sortTab.rows();
sortTab.clickSort('caught');
out.sort.caught = sortTab.rows();
sortTab.clickSort('score');
out.sort.score = sortTab.rows();
// A sort reorders rows without renumbering them. That number is what a click reports back, so a
// sort that renumbered would open the wrong file, silently, and only on a re-sorted index.
sortTab.clickSort('file');
out.sort.fileIds = sortTab.rowIds();
sortTab.clickSort('file');
out.sort.fileDescIds = sortTab.rowIds();
sortTab.clickRow(sortTab.rowIds()[0]);           // the first row as DRAWN, which is now the last file
out.sort.openedFirstDrawn = sortTab.hash();

// ---- the JSON download -------------------------------------------------------------------------

const dl = openTab(PAGE, '');
out.download = { before: dl.downloads().length };
dl.clickDownload();
const got = dl.downloads();
out.download.count = got.length;
out.download.name = got[0].name;
out.download.type = got[0].type;
out.download.revoked = got[0].revoked;
// The bytes must be the report itself, parsed rather than string-matched.
out.download.parsed = JSON.parse(got[0].text);

// ---- the browser's own back button -------------------------------------------------------------
//
// Structural moves get a history entry; stepping does not. Observed as the depth of the history
// and the reader's position in it, so "it pushed" and "it replaced" are told apart by what the
// browser is left holding.

const h = openTab(MULTI, '');
out.history = { onOpen: { depth: h.depth(), cursor: h.cursor(), index: isIndex(h) } };
h.clickRow(0);                                   // index -> file: structural
out.history.afterOpen = { depth: h.depth(), cursor: h.cursor(), index: isIndex(h) };
h.press('ArrowRight');
h.press('ArrowRight');
h.clickSel('#next');
out.history.afterSteps = { depth: h.depth(), cursor: h.cursor(), pos: h.pos() };
h.back();                                        // the BROWSER's back button
out.history.afterBack = { depth: h.depth(), cursor: h.cursor(), index: isIndex(h) };
// ...and the in-page button pushes the same kind of entry, so the two agree.
h.clickRow(0);
h.clickSel('#back');
out.history.afterInPageBack = { depth: h.depth(), cursor: h.cursor(), index: isIndex(h) };

// A single-file report has no index and therefore no structural move, so its history must be
// exactly what it was: one entry, however far the reader steps.
const solo = openTab(PAGE, '');
out.history.solo = { onOpen: solo.depth() };
for (let i = 0; i < 6; i++) solo.press('ArrowRight');
solo.clickSel('#prev');
solo.clickMark(KEYS[3].split(':').slice(1).join(':'));
out.history.solo.afterSteps = { depth: solo.depth(), cursor: solo.cursor(), pos: solo.pos() };

// A deep link still opens on its finding, and opening one costs no extra entry either.
const deepHist = openTab(PAGE, '#' + KEYS[3]);
out.history.deepLink = { depth: deepHist.depth(), pos: deepHist.pos() };

console.log(JSON.stringify(out));
