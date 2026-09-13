#!/usr/bin/env python3
"""The AI chat behind the Races tab: one question, one answer, three vendors.

The user picks a model, types a question, and the model answers it using the
racing database and the web. Everything here runs server-side, because the API
keys do -- see "Keys" below.

Three vendors, three protocols. Every shape below was confirmed against the
live APIs rather than recalled, because they differ in ways that matter:

    Anthropic   official SDK. tools take `input_schema`; a call comes back as
                a tool_use block and goes back as a tool_result block. Web
                search is Anthropic-hosted: declare web_search_20260209 and
                the results arrive in the same response.
    ChatGPT     REST, and it has to be the *Responses* API -- Chat Completions
                rejects web search outright ("Unknown parameter:
                'web_search_options'"), and the ticket asks for the web. Tools
                come back as function_call and go back as function_call_output
                keyed by call_id.
    Gemini      REST. functionDeclarations in, functionCall out,
                functionResponse back. Mixing its google_search with function
                declarations needs tool_config.include_server_side_tool_
                invocations, or the request is refused outright.

Keys come from the environment -- ANTHROPIC_API_KEY, OPENAI_API_KEY and
GOOGLE_API_KEY -- and never leave this process. The browser posts a question to
rbd_web and gets prose back; it is never given a key, and no request is made
from the page to a vendor. A missing key is reported as a missing key rather
than a failed request, because that is the one error a user can actually fix.

The tools are deliberately narrower than /api/horse/*. Those endpoints exist
for a programmatic caller and return every column; a busy jumper's form book is
400 rows of 78 fields, which would crowd out the conversation it is supposed to
inform. The tools here return the columns a form question actually turns on.
"""

import json
import os

from _venv import use_venv

use_venv()  # must precede the third-party imports below

import requests  # noqa: E402

# ------------------------------------------------------------------ models ---
#
# The dropdown, in the ticket's order. "Anthropic/Claude" and
# "Anthropic/Sonnet" are the two Anthropic tiers -- Opus is the flagship, so
# that is what the unqualified "Claude" entry points at.
#
# The OpenAI and Gemini ids were read from each vendor's own models endpoint
# with these keys, not guessed: gpt-5.5 was the newest general gpt-5.x, and
# gemini-pro-latest is Google's own alias for the current Pro, so it does not
# go stale the way a pinned preview id would.
MODELS = [
    ("anthropic/claude", "Anthropic/Claude", "anthropic", "claude-opus-5"),
    ("anthropic/sonnet", "Anthropic/Sonnet", "anthropic", "claude-sonnet-5"),
    ("openai/chatgpt", "ChatGPT", "openai", "gpt-5.5"),
    ("google/gemini", "Gemini", "google", "gemini-pro-latest"),
]
DEFAULT_MODEL = "anthropic/sonnet"   # the ticket's default

BY_KEY = {k: (k, label, vendor, mid) for k, label, vendor, mid in MODELS}

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}

MAX_STEPS = 8          # tool round trips before we stop; a runaway loop is billable
# Total tool calls allowed for one question. MAX_STEPS alone is not a budget:
# every vendor can emit many calls in a single round trip, and asked an open
# question ChatGPT fetched all 24 race cards on a day inside two of them. Each
# card is thousands of tokens of JSON, so the bill is set by calls, not steps.
MAX_CALLS = 20
MAX_TOKENS = 8000
HTTP_TIMEOUT = 180     # a search-and-reason turn is slow; this is not a page load


class ChatError(Exception):
    """Something the user should be told in prose rather than a stack trace."""


def _domain(url):
    """example.com from a URL, or "" -- for naming a source compactly."""
    if not isinstance(url, str):
        return ""
    rest = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return rest[4:] if rest.startswith("www.") else rest


# ------------------------------------------------------------------- tools ---
#
# One definition per tool, in a neutral shape, translated per vendor below.
# Keeping them in one list is what stops the three providers being offered
# different tools by accident.

