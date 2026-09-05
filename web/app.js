'use strict';

// The table is far too big to hold in the browser, so the server owns
// filtering, sorting and paging. Search is the interesting part: the server
// returns the ordinal position of every matching cell within the current
// filtered+sorted result set, so next/previous can jump to a match that lives
// on a page we have not loaded yet -- we just page to it and scroll.

const state = {
  columns: [],
  offset: 0,
  limit: 100,
  total: 0,
  sort: '',
  dir: 'asc',
  filters: {},          // column -> filter expression
  query: '',
  matches: [],          // [rowOrdinal, columnName][]
  matchIndex: -1,
  truncated: false,
};

const $ = (id) => document.getElementById(id);
const NUMERIC = new Set(['INTEGER', 'DOUBLE']);

function params(extra = {}) {
  const p = new URLSearchParams();
  for (const [col, val] of Object.entries(state.filters)) if (val) p.set('f_' + col, val);
  if (state.sort) { p.set('sort', state.sort); p.set('dir', state.dir); }
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p;
}

async function getJSON(url) {
  const res = await fetch(url);
  const body = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(body.error || 'request failed');
  return body;
}

function banner(msg) {
  const el = $('banner');
  el.hidden = !msg;
  el.textContent = msg || '';
}

// ---------------------------------------------------------------- rendering

// Built once. Rebuilding on every reload would destroy the filter <input> the
// user is typing into -- the debounced reload lands mid-word and the rest of
// the keystrokes go to a detached element.
function buildHead() {
  const head = $('headrow'), filt = $('filterrow');
  head.innerHTML = ''; filt.innerHTML = '';

  for (const col of state.columns) {
    const th = document.createElement('th');
    const btn = document.createElement('button');
    btn.className = 'hdr';
    btn.dataset.col = col.name;
    btn.title = `${col.name} (${col.type}) — click to sort`;
    btn.innerHTML = `<span>${col.label}</span><span class="arrow"></span>`;
    btn.onclick = () => {
      if (state.sort === col.name) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      else { state.sort = col.name; state.dir = 'asc'; }
      state.offset = 0;
      updateHead();
      reload({ research: true });
    };
    th.appendChild(btn);
    head.appendChild(th);

    const fth = document.createElement('th');
    const input = document.createElement('input');
    const ranged = NUMERIC.has(col.type) || col.type === 'DATE' || col.type === 'TIME';
    input.placeholder = ranged ? '>5, 1..9' : 'filter…';
    input.value = state.filters[col.name] || '';
    input.title = ranged
      ? 'Substring, or a comparison: >5  <=2.5  1..9'
      : 'Case-insensitive substring, or =exact';
    if (input.value) input.classList.add('active');
    input.dataset.col = col.name;
    input.oninput = debounce(() => {
      const v = input.value.trim();
      if (v) state.filters[col.name] = v; else delete state.filters[col.name];
      input.classList.toggle('active', !!v);
      state.offset = 0;
      reload({ research: true });
    }, 300);
    fth.appendChild(input);
    filt.appendChild(fth);
  }
}

/** Refresh only the bits of the header that depend on state, never the DOM nodes. */
function updateHead() {
  for (const btn of document.querySelectorAll('#headrow .hdr')) {
    const active = state.sort === btn.dataset.col;
    btn.querySelector('.arrow').textContent = active ? (state.dir === 'asc' ? '▲' : '▼') : '';
  }
  for (const input of document.querySelectorAll('#filterrow input')) {
    const want = state.filters[input.dataset.col] || '';
    // only touch an input the user is not currently typing into
    if (input !== document.activeElement && input.value !== want) input.value = want;
    input.classList.toggle('active', !!want);
  }
}

function highlight(text, needle) {
  if (!needle) return document.createTextNode(text);
  const frag = document.createDocumentFragment();
  const hay = text.toLowerCase(), find = needle.toLowerCase();
  let i = 0, at;
  while ((at = hay.indexOf(find, i)) !== -1) {
    if (at > i) frag.appendChild(document.createTextNode(text.slice(i, at)));
    const m = document.createElement('mark');
    m.textContent = text.slice(at, at + needle.length);
    frag.appendChild(m);
    i = at + needle.length;
  }
  if (i < text.length) frag.appendChild(document.createTextNode(text.slice(i)));
  return frag;
}

