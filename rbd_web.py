#!/usr/bin/env python3
"""Web service for browsing the race_results table.

    .venv/bin/python rbd_web.py            # http://127.0.0.1:8000

Serves a single page showing every column of the table, with per-column
filtering, sorting on any column, and a find-in-table search that steps
through matches with next/previous.

The table is ~150k rows, so filtering, sorting, searching and paging all
happen in DuckDB rather than the browser. The page holds one page of rows
at a time; the search returns the ordinal position of each matching cell
within the current filtered+sorted result set, which is what lets
next/previous jump to a match on a page that isn't loaded yet.

The database is opened read-only, so this can run while other readers are
attached. DuckDB does not allow a reader alongside a writer, so close any
`duckdb` CLI session or rbd_import.py run first.
"""

import argparse
import sys
from datetime import time
from pathlib import Path

from _venv import use_venv

use_venv()  # must precede the third-party imports below

import duckdb  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from rbd_import import (  # noqa: E402
    COLUMNS,
    DERIVED_COLUMNS,
    PRERACE_COLUMNS,
    PRERACE_TABLE,
    RACES_COLUMNS,
    RACES_TABLE,
    TABLE,
    lengths_sql,
)

DEFAULT_DB = Path("data/nagmeister.duckdb")
WEB_DIR = Path(__file__).parent / "web"
MAX_MATCHES = 20_000  # cap on cells returned by one search
PAGE_MAX = 500
# cap on rows one /api/horse/* call returns. Generous, because "all of it" is
# the point of those endpoints, but not unbounded: a busy jumper already has
# 400+ prerace_form rows of 78 columns each, and a caller that asks for every
# horse in a race should not be able to ask for tens of megabytes by accident.
AGENT_MAX = 1000
SUGGEST_MAX = 10  # near-miss names offered when a horse is not found

NUMERIC = {"INTEGER", "DOUBLE"}


