'use strict';

// The AI panel beside the Races table.
//
// It owns the model dropdown, the transcript and the box you type in. It does
// not own an API key: the page posts a question to /api/chat and gets prose
// back, and the keys live in the server process only. Nothing here knows which
// vendor answered beyond the label in the dropdown.
//
// The whole thread goes up on every turn, because none of the vendor APIs are
// stateful. That is also why Clear is a real feature rather than a nicety --
// an old thread is re-sent, and re-billed, on every question.
//
// Built through the DOM rather than innerHTML: a model's reply is untrusted
// text, and it is about to be put on the page.

(function () {
  const MAX_TURNS = 40;   // matches the server's cap, so we fail here not there

  // `getContext` is supplied by the grid: it returns what the user has
  // selected on the tab, so a question can say "this race" and mean it.
  window.nmChat = function mount(root, getContext) {
    const pick = root.querySelector('.chat-model');
    const log = root.querySelector('.chat-log');
    const form = root.querySelector('.chat-form');
    const input = root.querySelector('.chat-in');
    const send = root.querySelector('.chat-send');
    const reset = root.querySelector('.chat-reset');
    const note = root.querySelector('.chat-note');

    let history = [];       // [{role, content}], what gets posted
    let busy = false;
    let models = [];

    // Shown above the transcript, because the model is about to assume it and
    // the user should be able to see what "this race" resolves to.
    const ctxLine = document.createElement('div');
    ctxLine.className = 'chat-ctx';
    root.querySelector('.chat-log').before(ctxLine);

    function context() {
      return (getContext ? getContext() : null) || {};
    }

    function showContext() {
      const c = context();
      ctxLine.textContent = c.date
        ? `About: ${c.track} ${c.time} · ${c.date}` + (c.horse ? ` · ${c.horse}` : '')
        : 'No race selected — pick one above, or name one in your question.';
      ctxLine.classList.toggle('chat-ctx-none', !c.date);
    }

    function say(role, text, meta) {
      const wrap = document.createElement('div');
      wrap.className = 'chat-msg chat-' + role;
      const who = document.createElement('div');
      who.className = 'chat-who';
      who.textContent = role === 'user' ? 'You'
        : role === 'error' ? 'Error'
        : (models.find((m) => m.key === pick.value) || {}).label || 'AI';
      const body = document.createElement('div');
      body.className = 'chat-text';
      // paragraph per blank line; the models are asked for short prose, and
      // this keeps their line breaks without interpreting any markup
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
      return wrap;
    }

    function setBusy(on, label) {
      busy = on;
      send.disabled = on;
      input.disabled = on;
      send.textContent = on ? 'Thinking…' : 'Ask';
      note.textContent = on ? (label || '') : '';
    }

    /** The selected model's key is missing, so say which one and stop. */
    function readiness() {
      const m = models.find((x) => x.key === pick.value);
      if (m && !m.ready) {
        note.textContent = `needs ${m.env}`;
        return false;
      }
      note.textContent = '';
      return true;
    }

    async function ask(question) {
      history.push({ role: 'user', content: question });
      say('user', question);
      setBusy(true, 'reading the database…');
      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: pick.value, messages: history,
                                 context: context() }),
        });
        const body = await res.json().catch(() => ({ error: res.statusText }));
        if (!res.ok) throw new Error(body.error || 'request failed');
        history.push({ role: 'assistant', content: body.reply });
        // Two provenance lines, because they answer different questions: which
        // of our tables it read, and whether it went outside them. Web search
        // runs on the vendor's side, so without this the reader cannot tell a
        // database-only answer from one that also checked the market.
        const used = [...new Set(body.tools_used || [])];
        const web = [...new Set(body.sources || [])];
        const meta = [
          used.length ? 'read: ' + used.join(', ') : null,
          web.length ? 'web: ' + web.join(', ') : null,
        ].filter(Boolean).join('  ·  ');
        say('assistant', body.reply, meta || null);
      } catch (e) {
        // the failed turn is dropped, so a retry does not resend it
        history.pop();
        say('error', e.message);
      } finally {
        setBusy(false);
        readiness();
      }
    }

    form.onsubmit = (ev) => {
      ev.preventDefault();
      const q = input.value.trim();
      if (!q || busy) return;
      if (!readiness()) return;
      if (history.length >= MAX_TURNS) {
        say('error', 'This conversation is long enough to be expensive to re-send. '
                   + 'Clear it and start again.');
        return;
      }
      input.value = '';
      ask(q);
    };

    // Enter sends, Shift+Enter for a newline -- the box is multi-line because
    // a form question can be long, but sending is the common case
    input.onkeydown = (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        form.requestSubmit();
      }
    };

    reset.onclick = () => {
      history = [];
      log.textContent = '';
      readiness();
      showContext();
      input.focus();
    };

    pick.onchange = readiness;

    return {
      /** Called by the grid whenever the selected race or runner changes. */
      refresh: showContext,
      async load() {
        try {
          const d = await getJSON('/api/models');
          models = d.models;
          pick.textContent = '';
          for (const m of models) {
            const o = document.createElement('option');
            o.value = m.key;
            // the model id is in the tooltip, not the label: the label is the
            // ticket's wording and should stay stable as ids move on
            o.textContent = m.label + (m.ready ? '' : ' (no key)');
            o.title = `${m.label} — ${m.model}\nNeeds ${m.env}`;
            pick.append(o);
          }
          pick.value = d.default;
          readiness();
          showContext();
        } catch (e) {
          note.textContent = e.message;
        }
      },
    };
  };
})();