TOOLS = [
    {
        "name": "list_card_dates",
        "description": (
            "List the race days held in the database, newest first, with how "
            "many races each day has. Start here when the user does not say "
            "which day they mean."
        ),
        "properties": {},
        "required": [],
    },
    {
        "name": "list_races",
        "description": (
            "List the races on one day: track, off time, race type and "
            "distance. Use the track and time from here to call get_race_card."
        ),
        "properties": {"date": {"type": "string",
                                "description": "Race day as YYYY-MM-DD"}},
        "required": ["date"],
    },
    {
        "name": "get_race_card",
        "description": (
            "The runners in one race, in market order, each with its record "
            "over this race's own race type and distance: number of previous "
            "such races, wins, last/average/median finishing position, the "
            "return from backing it with GBP 1 each time, and how far behind "
            "the winner it has finished. This is the tool for questions about "
            "who is likely to win a specific race."
        ),
        "properties": {
            "date": {"type": "string", "description": "Race day as YYYY-MM-DD"},
            "track": {"type": "string", "description": "Course, e.g. DONCASTER"},
            "time": {"type": "string", "description": "Off time as HH:MM"},
        },
        "required": ["date", "track", "time"],
    },
    {
        "name": "get_horse_form",
        "description": (
            "One horse's past runs: date, course, race type, distance, "
            "finishing position, starting price, field size and how far behind "
            "the winner it finished. Newest first."
        ),
        "properties": {
            "horse": {"type": "string", "description": "Horse's name"},
            "limit": {"type": "integer",
                      "description": "How many runs to return, default 40"},
        },
        "required": ["horse"],
    },
    {
        "name": "get_horse_results",
        "description": (
            "One horse's results as recorded after racing: finishing position, "
            "starting price, the winning time of the race, and what GBP 1 to "
            "win returned. Complements get_horse_form, which is the pre-race "
            "form book."
        ),
        "properties": {
            "horse": {"type": "string", "description": "Horse's name"},
            "limit": {"type": "integer",
                      "description": "How many runs to return, default 40"},
        },
        "required": ["horse"],
    },
]

# The columns each form tool returns. Narrow on purpose -- see the module
# docstring. These are the fields a "will it win" question turns on.
FORM_FIELDS = ["race_date", "track", "race_type", "distance", "place",
               "industry_sp", "runners", "winning_distance", "official_rating"]
RESULT_FIELDS = ["race_date", "track_name", "distance", "place", "ind_sp",
                 "runners", "winning_time", "win_dist_len", "one_pnd_win"]


def _rows(web, sql, params=()):
    cur = web.db()
    cur.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _slim(web, payload, fields, limit, dedupe=False):
    """Keep only `fields` from an /api/horse/* style payload.

    `dedupe` collapses a prerace_form answer to one row per race. A row there
    is one past run, held once per card the horse was declared on, so the raw
    count is several times the number of races it has run -- a model told a
    horse has 1,332 runs will reason from 1,332.
    """
    if not payload.get("found"):
        return {"found": False, "suggestions": payload.get("suggestions", [])}
    rows, seen = [], set()
    for r in payload["rows"]:
        if dedupe:
            race = (r.get("race_date"), r.get("track"), r.get("race_time"))
            if race in seen:
                continue
            seen.add(race)
        rows.append({f: web.jsonable(r.get(f)) for f in fields})
    rows.reverse()                                   # newest first, for a reader
    return {"found": True, "horse": payload["horse"], "races_held": len(rows),
            "returned": min(len(rows), limit), "runs": rows[:limit]}


def run_tool(web, name, args):
    """Execute one tool call. Returns something JSON-serialisable.

    `web` is the live rbd_web module, handed in by the caller rather than
    imported here. rbd_web runs as __main__ under `python rbd_web.py`, so an
    `import rbd_web` in this file would build a *second* module object with its
    own globals -- including a `_db_path` still on the default -- and every
    query here would silently read a different database than the tabs do.
    """
    if name == "list_card_dates":
        return {"days": _rows(web,
            f"SELECT race_date, count(*) AS races FROM {web.RACES_TABLE}"
            " GROUP BY 1 ORDER BY 1 DESC LIMIT 60")}

    if name == "list_races":
        day = str(args.get("date", "")).strip()
        return {"date": day, "races": _rows(web,
            "SELECT track, race_time, race_type, distance"
            f" FROM {web.RACES_TABLE} WHERE race_date = TRY_CAST(? AS DATE)"
            " ORDER BY race_time, track", [day])}

    if name == "get_race_card":
        card = web.race_card(str(args.get("date", "")).strip(),
                             str(args.get("track", "")).strip(),
                             str(args.get("time", "")).strip())
        names = [c["name"] for c in card["columns"]]
        return {"race": card["race"],
                "runners": [dict(zip(names, r)) for r in card["rows"]]}

    if name in ("get_horse_form", "get_horse_results"):
        horse = str(args.get("horse", "")).strip()
        limit = min(int(args.get("limit") or 40), 100)
        form = name == "get_horse_form"
        ds = web.DATASETS["form" if form else "results"]
        fields = FORM_FIELDS if form else RESULT_FIELDS
        return _slim(web, web.horse_rows(ds, horse, web.AGENT_MAX, 0),
                     fields, limit, dedupe=form)

    raise ChatError(f"unknown tool {name!r}")