function renderRows(data) {
  const body = $('body');
  body.innerHTML = '';
  const hits = new Map();   // "ordinal:column" -> true, for cells on this page
  for (const [rn, col] of state.matches) {
    if (rn >= state.offset && rn < state.offset + data.rows.length) hits.set(rn + ':' + col, true);
  }
  const cur = state.matchIndex >= 0 ? state.matches[state.matchIndex] : null;

  data.rows.forEach((row, r) => {
    const ordinal = state.offset + r;
    const tr = document.createElement('tr');
    tr.dataset.rn = ordinal;
    row.forEach((val, c) => {
      const col = state.columns[c];
      const td = document.createElement('td');
      if (val === null) { td.textContent = '—'; td.className = 'null'; }
      else {
        td.appendChild(highlight(val, state.query));
        if (NUMERIC.has(col.type)) td.className = 'num';
      }
      if (hits.has(ordinal + ':' + col.name)) td.classList.add('hit');
      if (cur && cur[0] === ordinal && cur[1] === col.name) {
        td.classList.add('current');
        td.dataset.current = '1';
      }
      td.title = `${col.label}: ${val === null ? 'NULL' : val}`;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });

  $('empty').hidden = data.rows.length > 0;
}

function renderFooter() {
  const from = state.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + state.limit, state.total);
  $('pageinfo').textContent = `${from.toLocaleString()}–${to.toLocaleString()} of ${state.total.toLocaleString()}`;
  $('first').disabled = $('pprev').disabled = state.offset === 0;
  const lastOffset = Math.max(0, Math.floor((state.total - 1) / state.limit) * state.limit);
  $('pnext').disabled = $('last').disabled = state.offset >= lastOffset;
}

function renderMatchInfo() {
  const n = state.matches.length;
  $('matchinfo').textContent = !state.query ? ''
    : n === 0 ? 'no matches'
    : `${state.matchIndex + 1} / ${n.toLocaleString()}${state.truncated ? '+' : ''}`;
  $('next').disabled = $('prev').disabled = n === 0;
}

// ------------------------------------------------------------------- loading

async function reload({ research = false } = {}) {
  try {
    if (research) await runSearch({ jump: false });
    const data = await getJSON('/api/rows?' + params({ offset: state.offset, limit: state.limit }));
    state.total = data.total;
    if (state.offset >= state.total && state.total > 0) {
      state.offset = Math.floor((state.total - 1) / state.limit) * state.limit;
      return reload();
    }
    updateHead();
    renderRows(data);
    renderFooter();
    renderMatchInfo();
    banner('');
  } catch (e) {
    banner(e.message);
  }
}

async function runSearch({ jump = true } = {}) {
  const q = $('q').value.trim();
  state.query = q;
  if (!q) {
    state.matches = []; state.matchIndex = -1; state.truncated = false;
    renderMatchInfo();
    return;
  }
  const data = await getJSON('/api/search?' + params({ q }));
  state.matches = data.matches;
  state.truncated = data.truncated;
  state.matchIndex = state.matches.length ? 0 : -1;
  if (state.truncated) banner(`Showing the first ${data.total.toLocaleString()} matches — narrow the filters for more.`);
  renderMatchInfo();
  if (jump && state.matchIndex >= 0) await goToMatch(0);
}

async function goToMatch(index) {
  if (!state.matches.length) return;
  const n = state.matches.length;
  state.matchIndex = ((index % n) + n) % n;      // wrap at both ends
  const [ordinal] = state.matches[state.matchIndex];
  const page = Math.floor(ordinal / state.limit) * state.limit;
  if (page !== state.offset) {
    state.offset = page;
    await reload();
  } else {
    const data = await getJSON('/api/rows?' + params({ offset: state.offset, limit: state.limit }));
    renderRows(data);
    renderMatchInfo();
  }
  const cell = document.querySelector('td[data-current="1"]');
  if (cell) cell.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// --------------------------------------------------------------------- wiring

$('q').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  if (state.query === $('q').value.trim() && state.matches.length) {
    goToMatch(state.matchIndex + (e.shiftKey ? -1 : 1));
  } else {
    runSearch();
  }
});
$('q').addEventListener('input', debounce(() => runSearch(), 350));
$('next').onclick = () => goToMatch(state.matchIndex + 1);
$('prev').onclick = () => goToMatch(state.matchIndex - 1);

$('clear').onclick = () => {
  state.filters = {}; state.query = ''; state.matches = []; state.matchIndex = -1;
  state.sort = ''; state.dir = 'asc'; state.offset = 0;
  $('q').value = '';
  reload();
};

$('first').onclick = () => { state.offset = 0; reload(); };
$('pprev').onclick = () => { state.offset = Math.max(0, state.offset - state.limit); reload(); };
$('pnext').onclick = () => { state.offset += state.limit; reload(); };
$('last').onclick = () => {
  state.offset = Math.max(0, Math.floor((state.total - 1) / state.limit) * state.limit);
  reload();
};
$('limit').onchange = (e) => { state.limit = +e.target.value; state.offset = 0; reload(); };

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); $('q').select(); }
});

(async function init() {
  try {
    const [cols, st] = await Promise.all([getJSON('/api/columns'), getJSON('/api/stats')]);
    state.columns = cols.columns;
    $('stats').textContent =
      `${st.rows.toLocaleString()} rows · ${st.files.toLocaleString()} files · ${st.from} → ${st.to}`;
    buildHead();
    await reload();
  } catch (e) {
    banner(e.message);
  }
})();
