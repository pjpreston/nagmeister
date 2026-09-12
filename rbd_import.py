#!/usr/bin/env python3
"""Load the racing-bet-data.com results workbooks into a DuckDB table.

Reads columns A..AJ of the "Results" sheet of every workbook under
data/results/ into a single `race_results` table, tagged with the source
filename. Columns after AJ are the raw in-play tick data and are ignored.

    python rbd_import.py                      # load every file not yet loaded
    python rbd_import.py --date 04/09/2026    # just that day's file
    python rbd_import.py --file "data/results/2026-09/Results - 04092026.xlsx"
    python rbd_import.py --status             # what's loaded
    python rbd_import.py --schema             # print the DDL and exit
    python rbd_import.py --rederive           # recompute the derived columns

Three derived columns are computed as each row goes in: dist_yds (the race
length in yards), win_dist_len (lengths behind the winner) and one_pnd_win
(what £1 to win would have returned). --rederive recomputes them in place for
rows already loaded, which needs no workbook.

Re-running is safe: a file already recorded in `loaded_files` is skipped, so
the normal workflow is to run rbd_results.py to fetch new days and then run
this with no arguments. --force reloads files that are already in.

Why DuckDB: it is free and embedded (no server to run), it reads .xlsx
natively so there is no pandas/openpyxl dependency in the load path, and it
is columnar, which suits the aggregate-heavy queries this data is for.
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from _venv import use_venv

use_venv()  # must precede the third-party imports below

import duckdb  # noqa: E402

DEFAULT_DB = Path("data/nagmeister.duckdb")
DEFAULT_DATA_DIR = Path("data/results")
DEFAULT_PRERACE_DIR = Path("data/pre-race")
SHEET = "Results"
TABLE = "race_results"
LEDGER = "loaded_files"

# Column A..AJ of the Results sheet, in order. Types were derived by scanning
# every value in all 351 workbooks (115,899 rows) rather than sampling, because
# per-file type inference is actively wrong here -- DuckDB reads Place as DOUBLE
# from the early rows of most files and then hits 'UR' further down.
#
# The awkward ones, all confirmed against the full dataset:
#   Place       6,998 non-numeric -- DSQ, CO, RR, BD, PU and friends
#   Date        stored as an Excel serial in most files, 'DD/MM/YYYY' text in some
#   Pace        contains '#DIV/0!'
#   Stall/Draw  contains '#N/A' (38% -- jumps races have no stalls)
#   OffR        uses an en-dash for missing
#   WinDist     '½-[4¼]', 'hd', ...
# Excel error literals, empty strings and the en-dash all fall out as NULL via
# TRY_CAST, so no special-casing is needed beyond choosing the right type.
#
#      excel, source header,        db column,           type
COLUMNS = [
    ("A",  "Date",              "race_date",         "DATE"),
    ("B",  "Place",             "place",             "VARCHAR"),
    ("C",  "BFSP Rank",         "bfsp_rank",         "INTEGER"),
    ("D",  "Ind SP",            "ind_sp",            "VARCHAR"),
    ("E",  "Ind SP Decimal",    "ind_sp_decimal",    "DOUBLE"),
    ("F",  "Time",              "race_time",         "TIME"),
    ("G",  "Runners",           "runners",           "INTEGER"),
    ("H",  "Track Name",        "track_name",        "VARCHAR"),
    ("I",  "Class (GB only)",   "class_gb",          "INTEGER"),
    ("J",  "Distance",          "distance",          "VARCHAR"),
    ("K",  "Age",               "age",               "INTEGER"),
    ("L",  "Horse",             "horse",             "VARCHAR"),
    ("M",  "Weight",            "weight",            "VARCHAR"),
    ("N",  "Trainer",           "trainer",           "VARCHAR"),
    ("O",  "Jockey",            "jockey",            "VARCHAR"),
    ("P",  "OffR",              "official_rating",   "INTEGER"),
    ("Q",  "Race Name",         "race_name",         "VARCHAR"),
    ("R",  "Going",             "going",             "VARCHAR"),
    ("S",  "Headgear",          "headgear",          "VARCHAR"),
    ("T",  "WinDist",           "win_dist",          "VARCHAR"),
    ("U",  "Min Price",         "min_price",         "DOUBLE"),
    ("V",  "Max Price",         "max_price",         "DOUBLE"),
    ("W",  "Silk No",           "silk_no",           "INTEGER"),
    ("X",  "Pace",              "pace",              "INTEGER"),
    ("Y",  "Stall / Draw",      "stall_draw",        "INTEGER"),
    ("Z",  "Winning Time",      "winning_time",      "VARCHAR"),
    ("AA", "Prize Money",       "prize_money",       "DOUBLE"),
    ("AB", "BSP",               "bsp",               "DOUBLE"),
    ("AC", "15 Mins",           "price_15min",       "DOUBLE"),
    ("AD", "10 mins",           "price_10min",       "DOUBLE"),
    ("AE", "5 mins",            "price_5min",        "DOUBLE"),
    ("AF", "3 mins",            "price_3min",        "DOUBLE"),
    ("AG", "2 mins",            "price_2min",        "DOUBLE"),
    ("AH", "1 min",             "price_1min",        "DOUBLE"),
    ("AI", "Post Time",         "price_post_time",   "DOUBLE"),
    ("AJ", "Last Traded Price", "last_traded_price", "DOUBLE"),
]
N_COLS = len(COLUMNS)

# ---------------------------------------------------------------- derived ---
#
# Three numbers the workbook does not carry, computed as each row is inserted.
# The source columns are text that is awkward to do arithmetic on: Distance is
# '1m3½f', WinDist is '½-[3½]', and what a £1 win bet returned needs Place and
# Ind SP read together.
#
# Deliberately NOT part of COLUMNS. That list is the positional map onto the
# sheet's columns A..AJ -- source_columns() slices the workbook's own headers to
# len(COLUMNS) -- so anything added to it would be looked for in the file, and
# these are not in the file.
#
# The expressions are written against the *destination* column names rather than
# the cast expressions, so the same strings serve both the INSERT and --rederive
# and the two cannot drift apart.

# 1760 yards to the mile, 220 to the furlong.
YARDS_PER_MILE = 1760
YARDS_PER_FURLONG = 220

# Fractions in this data are only ever quarters. Checked across all 217,962
# rows of both Distance ('2m½f', '1m3½f') and WinDist ('1¼', '½-[3½]') -- no
# eighths appear, so there is no point handling them until they do.
FRACTIONS = {"¼": 0.25, "½": 0.5, "¾": 0.75}

# The named margins, in lengths. Racing writes a sub-length gap as a body part
# rather than a number, and these seven are the complete set in the data --
# 3,545 rows. Deliberately a whitelist: 'Min Price' and 'WinDist' also turn up
# in WinDist on the files with shifted columns, and those must stay NULL rather
# than being invented into a distance.
MARGINS = {
    "nse": 0.01,      # nose
    "shd": 0.05,      # short head
    "sht-hd": 0.05,
    "hd": 0.10,       # head
    "snk": 0.20,      # short neck
    "nk": 0.25,       # neck
    "dht": 0.0,       # dead heat -- not behind at all
}

# the character class shared by every pattern below, so they cannot disagree
_FRAC_CLASS = "".join(FRACTIONS)


def _case(expr, mapping, default="NULL"):
    """SQL CASE over a str->number mapping."""
    whens = " ".join(f"WHEN '{k}' THEN {v}" for k, v in mapping.items())
    return f"CASE {expr} {whens} ELSE {default} END"


def _frac(expr):
    """The fraction character in expr as a decimal, or 0 if there is none."""
    return _case(f"regexp_extract({expr}, '([{_FRAC_CLASS}])', 1)", FRACTIONS, default=0)


def _whole(expr):
    """The leading run of digits in expr as a number, or 0 if there is none."""
    return f"COALESCE(TRY_CAST(NULLIF(regexp_extract({expr}, '^([0-9]*)', 1), '') AS DOUBLE), 0)"


def _lengths(expr):
    """A beaten margin as decimal lengths: '1¼' -> 1.25, 'nk' -> 0.25.

    NULL for anything that is neither. Note a plain decimal like '14.5' is
    rejected too: that only shows up in the shifted-column files, where WinDist
    is holding a price, and NULL beats a plausible-looking wrong margin.
    """
    numeric = (
        f"CASE WHEN regexp_matches({expr}, '^[0-9]*[{_FRAC_CLASS}]?$') AND {expr} <> ''"
        f" THEN {_whole(expr)} + {_frac(expr)} END"
    )
    return f"COALESCE({numeric}, {_case(f'trim({expr})', MARGINS)})"


# 'Distance' is <miles>m<furlongs>f with either part optional and the furlongs
# optionally fractional: '7f', '1m', '2m½f', '1m3½f', '7½f'. 59 distinct values
# across the data, all matching this. Anything else -- including the junk the
# shifted-column files leave here -- is NULL rather than a guess.
_DIST_GRAMMAR = f"'^([0-9]+m)?([0-9]*[{_FRAC_CLASS}]?f)?$'"
_MILES = "regexp_extract(distance, '([0-9]+)m', 1)"
_FURLONGS = f"regexp_extract(distance, '([0-9]*)[{_FRAC_CLASS}]?f', 1)"

DIST_YDS_SQL = (
    f"CASE WHEN distance IS NULL"
    f"       OR NOT regexp_matches(distance, {_DIST_GRAMMAR})"
    f"       OR NOT regexp_matches(distance, '[0-9{_FRAC_CLASS}]') THEN NULL"
    f"     ELSE COALESCE(TRY_CAST(NULLIF({_MILES}, '') AS DOUBLE), 0) * {YARDS_PER_MILE}"
    f"        + (COALESCE(TRY_CAST(NULLIF({_FURLONGS}, '') AS DOUBLE), 0)"
    # the only fraction a distance ever carries is the furlong one -- miles are
    # always whole -- so this can just look for a fraction anywhere in the value
    f"           + {_frac('distance')}) * {YARDS_PER_FURLONG}"
    f" END"
)

# WinDist is either a bare margin ('1¼') or gap-[cumulative] ('½-[3½]'), where
# the bracketed figure is the distance behind the *winner* -- which is what we
# want. Empty means the winner, hence 0.
_BRACKET = "regexp_extract(win_dist, '\\[([^]]*)\\]', 1)"

WIN_DIST_LEN_SQL = (
    "CASE WHEN win_dist IS NULL THEN 0.0"
    " WHEN regexp_matches(win_dist, '\\[[^]]*\\]')"
    f" THEN {_lengths(_BRACKET)}"
    f" ELSE {_lengths('win_dist')} END"
)

# Ind SP is '5/1', '9/2', '2/1F', 'Evens' -- but the workbook already supplies
# the decimal equivalent, and it is the stake-inclusive one this wants: 5/1 is
# 6.0, exactly the £5 winnings plus the £1 back. So there is no odds parsing to
# do. Populated for all but 7 of 17,973 winners; those stay NULL, which is
# honest -- unknown odds are not a zero return.
ONE_PND_WIN_SQL = "CASE WHEN trim(place) = '1' THEN ind_sp_decimal ELSE 0.0 END"

#      db column,      label for the web UI,      type,     how to compute it
DERIVED_COLUMNS = [
    ("dist_yds",     "Dist (yds)",              "DOUBLE", DIST_YDS_SQL),
    ("win_dist_len", "Behind Winner (lengths)", "DOUBLE", WIN_DIST_LEN_SQL),
    ("one_pnd_win",  "£1 Win Return",           "DOUBLE", ONE_PND_WIN_SQL),
]

# --------------------------------------------------------------- pre-race ---
#
# The pre-race workbook downloaded by rbd_prerace.py is a different animal to
# the results file, so it gets its own table.
#
# It has one sheet per meeting (named after the racecourse, so the names change
# every day) plus "Combined" and "Selections". Combined is the union of the
# per-meeting sheets and is the only reliably named one, so that is what loads.
#
# A row is not a runner in today's race: it is one *past run* by a horse that is
# declared today, tagged in the last column with the race it is running in
# today. So the table is the form book for today's card. That is why the delete
# key for a re-run is prerace_date -- the day the file is for -- and not the
# race_date on the row, which is historic.
#
# Types come from scanning every value in the available workbooks, same as
# above. The ones that bite:
#   Place, LTO Pos    'BD', 'RR', 'PU', 'DSQ' alongside finishing positions
#   Race Rating       rating bands like '0-95', not a number
#   Tick Incr IR etc  '-' for missing
#   % SP Drop/Incr    '-' and 'NA' for missing
#   Winning Distance  '3¾', '½'
#   Up in Trip        'YES' / 'NO'
#   Headgear          'None' as a literal string, not a null
PRERACE_TABLE = "prerace_form"
PRERACE_SHEET = "Combined"

#      excel, source header,          db column,             type
PRERACE_COLUMNS = [
    ("A",  "Date",                    "race_date",           "DATE"),
    ("B",  "Country",                 "country",             "VARCHAR"),
    ("C",  "Track",                   "track",               "VARCHAR"),
    ("D",  "Going",                   "going",               "VARCHAR"),
    ("E",  "Racetype",                "race_type",           "VARCHAR"),
    ("F",  "Distance",                "distance",            "VARCHAR"),
    ("G",  "Class",                   "class",               "INTEGER"),
    ("H",  "Time",                    "race_time",           "TIME"),
    ("I",  "Stall",                   "stall",               "INTEGER"),
    ("J",  "Horse",                   "horse",               "VARCHAR"),
    ("K",  "Age",                     "age",                 "INTEGER"),
    ("L",  "Pace",                    "pace",                "INTEGER"),
    ("M",  "Weight",                  "weight",              "VARCHAR"),
    ("N",  "Jockey",                  "jockey",              "VARCHAR"),
    ("O",  "Trainer",                 "trainer",             "VARCHAR"),
    ("P",  "SP Fav",                  "sp_fav",              "INTEGER"),
    ("Q",  "Industry SP",             "industry_sp",         "DOUBLE"),
    ("R",  "Betfair SP",              "betfair_sp",          "DOUBLE"),
    ("S",  "IP Min",                  "ip_min",              "DOUBLE"),
    ("T",  "IP Max",                  "ip_max",              "DOUBLE"),
    ("U",  "Pred ISP",                "pred_isp",            "DOUBLE"),
    ("V",  "Place",                   "place",               "VARCHAR"),
    ("W",  "Winning Distance",        "winning_distance",    "VARCHAR"),
    ("X",  "Runners",                 "runners",             "INTEGER"),
    ("Y",  "Tick Drop IR",            "tick_drop_ir",        "INTEGER"),
    ("Z",  "Tick Incr IR",            "tick_incr_ir",        "INTEGER"),
    ("AA", "% SP Drop",               "pct_sp_drop",         "DOUBLE"),
    ("AB", "% SP Incr",               "pct_sp_incr",         "DOUBLE"),
    ("AC", "Runs last 18 mo",         "runs_last_18mo",      "INTEGER"),
    ("AD", "LTO5 % SP Drop",          "lto5_pct_sp_drop",    "DOUBLE"),
    ("AE", "LTO4 % SP Drop",          "lto4_pct_sp_drop",    "DOUBLE"),
    ("AF", "LTO3 % SP Drop",          "lto3_pct_sp_drop",    "DOUBLE"),
    ("AG", "LTO2 % SP Drop",          "lto2_pct_sp_drop",    "DOUBLE"),
    ("AH", "LTO % SP Drop",           "lto_pct_sp_drop",     "DOUBLE"),
    ("AI", "LTO5 IPL",                "lto5_ipl",            "DOUBLE"),
    ("AJ", "LTO4 IPL",                "lto4_ipl",            "DOUBLE"),
    ("AK", "LTO3 IPL",                "lto3_ipl",            "DOUBLE"),
    ("AL", "LTO2 IPL",                "lto2_ipl",            "DOUBLE"),
    ("AM", "LTO IPL",                 "lto_ipl",             "DOUBLE"),
    ("AN", "Wins L5",                 "wins_l5",             "INTEGER"),
    ("AO", "Avg % SP Drop L5",        "avg_pct_sp_drop_l5",  "DOUBLE"),
    ("AP", "Avg % SP Drop last 18 mo", "avg_pct_sp_drop_18mo", "DOUBLE"),
    ("AQ", "RBD Rating",              "rbd_rating",          "INTEGER"),
    ("AR", "RBD Rank",                "rbd_rank",            "INTEGER"),
    ("AS", "Prev Races",              "prev_races",          "INTEGER"),
    ("AT", "Days Since LTO",          "days_since_lto",      "INTEGER"),
    ("AU", "Course Winner",           "course_winner",       "VARCHAR"),
    ("AV", "Distance Winner",         "distance_winner",     "VARCHAR"),
    ("AW", "Cla diff since LTO",      "class_diff_lto",      "INTEGER"),
    ("AX", "OR diff since LTO",       "or_diff_lto",         "INTEGER"),
    ("AY", "Crs Wins",                "course_wins",         "INTEGER"),
    ("AZ", "Dist Wins",               "distance_wins",       "INTEGER"),
    ("BA", "Class Wins",              "class_wins",          "INTEGER"),
    ("BB", "Going Wins",              "going_wins",          "INTEGER"),
    ("BC", "Dist (F)",                "distance_furlongs",   "DOUBLE"),
    ("BD", "Up in Trip",              "up_in_trip",          "VARCHAR"),
    ("BE", "WGT (Lbs)",               "weight_lbs",          "INTEGER"),
    ("BF", "WGT diff since LTO",      "weight_diff_lto",     "INTEGER"),
    ("BG", "Tear Weight",             "tear_weight",         "INTEGER"),
    ("BH", "Race Rating",             "race_rating",         "VARCHAR"),
    ("BI", "DOB %",                   "dob_pct",             "DOUBLE"),
    ("BJ", "DOB P/L £1",              "dob_pl_1",            "DOUBLE"),
    ("BK", "PRB",                     "prb",                 "DOUBLE"),
    ("BL", "PRB To Date",             "prb_to_date",         "DOUBLE"),
    ("BM", "LTO Pos",                 "lto_pos",             "VARCHAR"),
    ("BN", "Tick Drop",               "tick_drop",           "INTEGER"),
    ("BO", "10 B2L",                  "b2l_10",              "DOUBLE"),
    ("BP", "25 B2L",                  "b2l_25",              "DOUBLE"),
    ("BQ", "50 B2L",                  "b2l_50",              "DOUBLE"),
    ("BR", "10 B2L To Date",          "b2l_10_to_date",      "DOUBLE"),
    ("BS", "25 B2L To Date",          "b2l_25_to_date",      "DOUBLE"),
    ("BT", "50 B2L To Date",          "b2l_50_to_date",      "DOUBLE"),
    ("BU", "OR",                      "official_rating",     "INTEGER"),
    ("BV", "Headgear",                "headgear",            "VARCHAR"),
    ("BW", "BF Rank",                 "bf_rank",             "INTEGER"),
    ("BX", "Todays Race",             "todays_race",         "VARCHAR"),
]
N_PRERACE_COLS = len(PRERACE_COLUMNS)

# ------------------------------------------------------------------ races ---
#
# The race card: one row per race taking place on a given day.
#
# Derived from prerace_form rather than declared with its own literals, so the
# types cannot drift from the table it is built out of. The db column names are
# prerace_form's, which is what makes a join read naturally:
#
#   races r JOIN prerace_form f
#     ON f.race_date = r.race_date AND f.track = r.track AND f.race_time = r.race_time
#
# Only races on the card date go in. A card's rows also carry every declared
# horse's form history, some 4,900 historic races per file, and those are not
# races taking place that day.
RACES_TABLE = "races"
RACES_KEY = ["race_date", "track", "race_time"]
RACES_COLUMNS = ["race_date", "track", "race_time", "race_type", "distance"]

_PRERACE_TYPE = {db: typ for _, _, db, typ in PRERACE_COLUMNS}

# Excel's day zero. 1899-12-30 rather than 12-31 absorbs the 1900 leap-year bug.
EXCEL_EPOCH = "DATE '1899-12-30'"


def ddl():
    cols = ",\n".join(f"    {db:<22} {typ}" for _, _, db, typ in COLUMNS)
    pre = ",\n".join(f"    {db:<22} {typ}" for _, _, db, typ in PRERACE_COLUMNS)
    # the derived columns go after filename, which is what puts them "at the
    # end" and, less obviously, is what keeps a migrated database identical to a
    # fresh one: ALTER TABLE ADD COLUMN can only append, so declaring them
    # anywhere else here would give the two different column orders
    derived = ",\n".join(f"    {db:<22} {typ}" for db, _, typ, _ in DERIVED_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {TABLE} (\n{cols},\n"
        f"    {'filename':<22} VARCHAR NOT NULL,\n{derived}\n);\n\n"
        f"CREATE TABLE IF NOT EXISTS {PRERACE_TABLE} (\n{pre},\n"
        # the day the workbook is for. race_date above is the historic date of
        # the run being described, so this is what identifies a load and what a
        # re-run deletes on.
        f"    {'prerace_date':<22} DATE NOT NULL,\n"
        f"    {'filename':<22} VARCHAR NOT NULL\n);\n\n"
        f"CREATE TABLE IF NOT EXISTS {RACES_TABLE} (\n"
        + ",\n".join(f"    {c:<22} {_PRERACE_TYPE[c]}" for c in RACES_COLUMNS)
        # (date, track, time) identifies a race; verified unique across the
        # loaded cards. Declaring it means a genuine collision fails the load
        # loudly and rolls back, rather than quietly storing two races at the
        # same track and time.
        + f",\n    PRIMARY KEY ({', '.join(RACES_KEY)})\n);\n\n"
        f"CREATE TABLE IF NOT EXISTS {LEDGER} (\n"
        f"    filename    VARCHAR PRIMARY KEY,\n"
        f"    source_path VARCHAR,\n"
        f"    rows_loaded BIGINT,\n"
        f"    file_mtime  TIMESTAMP,\n"
        f"    loaded_at   TIMESTAMP DEFAULT current_timestamp\n);"
    )


def cast_expr(src, typ):
    """SQL converting one all-varchar Excel column to its target type."""
    clean = f'NULLIF(TRIM("{src}"), \'\')'
    num = f"TRY_CAST({clean} AS DOUBLE)"
    if typ == "VARCHAR":
        # Excel error literals are noise even in the text columns
        return f"CASE WHEN {clean} LIKE '#%' THEN NULL ELSE {clean} END"
    if typ == "DOUBLE":
        return num
    if typ == "INTEGER":
        return f"TRY_CAST({num} AS INTEGER)"
    if typ == "DATE":
        # most files hold an Excel serial, a few hold 'DD/MM/YYYY' text
        return (
            f"CASE WHEN {num} IS NOT NULL THEN {EXCEL_EPOCH} + CAST({num} AS INTEGER)"
            f" ELSE COALESCE(CAST(TRY_STRPTIME({clean}, '%d/%m/%Y') AS DATE),"
            f" TRY_CAST({clean} AS DATE)) END"
        )
    if typ == "TIME":
        # a serial time is a fraction of a day
        return (
            f"CASE WHEN {num} IS NOT NULL"
            f" THEN TIME '00:00:00' + to_seconds(CAST(round({num} * 86400) AS BIGINT))"
            f" ELSE TRY_CAST({clean} AS TIME) END"
        )
    raise ValueError(f"unhandled type {typ}")


def add_derived_columns(con):
    """Bring a database created before the derived columns existed up to date.

    ddl() is CREATE TABLE IF NOT EXISTS, so it does nothing to a table that is
    already there -- without this, an existing database would silently keep
    working and never grow the new columns. ADD COLUMN appends, which is the
    same order ddl() declares them in. Values stay NULL until --rederive.
    """
    for db, _, typ, _ in DERIVED_COLUMNS:
        con.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {db} {typ}")


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute("INSTALL excel; LOAD excel;")
    con.execute(ddl())
    add_derived_columns(con)
    return con


def rederive(con):
    """Recompute the derived columns for every row already loaded.

    They are pure functions of columns the table already holds, so this needs
    no workbook and no re-download -- which is what makes adding a derived
    column to an existing database cheap. Same expressions the INSERT uses.
    """
    sets = ", ".join(f"{db} = {sql}" for db, _, _, sql in DERIVED_COLUMNS)
    con.execute(f"UPDATE {TABLE} SET {sets}")
    return con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]


def sq(s):
    """Single-quote a value for inlining into SQL."""
    return "'" + str(s).replace("'", "''") + "'"


def source_columns(con, path, sheet=SHEET, n=N_COLS):
    """The workbook's own first n column names, which we map to ours by position.

    Matching by name would be wrong: in the results files the AH header is
    '1 min ' in most and '1 min' in the ten most recent.
    """
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_xlsx({sq(path)}, sheet={sq(sheet)}, all_varchar=true)"
    ).fetchall()
    names = [r[0] for r in rows]
    if len(names) < n:
        raise ValueError(f"expected >= {n} columns in {sheet!r}, found {len(names)}")
    return names[:n]


def load_file(con, path, force=False):
    """Load one workbook. Returns rows inserted, or None if skipped."""
    path = Path(path)
    name = path.name
    already = con.execute(f"SELECT rows_loaded FROM {LEDGER} WHERE filename = ?", [name]).fetchone()
    if already and not force:
        return None

    src = source_columns(con, path)
    selects = ",\n       ".join(
        f"{cast_expr(src[i], typ)} AS {db}" for i, (_, _, db, typ) in enumerate(COLUMNS)
    )
    # a workbook can carry trailing all-blank rows; they are not runners
    not_blank = " OR ".join(f'NULLIF(TRIM("{c}"), \'\') IS NOT NULL' for c in src)
    src_cols = ", ".join(db for _, _, db, _ in COLUMNS)
    derived_names = ", ".join(db for db, _, _, _ in DERIVED_COLUMNS)
    derived_exprs = ",\n       ".join(sql for _, _, _, sql in DERIVED_COLUMNS)
    mtime = datetime.fromtimestamp(path.stat().st_mtime)

    con.execute("BEGIN TRANSACTION")
    try:
        if already:
            con.execute(f"DELETE FROM {TABLE} WHERE filename = ?", [name])
            con.execute(f"DELETE FROM {LEDGER} WHERE filename = ?", [name])
        # the casts run in a subquery so the derived expressions can refer to
        # the columns by their final names -- SQL will not let one select-list
        # item reference another's alias. Naming the insert columns rather than
        # relying on their position also means this no longer cares where in the
        # table the derived columns sit.
        con.execute(
            f"INSERT INTO {TABLE} ({src_cols}, filename, {derived_names})\n"
            f"SELECT {src_cols}, filename,\n       {derived_exprs}\n"
            f"FROM (SELECT\n       {selects},\n       {sq(name)} AS filename\n"
            f"      FROM read_xlsx({sq(path)}, sheet={sq(SHEET)}, all_varchar=true)\n"
            f"      WHERE {not_blank}) t"
        )
        n = con.execute(f"SELECT count(*) FROM {TABLE} WHERE filename = ?", [name]).fetchone()[0]
        con.execute(
            f"INSERT INTO {LEDGER} (filename, source_path, rows_loaded, file_mtime)"
            f" VALUES (?, ?, ?, ?)",
            [name, str(path), n, mtime],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def find_prerace_file(data_dir, day):
    """The pre-race workbook for one day, wherever rbd_prerace.py filed it."""
    root = Path(data_dir)
    stamp = day.strftime("%d%m%Y")
    hits = [p for p in sorted(root.rglob("*.xlsx")) if stamp in p.name]
    if not hits:
        raise SystemExit(
            f"no pre-race file for {day:%d/%m/%Y} under {root} "
            f"-- run rbd_prerace.py first"
        )
    return hits[0]


def load_prerace(con, path, day):
    """Load one pre-race workbook, replacing anything already held for that day.

    Re-runnable by construction: the delete and the insert share a transaction,
    so a failure part-way leaves the previous load intact rather than a table
    with the old rows gone and the new ones missing.
    """
    path = Path(path)
    src = source_columns(con, path, sheet=PRERACE_SHEET, n=N_PRERACE_COLS)
    selects = ",\n       ".join(
        f"{cast_expr(src[i], typ)} AS {db}"
        for i, (_, _, db, typ) in enumerate(PRERACE_COLUMNS)
    )
    not_blank = " OR ".join(f'NULLIF(TRIM("{c}"), \'\') IS NOT NULL' for c in src)

    con.execute("BEGIN TRANSACTION")
    try:
        removed = con.execute(
            f"SELECT count(*) FROM {PRERACE_TABLE} WHERE prerace_date = ?", [day]
        ).fetchone()[0]
        con.execute(f"DELETE FROM {PRERACE_TABLE} WHERE prerace_date = ?", [day])
        con.execute(
            f"INSERT INTO {PRERACE_TABLE} SELECT\n       {selects},\n"
            f"       DATE '{day:%Y-%m-%d}' AS prerace_date,\n"
            f"       {sq(path.name)} AS filename\n"
            f"FROM read_xlsx({sq(path)}, sheet={sq(PRERACE_SHEET)}, all_varchar=true)\n"
            f"WHERE {not_blank}"
        )
        n = con.execute(
            f"SELECT count(*) FROM {PRERACE_TABLE} WHERE prerace_date = ?", [day]
        ).fetchone()[0]
        races = refresh_races(con, day)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n, removed, races


def refresh_races(con, day):
    """Rebuild the race card for one day from the rows just loaded.

    Called inside load_prerace's transaction, so the card cannot end up
    describing a different day's data than prerace_form holds -- either both
    land or neither does.

    Restricted to race_date = prerace_date. The rest of a card's rows are the
    declared horses' form history, roughly 4,900 historic races per file, which
    are not races taking place that day.

    Re-runnable the same way as the load it belongs to: the day's races are
    deleted first, so a second run replaces rather than duplicates.
    """
    cols = ", ".join(RACES_COLUMNS)
    con.execute(f"DELETE FROM {RACES_TABLE} WHERE race_date = ?", [day])
    con.execute(
        f"INSERT INTO {RACES_TABLE} ({cols})"
        f" SELECT DISTINCT {cols} FROM {PRERACE_TABLE}"
        f" WHERE prerace_date = ? AND race_date = prerace_date",
        [day],
    )
    return con.execute(
        f"SELECT count(*) FROM {RACES_TABLE} WHERE race_date = ?", [day]
    ).fetchone()[0]


def find_files(data_dir):
    return sorted(Path(data_dir).rglob("*.xlsx"), key=lambda p: (p.parent.name, p.name))


def parse_date(text):
    """Accept 04/09/2026, 2026-09-04 or 04092026."""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d%m%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"unrecognised date {text!r} -- try DD/MM/YYYY or YYYY-MM-DD")


def file_for_date(data_dir, day):
    """The workbook for one race day, found by name then by folder."""
    wanted = f"Results - {day.strftime('%d%m%Y')}.xlsx"
    hit = [p for p in find_files(data_dir) if p.name == wanted]
    if not hit:
        raise SystemExit(f"no file named {wanted!r} under {data_dir}")
    return hit[0]


def show_status(con):
    files, rows = con.execute(
        f"SELECT count(*), coalesce(sum(rows_loaded), 0) FROM {LEDGER}"
    ).fetchone()
    print(f"{files} file(s) loaded, {rows:,} rows in {TABLE}")
    if not files:
        return
    lo, hi = con.execute(f"SELECT min(race_date), max(race_date) FROM {TABLE}").fetchone()
    print(f"race dates {lo} .. {hi}")
    print("\nmost recent loads:")
    for name, n, when in con.execute(
        f"SELECT filename, rows_loaded, loaded_at FROM {LEDGER} ORDER BY loaded_at DESC LIMIT 5"
    ).fetchall():
        print(f"  {name:<28} {n:>6,} rows   {when:%Y-%m-%d %H:%M}")


def show_prerace_status(con):
    days, rows = con.execute(
        f"SELECT count(DISTINCT prerace_date), count(*) FROM {PRERACE_TABLE}"
    ).fetchone()
    print(f"\n{days} pre-race day(s), {rows:,} rows in {PRERACE_TABLE}")
    if not days:
        return
    for day, n, horses in con.execute(
        f"SELECT prerace_date, count(*), count(DISTINCT horse) FROM {PRERACE_TABLE}"
        f" GROUP BY 1 ORDER BY 1 DESC LIMIT 5"
    ).fetchall():
        print(f"  {day}  {n:>7,} rows  {horses:>4} horses declared")
    rdays, rraces = con.execute(
        f"SELECT count(DISTINCT race_date), count(*) FROM {RACES_TABLE}"
    ).fetchone()
    print(f"\n{rdays} race card(s), {rraces:,} races in {RACES_TABLE}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"DuckDB file (default {DEFAULT_DB})")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"workbook root (default {DEFAULT_DATA_DIR})")
    ap.add_argument("--date", help="load only the file for this race day (DD/MM/YYYY)")
    ap.add_argument("--file", help="load only this workbook")
    ap.add_argument("--force", action="store_true", help="reload files already loaded")
    ap.add_argument("--status", action="store_true", help="show what is loaded and exit")
    ap.add_argument("--schema", action="store_true", help="print the DDL and exit")
    ap.add_argument("--rederive", action="store_true",
                    help="recompute the derived columns for rows already loaded and exit")
    ap.add_argument("--prerace", action="store_true",
                    help="load the pre-race workbook instead of the results files;"
                         " today's unless --date is given")
    ap.add_argument("--prerace-dir", default=DEFAULT_PRERACE_DIR,
                    help=f"pre-race workbook root (default {DEFAULT_PRERACE_DIR})")
    args = ap.parse_args(argv)

    if args.schema:
        print(ddl())
        return 0

    con = connect(args.db)
    if args.status:
        show_status(con)
        show_prerace_status(con)
        return 0

    if args.rederive:
        n = rederive(con)
        cols = ", ".join(db for db, _, _, _ in DERIVED_COLUMNS)
        print(f"recomputed {cols} for {n:,} rows in {TABLE}")
        return 0

    if args.prerace:
        day = parse_date(args.date) if args.date else date.today()
        path = Path(args.file) if args.file else find_prerace_file(args.prerace_dir, day)
        if not path.exists():
            raise SystemExit(f"no such file: {path}")
        n, removed, races = load_prerace(con, path, day)
        note = f" (replaced {removed:,})" if removed else ""
        print(f"{path.name}: {n:,} rows for {day:%d/%m/%Y}{note} in {PRERACE_TABLE}")
        print(f"{' ' * len(path.name)}  {races:,} races on the card in {RACES_TABLE}")
        return 0

    if args.file:
        targets = [Path(args.file)]
        if not targets[0].exists():
            raise SystemExit(f"no such file: {args.file}")
    elif args.date:
        targets = [file_for_date(args.data_dir, parse_date(args.date))]
    else:
        targets = find_files(args.data_dir)
        if not targets:
            raise SystemExit(f"no .xlsx files under {args.data_dir}")

    loaded = con.execute(f"SELECT filename FROM {LEDGER}").fetchall()
    known = {r[0] for r in loaded}
    todo = [p for p in targets if args.force or p.name not in known]
    print(f"{len(targets)} file(s) found, {len(todo)} to load")

    total, failed = 0, []
    for i, path in enumerate(todo, 1):
        try:
            n = load_file(con, path, force=args.force)
            total += n or 0
            print(f"  [{i}/{len(todo)}] {path.name:<28} {n:>6,} rows")
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {path.name:<28} FAILED: {e}", file=sys.stderr)
            failed.append(path.name)

    print(f"\ninserted {total:,} rows from {len(todo) - len(failed)} file(s)")
    if failed:
        print(f"{len(failed)} failed: {', '.join(failed[:5])}", file=sys.stderr)
        return 1
    show_status(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