SYSTEM = """You are a horse racing analyst built into NagMeister, a tool over a \
database of British and Irish racing: pre-race form cards and after-the-fact \
results.

Answer from the tools, not from memory. The database is the only thing that \
knows what this user actually holds, and it is the point of the tool. Call \
list_card_dates first if the user has not said which day they mean -- the data \
covers particular days, not all of racing.

When asked who will win a race, call get_race_card for that race and reason \
from what it returns: the runners' records at this exact race type and \
distance, their strike rate, their finishing positions, and what backing them \
has returned. Say which runners the numbers favour and why, in terms a reader \
can check against the card in front of them.

Be straight about the limits of what you looked at. A horse with two previous \
runs at the trip is a smaller sample than one with thirty, and saying so is \
more useful than a confident number. If the data does not support an answer, \
say what is missing rather than filling the gap.

Search the web as well, and do it before you commit to a selection rather than \
only when the database comes up short. The database is a form book: it knows \
what has happened, and nothing about what has changed since. Look for the \
things that would move your answer -- the current market at the bookmakers \
(Betfair, William Hill, Ladbrokes), non-runners and withdrawals, going and \
ground changes, jockey bookings, stable news, and what other analysts are \
saying about the race.

Then weigh the two against each other rather than reporting them separately. \
Say where the record and the market agree, and say so plainly where they \
disagree -- a horse the figures like but the market has drifted is a more \
useful observation than either fact alone, and so is the reverse. If the web \
adds nothing you could find, say that too.

Name the sources you used, with the site, so a reader can go and check.

Racing is not predictable and nothing here is a guarantee or financial advice. \
Say so once if you are asked for a selection; do not repeat it in every reply.

Format for a terminal-width panel: short paragraphs, no tables wider than a \
phrase, no headings for a two-line answer."""


# Appended to the system prompt when the user has a race selected on the tab.
# Kept separate from SYSTEM because it changes with every click, and because
# what it is really doing is resolving the word "this" -- the user is looking
# at a race and should not have to spell out which one.
CONTEXT_NOTE = """

The user currently has this race selected on the Races tab:

    date {date}, track {track}, off time {time}

That is what "this race", "the race", "these runners" or a question with no \
race named refers to. Call get_race_card with exactly those three values -- do \
not go looking for the day first. If they name a different race, use theirs \
instead."""

HORSE_NOTE = """

They also have {horse} selected among the runners, so an unqualified "this \
horse" or "it" means that one."""


def build_system(context):
    """SYSTEM, plus what the user is looking at."""
    if not context or not context.get("date"):
        return SYSTEM
    note = SYSTEM + CONTEXT_NOTE.format(
        date=context["date"], track=context.get("track", "?"),
        time=context.get("time", "?"))
    if context.get("horse"):
        note += HORSE_NOTE.format(horse=context["horse"])
    return note


# --------------------------------------------------------------- anthropic ---

def _anthropic_tools():
    return [{"name": t["name"], "description": t["description"],
             "input_schema": {"type": "object", "properties": t["properties"],
                              "required": t["required"]}}
            for t in TOOLS]


def _anthropic_sources(content, sources):
    """Record the sites a web_search_tool_result block cites.

    Web search runs Anthropic-side, so it never reaches `call` and would be
    invisible without this -- the reader could not tell a database answer from
    one that also went out to the bookmakers.
    """
    for b in content:
        if getattr(b, "type", None) != "web_search_tool_result":
            continue
        body = getattr(b, "content", None)
        # an error comes back as a single object where a success is a list
        for item in (body if isinstance(body, list) else []):
            d = _domain(getattr(item, "url", "") or "")
            if d and d not in sources:
                sources.append(d)