# --------------------------------------------------------- column meanings ---
#
# What each column actually means, for the header tooltips. The labels are the
# source workbook's own headers, and plenty of them ("PRB", "DOB %", "10 B2L",
# "Tear Weight") say nothing to a reader who has not seen the site's own
# documentation -- which is the gap these fill.
#
# Only columns whose meaning is established appear here. Where the label is
# site-specific jargon this data cannot settle, there is deliberately no entry
# and the tooltip falls back to the column name and type: a guessed definition
# that reads authoritatively is worse than none, because nothing later
# contradicts it. The ones left out on purpose are DOB % / DOB P/L, the B2L
# group, the IPL group and Tear Weight.
#
# Several of these were confirmed against the data rather than assumed --
# up_in_trip against each horse's previous distance, days_since_lto against the
# gap between its runs, prb against (runners - place) / runners.
COLUMN_DESC = {
    # identity and the race itself
    "race_date": "Date the race was run",
    "race_time": "Scheduled off time",
    "track": "Racecourse",
    "track_name": "Racecourse. Suffixed (AW) for all-weather, (IRE) for Ireland",
    "country": "Country the race was run in",
    "race_name": "Full name of the race",
    "race_type": "Category of race, e.g. Other Handicap, Maiden, Handicap Chase",
    "going": "Ground conditions, e.g. Good, Soft, Standard",
    "distance": "Race distance in miles and furlongs, e.g. 1m3½f",
    "distance_furlongs": "Race distance in furlongs, as a number",
    "class": "Class of race. 1 is the highest",
    "class_gb": "Class of race, British racing only. 1 is the highest",
    "runners": "Number of horses that ran",
    "prize_money": "Prize money for the race",
    "horse": "Horse's name",
    "age": "Horse's age in years on the day",
    "jockey": "Jockey",
    "trainer": "Trainer",
    "weight": "Weight carried, stones and pounds",
    "weight_lbs": "Weight carried, in pounds",
    "headgear": "Headgear worn. 'None' is recorded literally, not left empty",
    "stall": "Starting stall number",
    "stall_draw": "Starting stall number. Empty for races run without stalls",
    "silk_no": "Racecard number on the jockey's silks",
    "pace": "The source's pace figure for how the horse is ridden",
    # result
    "place": ("Finishing position. Non-numeric where the horse did not finish: "
              "PU pulled up, F fell, UR unseated rider, BD brought down, "
              "RR refused to race, SU slipped up, REF refused, DSQ disqualified"),
    "lto_pos": "Finishing position on its previous run",
    "win_dist": ("Distance behind the winner, in lengths. Written as gap-[total], "
                 "where the bracketed figure is the total behind the winner"),
    "winning_distance": "How far this horse finished behind the winner, in lengths",
    "winning_time": "The winning time for the race, not this horse's own time",
    "official_rating": "Official handicap rating",
    "race_rating": "Rating band the race was open to, e.g. 0-95",
    # prices
    "ind_sp": "Industry starting price, as a fraction",
    "ind_sp_decimal": "Industry starting price as decimal odds, stake included",
    "industry_sp": "Industry starting price as decimal odds, stake included",
    "pred_isp": "The source's predicted industry starting price",
    "sp_fav": "Rank in the market by starting price. 1 is the favourite",
    "bsp": "Betfair starting price",
    "betfair_sp": "Betfair starting price",
    "bfsp_rank": "Rank in the market by Betfair starting price. 1 is the shortest",
    "bf_rank": "Rank in the market by Betfair price. 1 is the shortest",
    "min_price": "Lowest price traded on Betfair",
    "max_price": "Highest price traded on Betfair",
    "ip_min": "Lowest price traded in running",
    "ip_max": "Highest price traded in running",
    "last_traded_price": "Last price traded on Betfair",
    "price_15min": "Betfair price 15 minutes before the off",
    "price_10min": "Betfair price 10 minutes before the off",
    "price_5min": "Betfair price 5 minutes before the off",
    "price_3min": "Betfair price 3 minutes before the off",
    "price_2min": "Betfair price 2 minutes before the off",
    "price_1min": "Betfair price 1 minute before the off",
    "price_post_time": "Betfair price at the off",
    # market movement
    "pct_sp_drop": "Percentage the price shortened before the off",
    "pct_sp_incr": "Percentage the price drifted before the off",
    "lto_pct_sp_drop": "Percentage the price shortened on its previous run",
    "lto2_pct_sp_drop": "Percentage the price shortened two runs ago",
    "lto3_pct_sp_drop": "Percentage the price shortened three runs ago",
    "lto4_pct_sp_drop": "Percentage the price shortened four runs ago",
    "lto5_pct_sp_drop": "Percentage the price shortened five runs ago",
    "avg_pct_sp_drop_l5": "Average price shortening over its last five runs",
    "avg_pct_sp_drop_18mo": "Average price shortening over the last 18 months",
    "tick_drop": "Price movement in Betfair ticks, negative for a drift",
    "tick_drop_ir": "Price movement in Betfair ticks in running",
    "tick_incr_ir": "Price drift in Betfair ticks in running",
    # record
    "prev_races": "Races the horse has run before this one",
    "runs_last_18mo": "Races the horse has run in the last 18 months",
    "wins_l5": "Wins in its last five runs",
    "course_wins": "Wins at this course",
    "distance_wins": "Wins at this distance",
    "class_wins": "Wins in this class",
    "going_wins": "Wins on this going",
    "course_winner": "Whether the horse has won at this course",
    "distance_winner": "Whether the horse has won at this distance",
    "prb": "Percentage of rivals beaten. 100 means it won, 0 means it finished last",
    "prb_to_date": "Percentage of rivals beaten across its career to that point",
    "days_since_lto": "Days since its previous run",
    "up_in_trip": "Whether this race is further than its previous run",
    # RBD is the source site's own initials. Sparse -- about 6% of rows carry it
    "rbd_rating": "racing-bet-data's own rating. Only present on a minority of rows",
    "rbd_rank": "Rank in the race by RBD Rating, where that rating is present",
    "class_diff_lto": "Change in class since its previous run",
    "or_diff_lto": "Change in official rating since its previous run",
    "weight_diff_lto": "Change in weight carried since its previous run, in pounds",
    # ours, not the workbook's
    "dist_yds": "Race distance in yards. Derived from Distance on import",
    "win_dist_len": ("Lengths behind the winner, as a number. 0 for the winner; "
                     "empty where the horse did not finish"),
    "one_pnd_win": ("What £1 to win would have returned, stake included. "
                    "0 if the horse did not win"),
    "prerace_date": "The card this row was read from -- the morning it was published",
    "todays_race": "The race on that card this horse was declared for",
    "filename": "Source workbook this row was loaded from",

    # The race card's computed columns. Their aliases are unique to the card, so
    # they can live here rather than in a second dictionary. Every one is over
    # the horse's *previous* races of this race's own type and distance.
    "one_pnd_invest": ("Total return from £1 to win on this horse in every one of "
                       "those races. Read against #Races, the notional stake"),
    "n_races": "Races of this type and distance the horse has run before this one",
    "n_wins": "How many of those it won",
    "last_plc": "Finishing position in the most recent of those races",
    "avg_plc": ("Mean finishing position over those races. Runs where it did not "
                "finish are counted in #Races but left out of this average"),
    "med_plc": "Median finishing position over those races",
    "max_win_dist_len": "Furthest it finished behind the winner in those races, in lengths",
    "min_win_dist_len": ("Closest it finished to the winner in those races. 0 if it "
                         "won one of them"),
    "avg_win_dist_len": "Mean lengths behind the winner over those races",
    "med_win_dist_len": "Median lengths behind the winner over those races",
}

