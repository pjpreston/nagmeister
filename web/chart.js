'use strict';

// The form graph under the race card: one horse's qualifying past races
// plotted against the date they were run.
//
// Small multiples rather than one plot with several lines. Place runs 1..30,
// £1 invest 0..500 and Winning Distance 0..240 lengths, so sharing a y axis
// would flatten whichever is smaller into a line along the bottom, and giving
// each its own axis on one plot invents a correlation that is not in the data.
// One stacked panel per metric, all on the same time axis, compares the shapes
// honestly and keeps every axis readable.
//
// Every panel is drawn in the one accent colour and titled with its metric:
// with a single series per panel the colour carries no information, so there is
// nothing for a legend to explain and no second hue to tell apart.
//
// Hand-written SVG, built through the DOM. There is no build step in this
// project and no chart library, and going through createElementNS/textContent
// rather than innerHTML keeps horse and track names out of the HTML parser.

(function () {
  const NS = 'http://www.w3.org/2000/svg';

  // panel geometry, in px
  const PANEL_H = 104;      // plot area of one metric
  const TITLE_H = 20;       // room above each plot for its heading
  const GAP = 16;           // between panels, so a title clears the plot above
  const PAD_L = 52;         // y-axis labels
  const PAD_R = 14;         // so the last marker is not clipped
  const AXIS_H = 22;        // date labels, on the bottom panel only
  const R = 4;              // marker radius: 8px across, the minimum that reads
  // keeps the first and last markers off the plot edges. Without it a point on
  // the boundary has its ring clipped by the axis and reads as half a dot.
  const X_INSET = 10;
  // vertical room inside the band, so a point at the series maximum is not
  // drawn touching the panel's top edge
  const Y_INSET = 8;

  function svg(name, attrs) {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  /** Tick values at round numbers spanning lo..hi, about `want` intervals.
   *
   * Starts at floor(lo/step), not ceil: the ticks have to bracket the data, or
   * a series whose range falls between two steps gets a single lonely label
   * and no sense of scale.
   */
  function ticks(lo, hi, want = 3) {
    if (!(hi > lo)) return [lo];
    const raw = (hi - lo) / want;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
    const out = [];
    const end = Math.ceil(hi / step) * step;
    for (let v = Math.floor(lo / step) * step; v <= end + step / 1e6; v += step) {
      out.push(Math.round(v * 1e6) / 1e6);
    }
    return out.length > 1 ? out : [lo, hi];
  }

  const fmt = (v) => (Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100));

  /** "2026-08-27" -> "27 Aug 26", for the date axis. */
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function shortDate(iso) {
    const [y, m, d] = iso.split('-');
    return `${+d} ${MONTHS[+m - 1]} ${y.slice(2)}`;
  }

  const BETTER = { lower: 'lower is better', higher: 'higher is better' };

  window.nmHorseChart = function mount(root) {
    let data = null;                 // last /api/horseform payload
    let chosen = new Set();          // metric names currently plotted
    let xs = [];                     // pixel x per point, for the crosshair

    const head = document.createElement('p');
    head.className = 'hf-head hint';
    const plot = document.createElement('div');
    plot.className = 'hf-plot';
    const boxes = document.createElement('div');
    boxes.className = 'hf-boxes';
    const tip = document.createElement('div');
    tip.className = 'hf-tip';
    tip.hidden = true;
    root.append(head, plot, boxes, tip);

    function buildBoxes() {
      boxes.textContent = '';
      if (!data) return;
      for (const m of data.metrics) {
        const label = document.createElement('label');
        label.className = 'hf-box';
        label.title = m.desc ? `${m.label}\n\n${m.desc}` : m.label;
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = chosen.has(m.name);
        cb.onchange = () => {
          if (cb.checked) chosen.add(m.name);
          else chosen.delete(m.name);
          draw();
        };
        label.append(cb, document.createTextNode(' ' + m.label));
        boxes.append(label);
      }
    }

    function draw() {
      plot.textContent = '';
      tip.hidden = true;
      if (!data) return;

      const pts = data.points;
      if (!pts.length) {
        const p = document.createElement('p');
        p.className = 'hint';
        p.textContent = 'No previous races at this race type and distance.';
        plot.append(p);
        return;
      }
      const show = data.metrics.filter((m) => chosen.has(m.name));
      if (!show.length) {
        const p = document.createElement('p');
        p.className = 'hint';
        p.textContent = 'Tick a metric below to plot it.';
        plot.append(p);
        return;
      }

      const width = Math.max(plot.clientWidth || root.clientWidth || 640, 320);
      const inner = width - PAD_L - PAD_R;
      const height = show.length * (TITLE_H + PANEL_H + GAP) + AXIS_H;
      const s = svg('svg', {
        width, height, viewBox: `0 0 ${width} ${height}`,
        class: 'hf-svg', role: 'img',
      });
      const title = svg('title');
      title.textContent =
        `${data.horse}: ${show.map((m) => m.label).join(', ')} over ${pts.length} races`;
      s.append(title);

      // x is real time, so a layoff shows as a gap rather than being closed up
      const t = pts.map((p) => Date.parse(p.date));
      const t0 = Math.min(...t), t1 = Math.max(...t);
      const span = inner - 2 * X_INSET;
      const xAt = (v) => (t1 === t0 ? PAD_L + inner / 2
                                    : PAD_L + X_INSET + ((v - t0) / (t1 - t0)) * span);
      xs = t.map(xAt);
      const lastIdx = pts.length - 1;

      show.forEach((m, panel) => {
        const top = panel * (TITLE_H + PANEL_H + GAP) + TITLE_H;
        const vals = pts.map((p) => p.values[m.name]);
        const known = vals.filter((v) => v !== null && v !== undefined);

        const g = svg('g');
        s.append(g);

        // heading: the metric name is the panel's identity, so no legend
        const h = svg('text', { x: PAD_L, y: top - 7, class: 'hf-title' });
        h.textContent = m.label;
        g.append(h);
        if (BETTER[m.better]) {
          // right-anchored, so it never collides with the metric name however
          // long that is -- no text measuring needed
          const hint = svg('text', {
            x: width - PAD_R, y: top - 7, class: 'hf-sub', 'text-anchor': 'end',
          });
          hint.textContent = BETTER[m.better];
          g.append(hint);
        }

        if (!known.length) {
          const none = svg('text', { x: PAD_L, y: top + PANEL_H / 2, class: 'hf-sub' });
          none.textContent = 'not recorded for these races';
          g.append(none);
          return;
        }

        let lo = Math.min(...known), hi = Math.max(...known);
        if (lo === hi) { lo -= 1; hi += 1; }          // a flat series still needs a band
        const tv = ticks(lo, hi);
        lo = Math.min(lo, ...tv); hi = Math.max(hi, ...tv);
        // the band is inset top and bottom, so a point at the series max or min
        // sits inside the panel rather than on its edge
        const plotH = PANEL_H - 2 * Y_INSET;
        const yAt = (v) => top + Y_INSET + plotH - ((v - lo) / (hi - lo)) * plotH;

        // hairline solid gridlines, one step off the surface
        for (const v of tv) {
          const y = yAt(v);
          g.append(svg('line', {
            x1: PAD_L, x2: width - PAD_R, y1: y, y2: y, class: 'hf-grid',
          }));
          // right-anchored against the axis, or a three-digit tick like "100"
          // runs rightwards into the plot
          const lab = svg('text', {
            x: PAD_L - 8, y: y + 4, class: 'hf-ytick', 'text-anchor': 'end',
          });
          lab.textContent = fmt(v);
          g.append(lab);
        }

        // the line, broken wherever a value is missing rather than interpolated
        let run = [];
        const flush = () => {
          if (run.length > 1) {
            g.append(svg('path', {
              d: 'M' + run.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join('L'),
              class: 'hf-line',
            }));
          }
          run = [];
        };
        vals.forEach((v, i) => {
          if (v === null || v === undefined) flush();
          else run.push([xs[i], yAt(v)]);
        });
        flush();

        // a left rule, so the series reads against an axis rather than floating
        g.append(svg('line', {
          x1: PAD_L, x2: PAD_L, y1: top, y2: top + PANEL_H, class: 'hf-axis',
        }));

        vals.forEach((v, i) => {
          if (v === null || v === undefined) return;
          // the most recent run is the one being asked about, so it gets a
          // larger marker and the value beside it -- selective direct labelling
          // rather than a number on every point
          const latest = i === lastIdx;
          g.append(svg('circle', {
            cx: xs[i], cy: yAt(v), r: latest ? R + 1.5 : R,
            class: latest ? 'hf-dot hf-dot-last' : 'hf-dot',
          }));
          if (latest) {
            const lab = svg('text', {
              x: xs[i] - R - 5, y: yAt(v) + 4, class: 'hf-last', 'text-anchor': 'end',
            });
            lab.textContent = m.name === 'place' && pts[i].place_text !== null
              ? pts[i].place_text : fmt(v);
            g.append(lab);
          }
        });
      });

      // Date axis, once, under the bottom panel.
      //
      // Labels are chosen by how far apart they land in pixels, not by taking
      // every nth race: the x scale is real time, so races cluster and two
      // consecutive points can be a day apart. Stepping by index collides.
      const axisY = show.length * (TITLE_H + PANEL_H + GAP) + 12;
      const MIN_GAP = 82;
      const picked = [];
      xs.forEach((x, i) => {
        if (!picked.length || x - xs[picked[picked.length - 1]] >= MIN_GAP) picked.push(i);
      });
      const last = pts.length - 1;
      if (picked[picked.length - 1] !== last) {
        // the most recent run is the one worth naming, so make room for it
        while (picked.length && xs[last] - xs[picked[picked.length - 1]] < MIN_GAP) picked.pop();
        picked.push(last);
      }
      for (const i of picked) {
        const x = xs[i];
        // clamp the ends inward so a centred label cannot overhang the plot
        const anchor = x < PAD_L + 30 ? 'start' : x > width - PAD_R - 30 ? 'end' : 'middle';
        const lab = svg('text', { x, y: axisY, class: 'hf-xtick', 'text-anchor': anchor });
        lab.textContent = shortDate(pts[i].date);
        s.append(lab);
      }

      // crosshair + one readout listing every plotted metric at that race
      const rule = svg('line', {
        x1: 0, x2: 0, y1: TITLE_H - 6,
        y2: show.length * (TITLE_H + PANEL_H + GAP) - GAP,
        class: 'hf-rule',
      });
      rule.setAttribute('visibility', 'hidden');
      s.append(rule);

      const nearest = (px) => {
        let best = 0, d = Infinity;
        xs.forEach((x, i) => { const dd = Math.abs(x - px); if (dd < d) { d = dd; best = i; } });
        return best;
      };

      s.onpointermove = (ev) => {
        const box = s.getBoundingClientRect();
        const i = nearest(ev.clientX - box.left);
        rule.setAttribute('x1', xs[i]); rule.setAttribute('x2', xs[i]);
        rule.setAttribute('visibility', 'visible');
        const p = pts[i];
        tip.textContent = '';
        const when = document.createElement('div');
        when.className = 'hf-tip-head';
        when.textContent = `${shortDate(p.date)} · ${p.track}`;
        tip.append(when);
        for (const m of show) {
          const v = p.values[m.name];
          const row = document.createElement('div');
          row.className = 'hf-tip-row';
          const key = document.createElement('span');
          key.className = 'hf-tip-key';
          const val = document.createElement('strong');
          // Place shows its own text, so a run that was pulled up reads 'PU'
          // rather than an empty cell
          val.textContent = m.name === 'place' && p.place_text !== null
            ? p.place_text
            : (v === null || v === undefined ? '—' : fmt(v));
          const name = document.createElement('span');
          name.textContent = m.label;
          row.append(key, val, name);
          tip.append(row);
        }
        tip.hidden = false;
        const host = root.getBoundingClientRect();
        const left = Math.min(ev.clientX - host.left + 14, host.width - 190);
        tip.style.left = `${Math.max(4, left)}px`;
        tip.style.top = `${ev.clientY - host.top + 14}px`;
      };
      s.onpointerleave = () => {
        rule.setAttribute('visibility', 'hidden');
        tip.hidden = true;
      };

      plot.append(s);
    }

    // the svg is sized in px for the crosshair maths, so redraw on resize
    if (window.ResizeObserver) {
      let w = 0;
      new ResizeObserver(() => {
        const now = plot.clientWidth;
        if (now && Math.abs(now - w) > 8) { w = now; draw(); }
      }).observe(plot);
    }

    return {
      show(payload) {
        data = payload;
        if (!chosen.size) chosen = new Set([payload.default]);
        head.textContent =
          `${payload.horse} — ${payload.points.length} previous race(s) at this type and distance`;
        buildBoxes();
        draw();
      },
      clear() {
        data = null;
        head.textContent = '';
        plot.textContent = '';
        boxes.textContent = '';
        tip.hidden = true;
      },
    };
  };
})();