def chat_anthropic(model_id, history, call, system, sources):
    import anthropic

    client = anthropic.Anthropic()
    tools = _anthropic_tools() + [
        # Anthropic-hosted: the search runs their side and the results come
        # back in this same response, so there is nothing to execute here.
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    ]
    msgs = [{"role": m["role"], "content": m["content"]} for m in history]

    # Opus is the tier the refusal-fallback beta covers; a betting question is
    # exactly the kind a classifier may decline, and a silent stop is a worse
    # answer than the same question run on the previous Opus.
    extra = {}
    if model_id == "claude-opus-5":
        extra = {"betas": ["server-side-fallback-2026-06-01"],
                 "fallbacks": [{"model": "claude-opus-4-8"}]}
    create = client.beta.messages.create if extra else client.messages.create

    for _ in range(MAX_STEPS):
        try:
            r = create(model=model_id, max_tokens=MAX_TOKENS, system=system,
                       thinking={"type": "adaptive"}, messages=msgs, tools=tools,
                       **extra)
        except anthropic.AuthenticationError:
            raise ChatError("ANTHROPIC_API_KEY was rejected by Anthropic.")
        except anthropic.RateLimitError:
            raise ChatError("Anthropic rate limit reached -- try again shortly.")
        except anthropic.APIStatusError as e:
            raise ChatError(f"Anthropic returned {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            raise ChatError("Could not reach Anthropic. Check the connection.")

        if r.stop_reason == "refusal":
            raise ChatError("The model declined to answer that.")

        _anthropic_sources(r.content, sources)
        calls = [b for b in r.content if b.type == "tool_use"]
        if not calls:
            return "".join(b.text for b in r.content if b.type == "text").strip()

        msgs.append({"role": "assistant", "content": r.content})
        results = []
        for b in calls:
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(call(b.name, b.input),
                                                  default=str)})
        # every result for one turn goes back in a single user message
        msgs.append({"role": "user", "content": results})

    raise ChatError(f"Gave up after {MAX_STEPS} tool calls without an answer.")


# ------------------------------------------------------------------ openai ---

def chat_openai(model_id, history, call, system, sources):
    key = os.environ["OPENAI_API_KEY"]
    searched = [False]      # a web_search_call ran, even if it named no site
    tools = [{"type": "web_search"}] + [
        {"type": "function", "name": t["name"], "description": t["description"],
         "parameters": {"type": "object", "properties": t["properties"],
                        "required": t["required"], "additionalProperties": False}}
        for t in TOOLS]
    items = [{"role": m["role"], "content": m["content"]} for m in history]

    for _ in range(MAX_STEPS):
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model_id, "instructions": system, "input": items,
                  "tools": tools, "max_output_tokens": MAX_TOKENS},
            timeout=HTTP_TIMEOUT)
        body = r.json()
        if body.get("error"):
            raise ChatError(f"OpenAI: {body['error'].get('message', r.status_code)}")

        out = [o for o in (body.get("output") or []) if o]
        for o in out:
            if o.get("type") == "web_search_call":
                # records that a search ran, but not where. The annotations
                # below name the sites when the answer cites them; reply()
                # falls back to a bare "web" only if none turn up.
                searched[0] = True
            for cc in (o.get("content") or []):
                for a in (cc.get("annotations") or []) if isinstance(cc, dict) else []:
                    d = _domain(a.get("url", ""))
                    if d and d not in sources:
                        sources.append(d)
        calls = [o for o in out if o.get("type") == "function_call"]
        if not calls:
            if searched[0] and not sources:
                sources.append("web (sites not named by the model)")
            text = "".join(
                c.get("text", "") for o in out if o.get("type") == "message"
                for c in (o.get("content") or []))
            return text.strip()

        # The *whole* assistant turn goes back, not just the function_call
        # items. gpt-5.x emits a `reasoning` item alongside its calls, and the
        # API rejects a function_call whose reasoning item is missing:
        #
        #   Item 'fc_...' of type 'function_call' was provided without its
        #   required 'reasoning' item: 'rs_...'
        #
        # Echoing everything also covers web_search_call items, which the API
        # accepts back as input -- confirmed against it, along with the failure
        # above, which only appears once the model actually reasons. A question
        # simple enough not to need reasoning never triggered it.
        items.extend(out)
        for c in calls:
            args = json.loads(c.get("arguments") or "{}")
            items.append({"type": "function_call_output", "call_id": c["call_id"],
                          "output": json.dumps(call(c["name"], args),
                                               default=str)})

    raise ChatError(f"Gave up after {MAX_STEPS} tool calls without an answer.")