# A few names mean different things in the two tables, so the table wins.
TABLE_DESC = {
    PRERACE_TABLE: {
        "race_date": ("Date of the run this row describes. Equal to Card Date on "
                      "the row for the race the horse is declared in; earlier on "
                      "its form rows"),
        "race_time": "Off time of the run this row describes",
        "place": ("Finishing position of the run this row describes, empty for the "
                  "race still to be run. Non-numeric where the horse did not "
                  "finish: PU pulled up, F fell, UR unseated rider, BD brought "
                  "down, DSQ disqualified"),
    },
}


def cell(v):
    """One value as the grid should show it.

    Times lose a zero seconds field: every race time in the data is to the
    minute, so ":00" is three characters of noise on every row of three
    tables. Kept only if the seconds are actually set, and still a valid TIME
    to cast back -- the race-card drill-down passes the displayed time
    straight to the API.
    """
    if v is None:
        return None
    if isinstance(v, time) and v.second == 0 and v.microsecond == 0:
        return f"{v.hour:02d}:{v.minute:02d}"
    return str(v)


def describe(table, col, typ):
    """The tooltip text for one column: what it means, then what it is.

    Always ends with the column name and type, so the tooltip is useful even
    where there is no description to give.
    """
    desc = TABLE_DESC.get(table, {}).get(col) or COLUMN_DESC.get(col)
    tail = f"{col} ({typ})"
    return f"{desc}\n\n{tail}" if desc else tail


class Dataset:
    """One browsable table.

    Everything the API needs to serve a table lives here, so adding another is
    a matter of adding an entry to DATASETS rather than another set of
    endpoints. The column labels are the source workbook's own headers, which
    are what the user recognises; filename and prerace_date are ours.
    """

    def __init__(self, key, table, label, columns, extra, order, count_distinct, date_col,
                 unit="files", compact=False, detail=None):
        self.key = key
        self.table = table
        self.label = label
        # compact: show ~10 rows and scroll, rather than filling the viewport
        self.compact = compact
        # detail: the drill-down this table supports, if any
        self.detail = detail
        self.meta = [(db, header, typ) for _, header, db, typ in columns] + extra
        self.names = [m[0] for m in self.meta]
        self.types = {m[0]: m[2] for m in self.meta}
        # tie-breakers appended to every ORDER BY; see order_by()
        self.order = order
        self.count_distinct = count_distinct
        self.date_col = date_col
        # what count_distinct counts, for the stats line
        self.unit = unit


DATASETS = {
    "results": Dataset(
        "results", TABLE, "Racing History", COLUMNS,
        # the derived columns are not in COLUMNS -- that is the map onto the
        # workbook's own columns -- so they come in here, where they sort and
        # filter like any other numeric column
        [(db, label, typ) for db, label, typ, _ in DERIVED_COLUMNS]
        + [("filename", "Source File", "VARCHAR")],
        ["race_date", "race_time", "filename"], "filename", "race_date",
    ),
    "form": Dataset(
        "form", PRERACE_TABLE, "Racing Form", PRERACE_COLUMNS,
        [("prerace_date", "Card Date", "DATE"), ("filename", "Source File", "VARCHAR")],
        # grouped by the card it belongs to, then by today's race, then the
        # horse, so a form table reads in the order you would study it
        ["prerace_date", "todays_race", "horse", "race_date"], "prerace_date", "race_date",
        unit="cards",
    ),
    # The race card. Its columns are the subset of prerace_form that
    # rbd_import derives races from, so the labels and types come from there
    # rather than being restated.
    "races": Dataset(
        "races", RACES_TABLE, "Races",
        # ordered by RACES_COLUMNS, not prerace_form's order: a card reads
        # Date, Track, Time, Type, Distance, and inheriting the source order
        # would put Time last
        sorted((c for c in PRERACE_COLUMNS if c[2] in RACES_COLUMNS),
               key=lambda c: RACES_COLUMNS.index(c[2])), [],
        ["race_date", "race_time", "track"], "race_date", "race_date",
        unit="cards", compact=True, detail="racecard",
    ),
}

# The six fields a race card shows, as (db column, label). Deliberately a short
# list rather than the whole of prerace_form: this is the shape of a printed
# race card, not another browsable table.
PRERACE_TYPE = {db: typ for _, _, db, typ in PRERACE_COLUMNS}

RACECARD_FIELDS = [
    ("horse", "Horse"),
    ("stall", "Stall"),
    ("age", "Age"),
    ("pace", "Pace"),
    ("weight", "Weight"),
    ("jockey", "Jockey"),
    ("trainer", "Trainer"),
    ("sp_fav", "SP Fav"),
    ("industry_sp", "Industry SP"),
]

