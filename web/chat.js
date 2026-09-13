'use strict';

// The AI panel beside the Races table: a tab per configured model, each with
// its own conversation.
//
// The conversations are genuinely separate. Asking Gemini something does not
// put it in Claude's history, so the tabs are four independent readings of the
// same race rather than one thread passed between models -- which is the point
// of having four, and also the only honest arrangement, since none of these
// vendors will accept another's reasoning blocks in a history anyway.
//
// The panel does not own an API key: it posts a question to /api/chat and gets
// prose back. Keys live in the server process only.
//
// The whole thread goes up on every turn, because none of the vendor APIs are
// stateful. That is also why Clear is a real feature rather than a nicety --
// an old thread is re-sent, and re-billed, on every question.
//
// Built through the DOM rather than innerHTML: a model's reply is untrusted
// text, and it is about to be put on the page.

(function () {
  const MAX_TURNS = 40;   // matches the server's cap, so we fail here not there

  window.nmChat = function mount(root, getContext) {
    const ctxLine = root.querySelector('.chat-ctx');
    const tabs = root.querySelector('.chat-tabs');
    const panes = root.querySelector('.chat-panes');
    let models = [];
    const convos = new Map();   // model key -> conversation

    function context() {
      return (getContext ? getContext() : null) || {};
    }

    // Shown once, above the tabs: it is the same race for every model, and
    // four copies of it would just cost height the table has not got.
    function showContext() {
      const c = context();
      ctxLine.textContent = c.date
        ? `About: ${c.track} ${c.time} · ${c.date}` + (c.horse ? ` · ${c.horse}` : '')
        : 'No race selected — pick one above, or name one in your question.';
      ctxLine.classList.toggle('chat-ctx-none', !c.date);
    }

    /** One model's tab, transcript and question box. */
    function makeConvo(model) {
      const pane = document.createElement('div');
      pane.className = 'chat-pane';
      pane.id = `chat-pane-${model.key.replace(/\W/g, '-')}`;
      pane.setAttribute('role', 'tabpanel');
      pane.hidden = true;

      const log = document.createElement('div');
      log.className = 'chat-log';
      log.setAttribute('role', 'log');
      log.setAttribute('aria-live', 'polite');

      const form = document.createElement('form');
      form.className = 'chat-form';
      const input = document.createElement('textarea');
      input.className = 'chat-in';
      input.rows = 2;
      input.spellcheck = false;
      input.placeholder = model.ready
        ? 'Ask about this race…'
        : `Set ${model.env} to use ${model.label}`;
      const bar = document.createElement('div');
      bar.className = 'chat-bar';
      const send = document.createElement('button');
      send.type = 'submit';
      send.className = 'chat-send';
      send.textContent = 'Ask';
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'chat-reset ghost';
      clear.title = 'Clear this conversation';
      clear.textContent = 'Clear';
      const note = document.createElement('span');
      note.className = 'chat-note hint';
      bar.append(send, clear, note);
      form.append(input, bar);
      pane.append(log, form);
      panes.append(pane);

      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'chat-tab';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', 'false');
      tab.setAttribute('aria-controls', pane.id);
      tab.textContent = model.label;
      tab.title = `${model.label} — ${model.model}\nNeeds ${model.env}`
        + (model.ready ? '' : '\n\nNo key set');
      if (!model.ready) tab.classList.add('chat-tab-nokey');
      tab.onclick = () => select(model.key);
      tabs.append(tab);

      let history = [];
      let busy = false;

      function say(role, text, meta) {
        const wrap = document.createElement('div');
        wrap.className = 'chat-msg chat-' + role;
        const who = document.createElement('div');
        who.className = 'chat-who';
        who.textContent = role === 'user' ? 'You'
          : role === 'error' ? 'Error' : model.label;
        const body = document.createElement('div');
        body.className = 'chat-text';
        for (const para of text.split(/\n{2,}/)) {
          const p = document.createElement('p');
          p.textContent = para.replace(/\n/g, ' ');
          body.append(p);
        }
        wrap.append(who, body);
        if (meta) {
          const m = document.createElement('div');
          m.className = 'chat-meta';
          m.textContent = meta;
          wrap.append(m);
        }
        log.append(wrap);
        log.scrollTop = log.scrollHeight;
      }

      function setBusy(on) {
        busy = on;
        send.disabled = on || !model.ready;
        input.disabled = on || !model.ready;
        send.textContent = on ? 'Thinking…' : 'Ask';
        note.textContent = on ? 'reading the data and the web…'
                              : (model.ready ? '' : `needs ${model.env}`);
        // a tab whose answer is still coming says so, since you can switch away
        tab.classList.toggle('chat-tab-busy', on);
      }

      async function ask(question) {
        history.push({ role: 'user', content: question });
        say('user', question);
        setBusy(true);
        try {
          const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: model.key, messages: history,
                                   context: context() }),
          });
          const body = await res.json().catch(() => ({ error: res.statusText }));
          if (!res.ok) throw new Error(body.error || 'request failed');
          history.push({ role: 'assistant', content: body.reply });
          // two provenance lines: which of our tables it read, and whether it
          // went outside them. Web search runs vendor-side, so without this a
          // database-only answer looks identical to one that checked the market
          const used = [...new Set(body.tools_used || [])];
          const web = [...new Set(body.sources || [])];
          say('assistant', body.reply, [
            used.length ? 'read: ' + used.join(', ') : null,
            web.length ? 'web: ' + web.join(', ') : null,
          ].filter(Boolean).join('  ·  ') || null);
        } catch (e) {
          history.pop();      // drop the failed turn so a retry does not resend it
          say('error', e.message);
        } finally {
          setBusy(false);
        }
      }

      form.onsubmit = (ev) => {
        ev.preventDefault();
        const q = input.value.trim();
        if (!q || busy || !model.ready) return;
        if (history.length >= MAX_TURNS) {
          say('error', 'This conversation is long enough to be expensive to '
                     + 're-send. Clear it and start again.');
          return;
        }
        input.value = '';
        ask(q);
      };

      // Enter sends, Shift+Enter for a newline
      input.onkeydown = (ev) => {
        if (ev.key === 'Enter' && !ev.shiftKey) {
          ev.preventDefault();
          form.requestSubmit();
        }
      };

      clear.onclick = () => { history = []; log.textContent = ''; input.focus(); };

      setBusy(false);
      return { model, tab, pane, input, focus: () => input.focus() };
    }

    function select(key) {
      for (const [k, c] of convos) {
        const on = k === key;
        c.pane.hidden = !on;
        c.tab.setAttribute('aria-selected', on ? 'true' : 'false');
      }
      const c = convos.get(key);
      if (c && c.model.ready) c.focus();
    }

    return {
      /** Called by the grid whenever the selected race or runner changes. */
      refresh: showContext,
      async load() {
        try {
          const d = await getJSON('/api/models');
          models = d.models;
          tabs.textContent = '';
          panes.textContent = '';
          convos.clear();
          for (const m of models) convos.set(m.key, makeConvo(m));
          // open on the configured default, or the first model with a key
          const first = convos.has(d.default) ? d.default
            : (models.find((m) => m.ready) || models[0] || {}).key;
          if (first) select(first);
          showContext();
        } catch (e) {
          ctxLine.textContent = e.message;
        }
      },
    };
  };
})();