# ------------------------------------------------------------------ gemini ---

def chat_google(model_id, history, call, system, sources):
    key = os.environ["GOOGLE_API_KEY"]
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model_id}:generateContent")
    tools = [
        {"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": {"type": "object", "properties": t["properties"],
                            "required": t["required"]}}
            for t in TOOLS]},
        {"google_search": {}},
    ]
    contents = [{"role": "user" if m["role"] == "user" else "model",
                 "parts": [{"text": m["content"]}]} for m in history]

    for _ in range(MAX_STEPS):
        r = requests.post(
            url, headers={"x-goog-api-key": key},
            json={"contents": contents,
                  "systemInstruction": {"parts": [{"text": system}]},
                  "tools": tools,
                  # without this Gemini refuses google_search alongside
                  # functionDeclarations rather than dropping one of them
                  "tool_config": {"include_server_side_tool_invocations": True},
                  "generationConfig": {"maxOutputTokens": MAX_TOKENS}},
            timeout=HTTP_TIMEOUT)
        body = r.json()
        if "error" in body:
            raise ChatError(f"Gemini: {body['error'].get('message', r.status_code)}")
        cands = body.get("candidates") or []
        if not cands:
            raise ChatError("Gemini returned no answer"
                            f" ({body.get('promptFeedback', 'no reason given')}).")

        # The chunk's `uri` is a vertexaisearch.cloud.google.com redirect, so
        # it names Google rather than the source. `title` carries the real
        # site -- "oddschecker.com" -- which is what a reader wants.
        for ch in ((cands[0].get("groundingMetadata") or {})
                   .get("groundingChunks") or []):
            w = (ch or {}).get("web") or {}
            d = _domain(w.get("title") or "") or _domain(w.get("uri") or "")
            if d and d not in sources:
                sources.append(d)
        content = cands[0].get("content") or {}
        parts = content.get("parts") or []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not calls:
            return "".join(p.get("text", "") for p in parts).strip()

        contents.append(content)
        answers = []
        for c in calls:
            args = c.get("args") or {}
            # Gemini takes the result as a nested object rather than a string,
            # so it goes through the request body's own encoder -- which has no
            # date support. Round-trip it here, where default=str can apply.
            result = json.loads(json.dumps(call(c["name"], args), default=str))
            answers.append({"functionResponse": {
                "name": c["name"], "response": {"result": result}}})
        contents.append({"role": "user", "parts": answers})

    raise ChatError(f"Gave up after {MAX_STEPS} tool calls without an answer.")


DRIVERS = {"anthropic": chat_anthropic, "openai": chat_openai,
           "google": chat_google}


def reply(model_key, history, web, context=None):
    """Answer the last message in `history`. Returns (text, tools_used, sources).

    `web` is the rbd_web module the server is actually running -- see run_tool
    for why it is passed rather than imported.

    `context` is what the user has selected on the tab, if anything: the race,
    and the runner within it. It resolves the pronouns -- somebody looking at a
    race card should be able to ask "who wins this?" without retyping the
    course and the off time.
    """
    if model_key not in BY_KEY:
        raise ChatError(f"unknown model {model_key!r}")
    _, label, vendor, model_id = BY_KEY[model_key]

    env = KEY_ENV[vendor]
    if not os.environ.get(env):
        raise ChatError(
            f"{label} needs {env} set in the environment. Add it to setenv.rc "
            "and restart the server.")

    used, sources = [], []

    def call(name, args):
        # Past the budget, answer the tool with a refusal rather than raising:
        # the model then writes its answer from what it already fetched, which
        # is a better outcome than losing the turn. Every vendor treats an
        # unexpected tool result as something to read, not a fatal error.
        if len(used) >= MAX_CALLS:
            return {"error": f"Tool budget of {MAX_CALLS} calls is spent for this "
                             "question. Answer from what you have already "
                             "gathered, and say it is based on a partial look."}
        used.append(name)
        return run_tool(web, name, args)

    text = DRIVERS[vendor](model_id, history, call, build_system(context),
                           sources)
    if not text:
        raise ChatError(f"{label} returned an empty answer.")
    return text, used, sources