# Per-horse form at this race's own race type and distance, from prerace_form.
#
# "Similar race" is same race_type AND same distance, which is what makes these
# worth reading: a horse's record over 5f handicaps says little about how it
# goes over 2m hurdles. Only runs *before* the selected race count.
#
# (alias, label, type, aggregate over the horse's qualifying past runs)
RACECARD_STATS = [
    # What £1 to win on this horse, every time it ran one of these races, would
    # have come back. industry_sp is already decimal and stake-inclusive, so a
    # winner at 6.0 returns 6 and everything else returns nothing. Summed, not
    # averaged: the question is what the whole sequence of bets paid, so a horse
    # that has never won one of these reads 0.0 rather than empty.
    ("one_pnd_invest", "£1 invest", "DOUBLE",
     "round(sum(CASE WHEN place = '1' THEN industry_sp ELSE 0 END), 2)"),
    ("n_races", "#Races", "INTEGER", "count(*)"),
    ("n_wins", "#Wins", "INTEGER", "count(*) FILTER (WHERE place = '1')"),
    # most recent finishing position. Kept as text because it can be 'PU', and
    # DATE + TIME orders two runs on the same day correctly
    ("last_plc", "Last Plc", "VARCHAR", "arg_max(place, race_date + race_time)"),
    # TRY_CAST leaves the non-finishers ('PU', 'F', 'UR', 'BD', 'DSQ' -- 1,358
    # rows) NULL, and avg/median skip nulls. So a pulled-up run still counts
    # towards #Races, where it belongs, but cannot drag an average it has no
    # meaningful value for.
    ("avg_plc", "Avg Plc", "DOUBLE", "round(avg(TRY_CAST(place AS DOUBLE)), 2)"),
    ("med_plc", "Med Plc", "DOUBLE", "round(median(TRY_CAST(place AS DOUBLE)), 2)"),
    # Spread of how far the horse finished behind the winner, over the same
    # races. win_dist_len is computed per row in the form CTE; see the query.
    ("max_win_dist_len", "MaxWinDistLen", "DOUBLE", "round(max(win_dist_len), 2)"),
    ("min_win_dist_len", "MinWinDistLen", "DOUBLE", "round(min(win_dist_len), 2)"),
    ("avg_win_dist_len", "AvgWinDistLen", "DOUBLE", "round(avg(win_dist_len), 2)"),
    ("med_win_dist_len", "MedWinDistLen", "DOUBLE", "round(median(win_dist_len), 2)"),
]

# How far behind the winner this row finished, in lengths. Shared by the card's
# WinDistLen columns and the graph, so the two cannot disagree. The winner is
# none behind, so its empty Winning Distance reads as 0 rather than unknown; a
# non-finisher has no meaningful distance and stays NULL.
WIN_DIST_LEN = (
    f"CASE WHEN f.place = '1' THEN 0.0 ELSE {lengths_sql('f.winning_distance')} END"
)

# The graph beneath the card: one point per qualifying past race, plotted
# against the date it was run.
#
# Every one of these is a column of the same `form` rows the card aggregates, so
# a point on the graph is always one of the runs the card counted -- select
# "£1 invest" here and the points are exactly the values its total sums.
#
# `better` drives a direction hint in the panel heading. The y axes are NOT
# flipped to make "up" mean "good": a reader who misses that a single axis is
# inverted misreads the whole panel, and half of these have no better direction
# anyway. Saying which way is good in words costs nothing and cannot mislead.
#
# (name, label, better, per-row expression over the form CTE)
HORSE_METRICS = [
    ("place", "Place", "lower", "TRY_CAST(place AS DOUBLE)"),
    # the individual returns that one_pnd_invest above sums
    ("invest", "£1 invest", "higher",
     "CASE WHEN place = '1' THEN industry_sp ELSE 0 END"),
    ("win_dist_len", "Winning Distance", "lower", "win_dist_len"),
    # The three below are not in the ticket. They are here because the question
    # the graph exists to answer is "is this horse going the right way", and a
    # finishing position alone cannot say: 3rd of 4 and 3rd of 20 plot
    # identically.
    #   Industry SP  - what the market made of it each time, so a shortening
    #                  price shows confidence building
    #   Official Rating - the handicapper's own assessment, which is the closest
    #                  thing in the data to a measured ability trend
    #   % Rivals Beaten - the source's own PRB, which is Place normalised by
    #                  field size, so it is comparable across a big field and a
    #                  match. 100 = won, 0 = last.
    ("industry_sp", "Industry SP", None, "industry_sp"),
    ("official_rating", "Official Rating", "higher", "official_rating"),
    ("prb", "% Rivals Beaten", "higher", "round(prb * 100, 1)"),
]

DEFAULT_METRIC = "place"

# What each checkbox plots. Separate from COLUMN_DESC because these are one
# race's value in a series, where the card's column of the same name is an
# aggregate over all of them -- "#Races the horse has run" against "the
# position it finished in this one".
HORSE_METRIC_DESC = {
    "place": "Where it finished in each race. Runs it did not finish have no point",
    "invest": ("What £1 to win returned on each race: the starting price if it won, "
               "otherwise 0. These are the values the card's £1 invest totals"),
    "win_dist_len": "How far it finished behind the winner, in lengths. 0 where it won",
    "industry_sp": "Its starting price each time, so a shortening market falls",
    "official_rating": "The handicapper's rating at the time of each run",
    "prb": "Percentage of rivals beaten: 100 means it won, 0 means it finished last",
}


