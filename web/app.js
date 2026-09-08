'use strict';

// One grid per dataset. The tables are far too big to hold in the browser, so
// the server owns filtering, sorting and paging. Search is the interesting
// part: the server returns the ordinal position of every matching cell within
// the current filtered+sorted result set, so next/previous can jump to a match
// that lives on a page we have not loaded yet -- we page to it and scroll.
//
// Both tabs run this same code against different datasets, so a third table
// would be an entry in DATASETS on the server and nothing here.

const NUMERIC = new Set(['INTEGER', 'DOUBLE']);

function getJSON(url) {
  return fetch(url).then(async (res) => {
    const body = await res.json().catch(() => ({ error: res.statusText }));
    if (!res.ok) throw new Error(body.error || 'request failed');
    return body;
  });
}

function banner(msg) {
  const el = document.getElementById('banner');
  el.hidden = !msg;
  el.textContent = msg || '';
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function createGrid(root, key) {
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
    loaded: false,
  };

  const el = (cls) => root.querySelector('.' + cls);

  function params(extra = {}) {
    const p = new URLSearchParams({ dataset: key });
    for (const [col, val] of Object.entries(state.filters)) if (val) p.set('f_' + col, val);
    if (state.sort) { p.set('sort', state.sort); p.set('dir', state.dir); }
    for (const [k, v] of Object.entries(extra)) p.set(k, v);
    return p;
  }

  // ------------------------------------------------------------- rendering

  // Built once. Rebuilding on every reload would destroy the filter <input>
  // the user is typing into -- the debounced reload lands mid-word and the
  // rest of the keystrokes go to a detached element.
  function buildHead() {
    const head = el('g-headrow'), filt = el('g-filterrow');
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

  /** Refresh only the bits of the header that depend on state, never the nodes. */
  function updateHead() {
    for (const btn of root.querySelectorAll('.g-headrow .hdr')) {
      const active = state.sort === btn.dataset.col;
      btn.querySelector('.arrow').textContent = active ? (state.dir === 'asc' ? '▲' : '▼') : '';
    }
    for (const input of root.querySelectorAll('.g-filterrow input')) {
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
    const body = el('g-body');
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

    el('g-empty').hidden = data.rows.length > 0;
  }

  function renderFooter() {
    const from = state.total === 0 ? 0 : state.offset + 1;
    const to = Math.min(state.offset + state.limit, state.total);
    el('g-pageinfo').textContent =
      `${from.toLocaleString()}–${to.toLocaleString()} of ${state.total.toLocaleString()}`;
    el('g-first').disabled = el('g-pprev').disabled = state.offset === 0;
    const lastOffset = Math.max(0, Math.floor((state.total - 1) / state.limit) * state.limit);
    el('g-pnext').disabled = el('g-last').disabled = state.offset >= lastOffset;
  }

  function renderMatchInfo() {
    const n = state.matches.length;
    el('g-matchinfo').textContent = !state.query ? ''
      : n === 0 ? 'no matches'
      : `${state.matchIndex + 1} / ${n.toLocaleString()}${state.truncated ? '+' : ''}`;
    el('g-next').disabled = el('g-prev').disabled = n === 0;
  }

  // --------------------------------------------------------------- loading

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
    const q = el('g-q').value.trim();
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
    if (state.truncated) {
      banner(`Showing the first ${data.total.toLocaleString()} matches — narrow the filters for more.`);
    }
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
    const cell = root.querySelector('td[data-current="1"]');
    if (cell) cell.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
  }

  // ---------------------------------------------------------------- wiring

  el('g-q').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (state.query === el('g-q').value.trim() && state.matches.length) {
      goToMatch(state.matchIndex + (e.shiftKey ? -1 : 1));
    } else {
      runSearch();
    }
  });
  el('g-q').addEventListener('input', debounce(() => runSearch(), 350));
  el('g-next').onclick = () => goToMatch(state.matchIndex + 1);
  el('g-prev').onclick = () => goToMatch(state.matchIndex - 1);

  el('g-clear').onclick = () => {
    state.filters = {}; state.query = ''; state.matches = []; state.matchIndex = -1;
    state.sort = ''; state.dir = 'asc'; state.offset = 0;
    el('g-q').value = '';
    reload();
  };

  el('g-first').onclick = () => { state.offset = 0; reload(); };
  el('g-pprev').onclick = () => { state.offset = Math.max(0, state.offset - state.limit); reload(); };
  el('g-pnext').onclick = () => { state.offset += state.limit; reload(); };
  el('g-last').onclick = () => {
    state.offset = Math.max(0, Math.floor((state.total - 1) / state.limit) * state.limit);
    reload();
  };
  el('g-limit').onchange = (e) => { state.limit = +e.target.value; state.offset = 0; reload(); };

  return {
    key,
    focusSearch: () => el('g-q').select(),
    /** Fetched on first view, so opening the app does not query every table. */
    async ensureLoaded() {
      if (state.loaded) return;
      state.loaded = true;
      try {
        const [cols, st] = await Promise.all([
          getJSON('/api/columns?dataset=' + key),
          getJSON('/api/stats?dataset=' + key),
        ]);
        state.columns = cols.columns;
        el('g-stats').textContent =
          `${st.rows.toLocaleString()} rows · ${st.files.toLocaleString()} ${st.unit} · ${st.from} → ${st.to}`;
        buildHead();
        await reload();
      } catch (e) {
        state.loaded = false;
        banner(e.message);
      }
    },
  };
}

// ------------------------------------------------------------------- boot

const grids = new Map();

(async function init() {
  try {
    const { datasets } = await getJSON('/api/datasets');
    const tabs = document.querySelector('.tabs');
    const panels = document.getElementById('panels');
    const tpl = document.getElementById('gridtpl');
    const settingsTab = document.getElementById('tab-settings');

    for (const ds of datasets) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.setAttribute('role', 'tab');
      btn.id = 'tab-' + ds.key;
      btn.setAttribute('aria-controls', 'panel-' + ds.key);
      btn.setAttribute('aria-selected', 'false');
      btn.dataset.tab = ds.key;
      btn.textContent = ds.label;
      tabs.insertBefore(btn, settingsTab);   // Settings stays last

      const panel = document.createElement('section');
      panel.id = 'panel-' + ds.key;
      panel.className = 'panel';
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', btn.id);
      panel.hidden = true;
      panel.appendChild(tpl.content.cloneNode(true));
      panels.appendChild(panel);

      grids.set(ds.key, createGrid(panel, ds.key));
    }

    // settings.js owns tab switching; tell it what exists and which to open
    window.nmOnTabShown = (name) => {
      const g = grids.get(name);
      if (g) g.ensureLoaded();
    };
    window.nmShowTab(location.hash.slice(1) || datasets[0].key, false);
  } catch (e) {
    banner(e.message);
  }
})();

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
    const open = document.querySelector('.panel:not([hidden])');
    const g = open && grids.get(open.id.replace('panel-', ''));
    if (g) { e.preventDefault(); g.focusSearch(); }
  }
});
