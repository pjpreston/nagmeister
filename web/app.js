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

// A small right-click menu. One element shared by every grid, since only one
// menu can be open at a time; it closes on any click, Escape, scroll or resize.
let menuEl = null;

function closeMenu() {
  if (menuEl) menuEl.hidden = true;
}

/** Open the menu at a viewport point. items: [label, action][] */
function openMenu(x, y, items) {
  if (!menuEl) {
    menuEl = document.createElement('div');
    menuEl.className = 'ctxmenu';
    menuEl.setAttribute('role', 'menu');
    document.body.append(menuEl);
    document.addEventListener('click', closeMenu);
    // capture runs before the cell's own handler, so right-clicking another
    // horse closes this menu and then opens a fresh one
    document.addEventListener('contextmenu', closeMenu, true);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
    window.addEventListener('resize', closeMenu);
    // capture, so a scroll inside the panel closes it too, not only the page
    document.addEventListener('scroll', closeMenu, true);
  }
  menuEl.textContent = '';
  for (const [label, action] of items) {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.textContent = label;
    b.onclick = () => { closeMenu(); action(); };
    menuEl.append(b);
  }
  menuEl.hidden = false;
  // keep it on screen when opened near the right or bottom edge
  const { width, height } = menuEl.getBoundingClientRect();
  menuEl.style.left = Math.min(x, window.innerWidth - width - 4) + 'px';
  menuEl.style.top = Math.min(y, window.innerHeight - height - 4) + 'px';
  menuEl.querySelector('button').focus({ preventScroll: true });
}