def dataset(request):
    key = request.query_params.get("dataset", "results")
    if key not in DATASETS:
        raise HTTPException(400, f"unknown dataset {key!r}")
    return DATASETS[key]

app = FastAPI(title="Nag Meister", docs_url="/api/docs")
_db_path = DEFAULT_DB
_con = None


def db():
    """A per-request cursor over the shared read-only connection."""
    global _con
    if _con is None:
        try:
            _con = duckdb.connect(str(_db_path), read_only=True)
        except duckdb.IOException as e:
            raise HTTPException(
                503,
                f"cannot open {_db_path} read-only: {e}. "
                "A writer holds the lock -- close any duckdb CLI or rbd_import.py run.",
            )
    return _con.cursor()


def quote(ds, col):
    """Validate a column name against the dataset's whitelist and quote it."""
    if col not in ds.names:
        raise HTTPException(400, f"unknown column {col!r}")
    return f'"{col}"'


def filter_clause(ds, col, expr):
    """SQL + params for one column filter.

    Text columns match on substring, or exactly when the filter starts with
    '=' -- without that, a Place filter of "1" also matches 10, 11 and 12.
    Numeric and date columns additionally understand >, >=, <, <=, = and
    `a..b`, so you can ask for `bsp <5` or `race_date 2026-01-01..2026-03-31`.
    """
    expr = expr.strip()
    if not expr:
        return None, []
    q = quote(ds, col)
    typ = ds.types[col]

    if typ not in NUMERIC and typ not in ("DATE", "TIME") and expr.startswith("="):
        return f"CAST({q} AS VARCHAR) ILIKE ?", [expr[1:].strip()]

    if typ in NUMERIC or typ in ("DATE", "TIME"):
        cast = "DOUBLE" if typ in NUMERIC else typ
        if ".." in expr:
            lo, hi = (p.strip() for p in expr.split("..", 1))
            if lo and hi:
                return f"{q} BETWEEN TRY_CAST(? AS {cast}) AND TRY_CAST(? AS {cast})", [lo, hi]
            if lo:
                return f"{q} >= TRY_CAST(? AS {cast})", [lo]
            if hi:
                return f"{q} <= TRY_CAST(? AS {cast})", [hi]
        for op in (">=", "<=", "!=", ">", "<", "="):
            if expr.startswith(op):
                val = expr[len(op):].strip()
                if val:
                    return f"{q} {op} TRY_CAST(? AS {cast})", [val]

    return f"CAST({q} AS VARCHAR) ILIKE ?", [f"%{expr}%"]


def build_where(ds, filters):
    """AND together the per-column filters. Returns (sql, params)."""
    clauses, params = [], []
    for col, expr in filters.items():
        sql, ps = filter_clause(ds, col, expr)
        if sql:
            clauses.append(sql)
            params.extend(ps)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def order_by(ds, sort, direction):
    """A *total* ordering, always.

    The search endpoint numbers rows with ROW_NUMBER() and the rows endpoint
    pages with LIMIT/OFFSET. Those are separate queries, so if the ORDER BY
    leaves ties the two can break them differently and a match ordinal then
    points at the wrong row -- in race_results 16k+ groups share
    (race_date, race_time, filename), because every runner in a race does. So
    rowid always terminates the sort, making it unique and reproducible across
    both queries whichever dataset is being served.
    """
    if sort and sort not in ds.names:
        raise HTTPException(400, f"unknown sort column {sort!r}")
    dirn = "DESC" if str(direction).lower() == "desc" else "ASC"
    tail = ", ".join(ds.order + ["rowid"])
    if not sort:
        return f"ORDER BY {tail}"
    return f"ORDER BY {quote(ds, sort)} {dirn} NULLS LAST, {tail}"


def parse_filters(request_params):
    """Pull f_<column>=expr pairs out of the query string."""
    out = {}
    for key, val in request_params:
        if key.startswith("f_") and val:
            out[key[2:]] = val
    return out


@app.get("/api/datasets")
def api_datasets():
    """The tables the UI can show, in tab order."""
    return {"datasets": [
        {"key": d.key, "label": d.label, "compact": d.compact, "detail": d.detail}
        for d in DATASETS.values()
    ]}


@app.get("/api/columns")
def api_columns(request: Request):
    ds = dataset(request)
    return {"dataset": ds.key,
            "columns": [{"name": n, "label": l, "type": t,
                         "desc": describe(ds.table, n, t)} for n, l, t in ds.meta]}


