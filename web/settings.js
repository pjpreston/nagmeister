'use strict';

// Tabs, themes and the table font.
//
// Preferences live in localStorage under one key, and a tiny inline script in
// <head> applies them before first paint so the default theme never flashes.
// This file re-applies the same values, renders the pickers, and saves changes.
//
// Everything is client-side on purpose: these are per-browser display choices,
// and the DuckDB file the server opens is read-only, so there is nowhere on the
// server to write them without adding a second writable store.

const PREFS_KEY = 'nagmeister.prefs';

const THEMES = [
  { id: 'auto',            name: 'Auto',            bg: '#ffffff', fg: '#1b1f24', accent: '#199063', dark: '#14171a' },
  { id: 'light',           name: 'Light',           bg: '#ffffff', fg: '#1b1f24', accent: '#1f6feb' },
  { id: 'dark',            name: 'Dark',            bg: '#14171a', fg: '#e6e9ec', accent: '#58a6ff' },
  { id: 'turf',            name: 'Turf',            bg: '#f4fbf7', fg: '#14291f', accent: '#199063' },
  { id: 'midnight',        name: 'Midnight',        bg: '#0b1020', fg: '#dfe6f5', accent: '#7aa2ff' },
  { id: 'slate',           name: 'Slate',           bg: '#1e2227', fg: '#dde3ea', accent: '#79b8ff' },
  { id: 'nord',            name: 'Nord',            bg: '#2e3440', fg: '#e5e9f0', accent: '#88c0d0' },
  { id: 'solarized-light', name: 'Solarized Light', bg: '#fdf6e3', fg: '#35434a', accent: '#268bd2' },
  { id: 'solarized-dark',  name: 'Solarized Dark',  bg: '#002b36', fg: '#d6e2e4', accent: '#268bd2' },
  { id: 'sepia',           name: 'Sepia',           bg: '#f6efe3', fg: '#3a3025', accent: '#9a6b3f' },
  { id: 'rose',            name: 'Rose',            bg: '#fff5f7', fg: '#3d2229', accent: '#c2185b' },
  { id: 'mono',            name: 'Mono',            bg: '#ffffff', fg: '#111111', accent: '#333333' },
  { id: 'contrast',        name: 'High Contrast',   bg: '#000000', fg: '#ffffff', accent: '#ffe600' },
];

// Only stacks that resolve without downloading anything, so the table never
// reflows late or falls back to something unintended.
const FONTS = [
  { name: 'System (default)',   stack: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif' },
  { name: 'Helvetica / Arial',  stack: 'Helvetica, Arial, sans-serif' },
  { name: 'Verdana',            stack: 'Verdana, Geneva, sans-serif' },
  { name: 'Trebuchet MS',       stack: '"Trebuchet MS", Tahoma, sans-serif' },
  { name: 'Georgia',            stack: 'Georgia, "Times New Roman", serif' },
  { name: 'Times',              stack: '"Times New Roman", Times, serif' },
  { name: 'Palatino',           stack: 'Palatino, "Palatino Linotype", "Book Antiqua", serif' },
  { name: 'System monospace',   stack: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
  { name: 'Courier New',        stack: '"Courier New", Courier, monospace' },
];

const DEFAULTS = { theme: 'auto', font: FONTS[0].stack, tabular: true };

function loadPrefs() {
  try {
    return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'));
  } catch (e) {
    return Object.assign({}, DEFAULTS);
  }
}

function savePrefs(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    return true;
  } catch (e) {
    // private browsing, or storage full: the choice still applies for this
    // session, it just will not survive a reload. Say so rather than fail mute.
    toast('Could not save — preference applies for this session only', true);
    return false;
  }
}

let prefs = loadPrefs();

function apply() {
  const root = document.documentElement;
  root.dataset.theme = prefs.theme;
  root.style.setProperty('--table-font', prefs.font);
  root.style.setProperty('--table-numeric', prefs.tabular ? 'tabular-nums' : 'normal');
  const meta = document.querySelector('meta[name="theme-color"]');
  const t = THEMES.find((x) => x.id === prefs.theme);
  if (meta && t) meta.setAttribute('content', t.bg);
}

let toastTimer;
function toast(msg, sticky) {
  const el = document.getElementById('savedmsg');
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => { el.hidden = true; }, 1400);
}