function createGrid(root, ds) {
  const key = ds.key;
  const state = {
    selected: null,      // the race the drill-down is pinned to, if any
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
      // the server explains what the column means; this only adds the
      // interaction, so a column with no description still says what it is
      btn.title = `${col.label}\n\n${col.desc || `${col.name} (${col.type})`}`
        + '\n\nClick to sort';
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
      if (ds.detail) {
        // a row is a race; clicking it pins the drill-down to that race
        const pick = raceKeyOf(row);
        tr.classList.add('pickable');
        if (state.selected && sameRace(pick, state.selected)) tr.classList.add('selected');
        tr.onclick = () => selectRace(pick);
      }
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
    state.selected = null;
    el('g-q').value = '';
    const out = root.querySelector('.rc-out');
    const btn = root.querySelector('.rc-view');
    const hint = root.querySelector('.rc-hint');
    if (out) out.innerHTML = '';
    clearDetail();
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = 'Select a race above.';
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

  /** Pull (date, track, time) out of a rendered row by column name. */
  function raceKeyOf(row) {
    const at = (name) => row[state.columns.findIndex((c) => c.name === name)];
    return { date: at('race_date'), track: at('track'), time: at('race_time') };
  }

  const sameRace = (a, b) => a && b && a.date === b.date && a.track === b.track && a.time === b.time;

  function selectRace(pick) {
    state.selected = pick;
    // a new race means a new subject for the chat, and the old runner is no
    // longer one of its runners
    state.horse = null;
    if (chatPanel) chatPanel.refresh();
    for (const tr of root.querySelectorAll('.g-body tr')) {
      const row = [...tr.children].map((td) => (td.classList.contains('null') ? null : td.textContent));
      tr.classList.toggle('selected', sameRace(raceKeyOf(row), pick));
    }
    const btn = root.querySelector('.rc-view');
    const hint = root.querySelector('.rc-hint');
    if (btn) btn.disabled = false;
    if (hint) hint.textContent = `${pick.track} ${pick.time} — ${pick.date}`;
  }

  // the form graph under the card, if this dataset has the drill-down. Set at
  // the bottom of this factory, once the panel's markup exists.
  let chart = null;

  /** A one-line message in a detail pane, replacing whatever was there. */
  function note(host, msg) {
    host.textContent = '';
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = msg;
    host.append(p);
  }

  async function showRaceCard() {
    const out = root.querySelector('.rc-out');
    const pick = state.selected;
    if (!out || !pick) return;
    note(out, 'Loading…');
    clearDetail();
    if (chart) chart.clear();
    state.horse = null;
    if (chatPanel) chatPanel.refresh();
    try {
      const q = new URLSearchParams({ date: pick.date, track: pick.track, time: pick.time });
      const data = await getJSON('/api/racecard?' + q);
      if (!data.rows.length) {
        note(out, data.complete
          ? 'No runners recorded for this race.'
          : 'No runners held for this race: each horse is kept only from its newest card, '
            + 'and every runner here has been declared again since.');
        return;
      }
      // built through the DOM rather than innerHTML: the cells are horse,
      // jockey and trainer names out of the database, and each row needs a
      // click handler anyway
      out.textContent = '';
      const h3 = document.createElement('h3');
      h3.className = 'rc-title';
      h3.append(document.createTextNode(`${pick.track} ${pick.time} · ${pick.date} `));
      const count = document.createElement('span');
      count.className = 'hint';
      count.textContent = `${data.rows.length} runners`;
      // a past card keeps only the horses not declared again since, so the
      // count is not the field size -- say so rather than let it pass for one
      if (!data.complete) {
        count.textContent += ' held — runners declared again since are on their newer card';
      }
      h3.append(count);

      const table = document.createElement('table');
      table.className = 'rc-table grid';
      const thead = document.createElement('thead');
      const hrow = document.createElement('tr');
      for (const c of data.columns) {
        const th = document.createElement('th');
        th.textContent = c.label;
        th.title = `${c.label}\n\n${c.desc || `${c.name} (${c.type})`}`;
        hrow.append(th);
      }
      thead.append(hrow);
      const tbody = document.createElement('tbody');
      const horseAt = data.columns.findIndex((c) => c.name === 'horse');
      const investAt = data.columns.findIndex((c) => c.name === 'one_pnd_invest');

      data.rows.forEach((r) => {
        const tr = document.createElement('tr');
        const horse = r[horseAt];
        r.forEach((v, i) => {
          const td = document.createElement('td');
          if (v === null) { td.textContent = '—'; td.className = 'null'; }
          else {
            td.textContent = v;
            if (NUMERIC.has(data.columns[i].type)) td.className = 'num';
          }
          if (i === horseAt && horse) {
            td.classList.add('rc-horse');
            td.title = `${horse}\n\nRight-click for this horse's previous races`;
            td.oncontextmenu = (e) => {
              e.preventDefault();
              openMenu(e.clientX, e.clientY, [
                ['Show history', () => showHistory(horse)],
              ]);
            };
          }
          if (i === investAt && horse) {
            td.title = `£1 invest: ${v === null ? '—' : v}\n\nRight-click to see how it is worked out`;
            td.oncontextmenu = (e) => {
              e.preventDefault();
              openMenu(e.clientX, e.clientY, [
                ['Calculation', () => showCalculation(horse, v)],
              ]);
            };
          }
          tr.append(td);
        });
        if (chart && horse) {
          tr.classList.add('pickable');
          tr.onclick = () => selectHorse(horse);
        }
        tbody.append(tr);
      });
      table.append(thead, tbody);
      out.append(h3, table);

      // open on the favourite, so the graph is never an empty frame waiting to
      // be clicked -- the card is already sorted into market order
      const first = data.rows[0] && data.rows[0][horseAt];
      if (first) selectHorse(first);
    } catch (e) {
      out.textContent = '';
      banner(e.message);
    }
  }

  /** Load and plot one runner's form, and mark its row as the chosen one. */
  async function selectHorse(horse) {
    const pick = state.selected;
    if (!chart || !pick) return;
    state.horse = horse;
    if (chatPanel) chatPanel.refresh();
    for (const tr of root.querySelectorAll('.rc-table tbody tr')) {
      const cell = tr.querySelector('td');
      tr.classList.toggle('selected', !!cell && cell.textContent === horse);
    }
    try {
      const q = new URLSearchParams({
        date: pick.date, track: pick.track, time: pick.time, horse,
      });
      chart.show(await getJSON('/api/horseform?' + q));
    } catch (e) {
      banner(e.message);
    }
  }

  // The slot beneath the card holds one detail at a time -- a runner's history
  // or the working behind its £1 invest -- so opening one replaces the other.
  function clearDetail() {
    const out = root.querySelector('.hh-out');
    if (out) out.textContent = '';
  }

  /** Empty the detail slot and give it a heading with a close button. */
  function detailHead(out, title, hint) {
    out.textContent = '';
    const h3 = document.createElement('h3');
    h3.className = 'rc-title';
    h3.append(document.createTextNode(title + ' '));
    const count = document.createElement('span');
    count.className = 'hint';
    count.textContent = hint;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'hh-close ghost';
    close.title = 'Close';
    close.setAttribute('aria-label', 'Close');
    close.textContent = '×';
    close.onclick = clearDetail;
    h3.append(count, close);
    out.append(h3);
  }

  function detailHint(out, msg) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = msg;
    out.append(p);
  }

  /** A read-only table, built through the DOM: names come from the database. */
  function detailTable(columns, rows) {
    const table = document.createElement('table');
    table.className = 'rc-table grid';
    const thead = document.createElement('thead');
    const hrow = document.createElement('tr');
    for (const c of columns) {
      const th = document.createElement('th');
      th.textContent = c.label;
      th.title = `${c.label}\n\n${c.desc || `${c.name} (${c.type})`}`;
      hrow.append(th);
    }
    thead.append(hrow);
    const tbody = document.createElement('tbody');
    for (const r of rows) {
      const tr = document.createElement('tr');
      r.forEach((v, i) => {
        const td = document.createElement('td');
        if (v === null) { td.textContent = '—'; td.className = 'null'; }
        else {
          td.textContent = v;
          if (NUMERIC.has(columns[i].type)) td.className = 'num';
        }
        tr.append(td);
      });
      tbody.append(tr);
    }
    table.append(thead, tbody);
    return table;
  }

  /** List a runner's previous races in a table beneath the card. */
  async function showHistory(horse) {
    const out = root.querySelector('.hh-out');
    const pick = state.selected;
    if (!out || !pick) return;
    note(out, 'Loading…');
    try {
      const data = await getJSON('/api/horsehistory?' + new URLSearchParams({ date: pick.date, horse }));
      const n = data.rows.length;
      detailHead(out, `${horse} — history`, `${n} previous ${n === 1 ? 'race' : 'races'}`);
      if (!n) {
        detailHint(out, 'No earlier races recorded for this horse.');
        return;
      }
      out.append(detailTable(data.columns, data.rows));
      out.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch (e) {
      out.textContent = '';
      banner(e.message);
    }
  }

  const money = (x) => (x < 0 ? '−£' : '£') + Math.abs(x).toFixed(2);

  /**
   * Show how a runner's £1 invest is worked out: one £1 win bet per previous
   * race, with its stake, return and profit, and a running total that ends on
   * the figure in the card.
   *
   * Read from /api/horseform, whose rows are the very runs the card sums and
   * whose `invest` value is each one's profit or loss -- so the bottom line
   * here cannot disagree with the cell. `shown` is the cell's own value, to
   * check against.
   */
  async function showCalculation(horse, shown) {
    const out = root.querySelector('.hh-out');
    const pick = state.selected;
    if (!out || !pick) return;
    note(out, 'Loading…');
    try {
      const q = new URLSearchParams({ date: pick.date, track: pick.track, time: pick.time, horse });
      const { points } = await getJSON('/api/horseform?' + q);
      detailHead(out, `${horse} — £1 invest calculation`,
        `${points.length} previous ${points.length === 1 ? 'race' : 'races'}`);
      if (!points.length) {
        detailHint(out, 'No previous races, so no bets to add up: the cell is empty.');
        return;
      }

      detailHint(out, 'A £1 bet to win on every previous race. Industry SP is decimal '
        + 'odds with the stake included, so a winner at 6.0 returns £6 — £5 profit — '
        + 'and any other result loses the £1 stake.');

      let staked = 0, returned = 0, total = 0, wins = 0, unknown = 0;
      const rows = points.map((p) => {
        const pl = p.values.invest;
        const sp = p.values.industry_sp;
        const won = p.place_text === '1';
        if (pl === null) {
          // a winner with no SP recorded: its profit cannot be known, so it is
          // left out of the total -- stake included -- as the card leaves it out
          unknown += 1;
          return [p.date, p.track, p.place_text, sp, '—', '—', 'not counted', money(total)];
        }
        staked += 1;
        total += pl;
        if (won) { wins += 1; returned += pl + 1; }
        return [p.date, p.track, p.place_text, sp === null ? null : sp.toFixed(2),
          money(1), money(won ? pl + 1 : 0), money(pl), money(total)];
      });
      const cols = [
        { name: 'race_date', label: 'Date', type: 'DATE', desc: 'Date of the race' },
        { name: 'track', label: 'Track', type: 'VARCHAR', desc: 'Racecourse' },
        { name: 'place', label: 'Place', type: 'VARCHAR', desc: 'Where it finished; 1 is a win' },
        { name: 'industry_sp', label: 'Industry SP', type: 'DOUBLE', desc: 'Decimal starting price, stake included' },
        { name: 'stake', label: 'Stake', type: 'DOUBLE', desc: 'The £1 bet on this race' },
        { name: 'return', label: 'Return', type: 'DOUBLE', desc: 'What came back: the SP if it won, otherwise nothing' },
        { name: 'pl', label: 'Profit/Loss', type: 'DOUBLE', desc: 'Return less stake: SP − 1 for a win, −1 otherwise' },
        { name: 'running', label: 'Running total', type: 'DOUBLE', desc: 'Profit or loss so far, oldest race first' },
      ];
      out.append(detailTable(cols, rows));

      const sum = document.createElement('p');
      sum.className = 'calc-sum';
      sum.textContent = `${staked} × £1 staked = ${money(staked)}.  `
        + `${wins} ${wins === 1 ? 'win' : 'wins'} returned ${money(returned)}.  `
        + `${money(returned)} − ${money(staked)} = ${money(total)}`;
      out.append(sum);
      if (unknown) {
        detailHint(out, `${unknown} winning ${unknown === 1 ? 'run has' : 'runs have'} no SP `
          + 'recorded, so its profit cannot be known and it is left out, stake and all.');
      }
      // the card rounds to 2dp; anything more than a rounding step apart means
      // the two are no longer reading the same runs, which should never happen
      if (shown !== null && Math.abs(Number(shown) - total) > 0.011) {
        detailHint(out, `This does not match the card's ${money(Number(shown))} — reload the card.`);
      }
      out.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch (e) {
      out.textContent = '';
      banner(e.message);
    }
  }

  if (ds.detail === 'racecard') {
    const btn = root.querySelector('.rc-view');
    if (btn) btn.onclick = showRaceCard;
    const host = root.querySelector('.hf-out');
    if (host && window.nmHorseChart) chart = window.nmHorseChart(host);
  }

  // the model list is fetched once, when the tab is first opened
  const chatHost = root.querySelector('.chat');
  // The chat asks the grid what the user is looking at, rather than the grid
  // pushing into the chat: the selection lives here, and this way the panel
  // always reads the current value instead of a copy that can go stale.
  const chatPanel = chatHost && window.nmChat
    ? window.nmChat(chatHost, () => (state.selected
        ? { ...state.selected, horse: state.horse || '' }
        : null))
    : null;

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
        const unit = st.files === 1 ? st.unit.replace(/s$/, '') : st.unit;
        el('g-stats').textContent =
          `${st.rows.toLocaleString()} rows · ${st.files.toLocaleString()} ${unit} · ${st.from} → ${st.to}`;
        buildHead();
        await reload();
        // the chat's own failure must not take the table down with it
        if (chatPanel) chatPanel.load();
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
    const settingsPanel = document.getElementById('panel-settings');

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
      if (ds.compact) panel.classList.add('compact');
      // The chat sits beside the table, not under it, so the table and the
      // panel share a flex row while the toolbar above and the pager below
      // stay full width.
      if (ds.chat && window.nmChat) {
        const ctpl = document.getElementById('chattpl');
        const wrap = panel.querySelector('.tablewrap');
        if (ctpl && wrap) {
          const row = document.createElement('div');
          row.className = 'with-chat';
          wrap.replaceWith(row);
          row.append(wrap, ctpl.content.cloneNode(true));
        }
      }
      if (ds.detail) {
        const dtpl = document.getElementById(ds.detail + 'tpl');
        if (dtpl) panel.appendChild(dtpl.content.cloneNode(true));
      }
      panels.insertBefore(panel, settingsPanel);   // Settings stays last

      grids.set(ds.key, createGrid(panel, ds));
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