# The two data endpoints read the raw query string rather than declaring
# parameters, because the per-column filters arrive as arbitrary f_<column> keys.
@app.get("/api/rows")
def rows(request: Request):
    ds = dataset(request)
    offset = int(request.query_params.get("offset", 0) or 0)
    limit = min(max(int(request.query_params.get("limit", 100) or 100), 1), PAGE_MAX)
    sort = request.query_params.get("sort", "")
    direction = request.query_params.get("dir", "asc")
    filters = parse_filters(request.query_params.multi_items())

    where, params = build_where(ds, filters)
    cur = db()
    total = cur.execute(f"SELECT count(*) FROM {ds.table}{where}", params).fetchone()[0]
    cols = ", ".join(quote(ds, c) for c in ds.names)
    data = cur.execute(
        f"SELECT {cols} FROM {ds.table}{where} {order_by(ds, sort, direction)} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return {
        "dataset": ds.key,
        "total": total,
        "offset": offset,
        "limit": limit,
        "columns": ds.names,
        "rows": [[cell(v) for v in row] for row in data],
    }


@app.get("/api/search")
def search(request: Request):
    """Ordinal position of every cell matching q, in result-set order.

    Returns [[row_ordinal, column_name], ...] so the page can jump straight to
    the row containing the nth match even if it is not currently loaded.
    """
    ds = dataset(request)
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return {"query": "", "total": 0, "matches": [], "truncated": False}

    sort = request.query_params.get("sort", "")
    direction = request.query_params.get("dir", "asc")
    filters = parse_filters(request.query_params.multi_items())
    where, params = build_where(ds, filters)

    casts = ", ".join(f"CAST({quote(ds, c)} AS VARCHAR) AS {quote(ds, c)}" for c in ds.names)
    onlist = ", ".join(quote(ds, c) for c in ds.names)
    rn = f"ROW_NUMBER() OVER ({order_by(ds, sort, direction)}) - 1"
    sql = f"""
        WITH ordered AS (SELECT {rn} AS __rn, {casts} FROM {ds.table}{where})
        SELECT __rn, col FROM (UNPIVOT ordered ON {onlist} INTO NAME col VALUE val)
        WHERE val ILIKE ?
        ORDER BY __rn, col
        LIMIT {MAX_MATCHES + 1}
    """
    cur = db()
    hits = cur.execute(sql, params + [f"%{q}%"]).fetchall()
    truncated = len(hits) > MAX_MATCHES
    hits = hits[:MAX_MATCHES]
    return {
        "query": q,
        "total": len(hits),
        "truncated": truncated,
        "matches": [[int(r), c] for r, c in hits],
    }


@app.get("/api/racecard")
def racecard(request: Request):
    """The runners in one race, for the Races tab's drill-down.

    A race is identified by (race_date, track, race_time) -- the key of the
    races table. Matching prerace_form on those three gives the horses that ran
    in that race; for a race on the card date those are the declared runners.

    Each runner also carries its record over this race's own race type and
    distance (RACECARD_STATS), so the card can be read as form rather than just
    a list of names.
    """
    day = (request.query_params.get("date") or "").strip()
    track = (request.query_params.get("track") or "").strip()
    time_ = (request.query_params.get("time") or "").strip()
    if not (day and track and time_):
        raise HTTPException(400, "date, track and time are all required")

    cols = ", ".join(f'r."{c}"' for c, _ in RACECARD_FIELDS)
    aggs = ",\n                   ".join(
        f"{sql} AS {alias}" for alias, _, _, sql in RACECARD_STATS
    )
    stat_cols = ", ".join(f"s.{alias}" for alias, _, _, _ in RACECARD_STATS)
    sql = f"""
        WITH race AS (
            SELECT race_type, distance FROM {RACES_TABLE}
            WHERE race_date = TRY_CAST(? AS DATE)
              AND track = ? AND race_time = TRY_CAST(? AS TIME)
        ),
        -- prerace_date = race_date restricts this to the card the race was
        -- declared on, which is also how the races table itself is built. Once
        -- several cards are loaded a race's runners also appear as form history
        -- in later cards, with the actual SP rather than the morning's, and
        -- without this the same horse comes back twice.
        runners AS (
            SELECT DISTINCT {", ".join(f'"{c}"' for c, _ in RACECARD_FIELDS)}
            FROM {PRERACE_TABLE}
            WHERE race_date = TRY_CAST(? AS DATE)
              AND track = ? AND race_time = TRY_CAST(? AS TIME)
              AND prerace_date = race_date
        ),
        -- one row per past race, not per row held: a horse declared on several
        -- of the loaded cards carries its whole history in each of them, so
        -- counting rows would count those runs once per card. DISTINCT ON keys
        -- on the race alone, so this stays one row per race even if two cards
        -- ever disagree about a detail of it -- they do not today, but a plain
        -- DISTINCT over the payload would silently start double-counting.
        form AS (
            SELECT DISTINCT ON (f.horse, f.race_date, f.track, f.race_time)
                   f.horse, f.race_date, f.race_time, f.track, f.place,
                   f.industry_sp,
                   {WIN_DIST_LEN} AS win_dist_len
            FROM {PRERACE_TABLE} f, race
            WHERE f.horse IN (SELECT horse FROM runners)
              AND f.race_type = race.race_type
              AND f.distance = race.distance
              -- strictly earlier races only. The ticket describes this as
              -- "rows - 1", which excludes the horse's row for today; going by
              -- date is the same thing for the latest card and stays right for
              -- an earlier one, where later cards have since added runs that
              -- are still in the future as far as this race is concerned.
              AND f.race_date < TRY_CAST(? AS DATE)
            -- DISTINCT ON takes the first row per key, so name one: the
            -- earliest card that carried this run
            ORDER BY f.horse, f.race_date, f.track, f.race_time, f.prerace_date
        ),
        stats AS (
            SELECT horse,
                   {aggs}
            FROM form GROUP BY horse
        )
        SELECT {cols}, {stat_cols}
        FROM runners r LEFT JOIN stats s USING (horse)
        -- by market rank, so the favourite leads as a card would print it
        ORDER BY r.industry_sp NULLS LAST, r.horse
    """
    cur = db()
    rows = cur.execute(sql, [day, track, time_, day, track, time_, day]).fetchall()
    return {
        "race": {"date": day, "track": track, "time": time_},
        "columns": [{"name": c, "label": l, "type": PRERACE_TYPE.get(c, "VARCHAR"),
                     "desc": describe(PRERACE_TABLE, c, PRERACE_TYPE.get(c, "VARCHAR"))}
                    for c, l in RACECARD_FIELDS]
        + [{"name": a, "label": l, "type": t, "desc": describe(None, a, t)}
           for a, l, t, _ in RACECARD_STATS],
        "rows": [[cell(v) for v in r] for r in rows],
    }