function update(patch) {
  prefs = Object.assign({}, prefs, patch);
  apply();
  if (savePrefs(prefs)) toast('Saved');
  renderThemes();
}

// ------------------------------------------------------------------ pickers

function renderThemes() {
  const host = document.getElementById('themes');
  if (!host) return;
  host.innerHTML = '';
  for (const t of THEMES) {
    const b = document.createElement('button');
    b.className = 'swatch';
    b.type = 'button';
    b.setAttribute('aria-pressed', String(prefs.theme === t.id));
    b.title = `${t.name} theme`;
    // A miniature of the app: header bar, a row, and the accent.
    b.innerHTML =
      `<span class="chip" style="background:${t.bg}">
         <i class="bar"  style="background:${t.fg};opacity:.82"></i>
         <i class="bar2" style="background:${t.fg};opacity:.4"></i>
         <i class="dot"  style="background:${t.accent}"></i>
       </span>
       <span class="name">${t.name}</span>`;
    if (t.id === 'auto') {
      // show auto as split light/dark so it reads as "follows the system"
      b.querySelector('.chip').style.background =
        `linear-gradient(105deg, ${t.bg} 0 50%, ${t.dark} 50% 100%)`;
    }
    b.onclick = () => update({ theme: t.id });
    host.appendChild(b);
  }
}

function renderFonts() {
  const sel = document.getElementById('fontpick');
  if (!sel) return;
  sel.innerHTML = '';
  for (const f of FONTS) {
    const o = document.createElement('option');
    o.value = f.stack;
    o.textContent = f.name;
    o.style.fontFamily = f.stack;
    o.selected = f.stack === prefs.font;
    sel.appendChild(o);
  }
  sel.onchange = () => update({ font: sel.value });

  const tab = document.getElementById('tabular');
  if (tab) {
    tab.checked = !!prefs.tabular;
    tab.onchange = () => update({ tabular: tab.checked });
  }
}

// --------------------------------------------------------------------- tabs

/** Tab names are read from the DOM, because app.js adds one per dataset at
 *  runtime -- a hardcoded list here would silently drop any new table. */
function knownTabs() {
  return [...document.querySelectorAll('.tabs button[data-tab]')].map((b) => b.dataset.tab);
}

function showTab(name, push) {
  const known = knownTabs();
  if (!known.includes(name)) name = known[0] || 'settings';
  for (const id of known) {
    const tab = document.getElementById('tab-' + id);
    const panel = document.getElementById('panel-' + id);
    const on = id === name;
    if (tab) tab.setAttribute('aria-selected', String(on));
    if (panel) panel.hidden = !on;
  }
  if (push && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  const label = document.getElementById('tab-' + name);
  document.title = label ? `NagMeister — ${label.textContent.trim()}` : 'NagMeister';
  // a grid only queries its table once it is actually shown
  if (typeof window.nmOnTabShown === 'function') window.nmOnTabShown(name);
}
window.nmShowTab = showTab;

function initTabs() {
  // delegated, so tabs added later by app.js work without re-wiring
  const nav = document.querySelector('.tabs');
  if (nav) {
    nav.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-tab]');
      if (btn) showTab(btn.dataset.tab, true);
    });
  }
  // deep-linkable, and the back button behaves
  addEventListener('hashchange', () => showTab(location.hash.slice(1), false));
}

apply();
renderThemes();
renderFonts();
initTabs();

const reset = document.getElementById('resetprefs');
if (reset) reset.onclick = () => { prefs = Object.assign({}, DEFAULTS); apply(); savePrefs(prefs);
                                   renderThemes(); renderFonts(); toast('Reset'); };