@app.get("/api/horseform")
def horseform(request: Request):
    """One horse's qualifying past races, as a time series for the graph.

    Same race, same "similar race" rule and same per-row values as
    /api/racecard -- this returns the individual rows that endpoint aggregates,
    oldest first, so a point on the graph is always one of the runs the card
    counted.

    Values are returned as numbers rather than strings, because the client
    plots them. place_text comes along beside the numeric place so the tooltip
    can show 'PU' for a run that has no number.
    """
    day = (request.query_params.get("date") or "").strip()
    track = (request.query_params.get("track") or "").strip()
    time_ = (request.query_params.get("time") or "").strip()
    horse = (request.query_params.get("horse") or "").strip()
    if not (day and track and time_ and horse):
        raise HTTPException(400, "date, track, time and horse are all required")

    metrics = ",\n                   ".join(
        f"{sql} AS {name}" for name, _, _, sql in HORSE_METRICS
    )
    names = ", ".join(name for name, _, _, _ in HORSE_METRICS)
    sql = f"""
        WITH race AS (
            SELECT race_type, distance FROM {RACES_TABLE}
            WHERE race_date = TRY_CAST(? AS DATE)
              AND track = ? AND race_time = TRY_CAST(? AS TIME)
        ),
        -- one row per past race; see /api/racecard for why DISTINCT ON
        form AS (
            SELECT DISTINCT ON (f.race_date, f.track, f.race_time)
                   f.race_date, f.race_time, f.track, f.place, f.industry_sp,
                   f.official_rating, f.prb,
                   {WIN_DIST_LEN} AS win_dist_len
            FROM {PRERACE_TABLE} f, race
            WHERE f.horse = ?
              AND f.race_type = race.race_type
              AND f.distance = race.distance
              AND f.race_date < TRY_CAST(? AS DATE)
            ORDER BY f.race_date, f.track, f.race_time, f.prerace_date
        )
        SELECT race_date, track, place,
               {metrics}
        FROM form ORDER BY race_date, race_time
    """
    cur = db()
    rows = cur.execute(sql, [day, track, time_, horse, day]).fetchall()
    return {
        "horse": horse,
        "race": {"date": day, "track": track, "time": time_},
        "metrics": [{"name": n, "label": l, "better": b,
                     "desc": HORSE_METRIC_DESC.get(n, "")}
                    for n, l, b, _ in HORSE_METRICS],
        "default": DEFAULT_METRIC,
        "points": [
            {"date": str(r[0]), "track": r[1], "place_text": r[2],
             "values": dict(zip(names.split(", "), r[3:]))}
            for r in rows
        ],
    }


# ------------------------------------------------------------------- agent ---
#
# Endpoints meant to be called by an AI agent rather than by the page.
#
# They differ from the grid endpoints on purpose:
#
#   * rows are objects keyed by column name, not positional arrays, so a row
#     carries its own meaning and the caller does not have to hold a separate
#     column list to read one;
#   * values keep their JSON types -- a number stays a number -- rather than
#     being stringified for display;
#   * the query parameters are declared rather than read out of the raw query
#     string, so /api/docs and /openapi.json describe them and an agent can
#     discover how to call these without being told.
#
# The columns come from the same Dataset entries the grid uses, so these
# describe exactly the schema the tabs show and cannot drift from it.


def jsonable(v):
    """A DuckDB value as something json can hold, without losing type.

    Only dates and times need help; ints, floats, strings and None are already
    fine, and turning those into strings would make the caller parse them back.
    """
    return v.isoformat() if hasattr(v, "isoformat") else v


def horse_rows(ds, horse, limit, offset):
    """Every row of one dataset for one horse, oldest run first.

    Matching is case-insensitive because the two tables disagree about the
    capitals in a name: race_results has 'Moon DOrange' where prerace_form has
    'Moon Dorange', and the same for most French and Irish names. An agent that
    took a name from one endpoint could not query the other otherwise. The
    spelling this table actually holds comes back in `horse`.

    A miss returns 200 with no rows and a list of near-miss names rather than a
    404: not finding a horse is a normal answer to a reasonable question, and
    the suggestions are what let a caller correct a spelling without a separate
    lookup endpoint.
    """
    cur = db()
    cols = ", ".join(f'"{c}"' for c in ds.names)
    where = "WHERE upper(horse) = upper(?)"
    total = cur.execute(
        f"SELECT count(*) FROM {ds.table} {where}", [horse]
    ).fetchone()[0]

    if not total:
        near = cur.execute(
            f"SELECT DISTINCT horse FROM {ds.table}"
            f" WHERE horse ILIKE ? AND horse IS NOT NULL"
            f" ORDER BY horse LIMIT {SUGGEST_MAX}",
            [f"%{horse}%"],
        ).fetchall()
        return {
            "horse": horse, "table": ds.table, "found": False,
            "total": 0, "offset": 0, "limit": limit, "truncated": False,
            "columns": [{"name": n, "label": l, "type": t} for n, l, t in ds.meta],
            "rows": [],
            "suggestions": [r[0] for r in near],
        }

    # rowid terminates the sort so paging is stable: every runner in a race
    # shares (race_date, race_time), so without it two pages can disagree
    rows = cur.execute(
        f"SELECT {cols} FROM {ds.table} {where}"
        f" ORDER BY race_date, race_time, rowid LIMIT ? OFFSET ?",
        [horse, limit, offset],
    ).fetchall()
    return {
        "horse": rows[0][ds.names.index("horse")] if rows else horse,
        "table": ds.table,
        "found": True,
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": offset + len(rows) < total,
        "order": "race_date, race_time",
        "columns": [{"name": n, "label": l, "type": t} for n, l, t in ds.meta],
        "rows": [{n: jsonable(v) for n, v in zip(ds.names, r)} for r in rows],
    }


HORSE_Q = Query(..., description="Horse name, matched exactly but case-insensitively",
                examples=["Rockley Point"])
LIMIT_Q = Query(AGENT_MAX, ge=1, le=AGENT_MAX, description="Max rows to return")
OFFSET_Q = Query(0, ge=0, description="Rows to skip, for paging through `total`")


@app.get("/api/horse/form", summary="Every prerace_form row for one horse")
def horse_form(horse: str = HORSE_Q, limit: int = LIMIT_Q, offset: int = OFFSET_Q):
    """One horse's whole form book: a row per past run, from every card it was
    declared on. A run held on more than one card appears once per card, so
    deduplicate on (race_date, track, race_time) before counting runs."""
    return horse_rows(DATASETS["form"], horse, limit, offset)


@app.get("/api/horse/results", summary="Every race_results row for one horse")
def horse_results(horse: str = HORSE_Q, limit: int = LIMIT_Q, offset: int = OFFSET_Q):
    """One horse's results: a row per race it ran, with the finishing position,
    prices and the derived dist_yds, win_dist_len and one_pnd_win."""
    return horse_rows(DATASETS["results"], horse, limit, offset)


@app.get("/api/stats")
def stats(request: Request):
    ds = dataset(request)
    cur = db()
    rows_, files = cur.execute(
        f"SELECT count(*), count(DISTINCT {ds.count_distinct}) FROM {ds.table}"
    ).fetchone()
    lo, hi = cur.execute(
        f"SELECT min({ds.date_col}), max({ds.date_col}) FROM {ds.table}"
    ).fetchone()
    return {"dataset": ds.key, "rows": rows_, "files": files, "unit": ds.unit,
            "from": str(lo), "to": str(hi)}


# Without an explicit Cache-Control the browser applies heuristic freshness and
# will happily reuse a cached app.js for hours without asking. After a change
# that pairs a new index.html with a new app.js that is not a stale nicety, it
# is a broken page: the old script runs against the new markup and, in the case
# that prompted this, silently rendered only the Settings tab. "no-cache" still
# permits caching -- it just requires revalidation, so the usual answer stays a
# cheap 304 rather than a full re-download.
NO_CACHE = "no-cache, must-revalidate"


@app.middleware("http")
async def revalidate_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = NO_CACHE
    return response


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.exception_handler(HTTPException)
def http_error(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def main():
    global _db_path
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"DuckDB file (default {DEFAULT_DB})")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    _db_path = Path(args.db)
    if not _db_path.exists():
        raise SystemExit(f"no database at {_db_path} -- run rbd_import.py first")

    import uvicorn

    print(f"serving {_db_path} on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
