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

Re-running is safe: a file already recorded in `loaded_files` is skipped, so
the normal workflow is to run rbd_results.py to fetch new days and then run
this with no arguments. --force reloads files that are already in.

Why DuckDB: it is free and embedded (no server to run), it reads .xlsx
natively so there is no pandas/openpyxl dependency in the load path, and it
is columnar, which suits the aggregate-heavy queries this data is for.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from _venv import use_venv

use_venv()  # must precede the third-party imports below

import duckdb  # noqa: E402

DEFAULT_DB = Path("data/nagmeister.duckdb")
DEFAULT_DATA_DIR = Path("data/results")
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

# Excel's day zero. 1899-12-30 rather than 12-31 absorbs the 1900 leap-year bug.
EXCEL_EPOCH = "DATE '1899-12-30'"


def ddl():
    cols = ",\n".join(f"    {db:<18} {typ}" for _, _, db, typ in COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {TABLE} (\n{cols},\n"
        f"    {'filename':<18} VARCHAR NOT NULL\n);\n\n"
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


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute("INSTALL excel; LOAD excel;")
    con.execute(ddl())
    return con


def sq(s):
    """Single-quote a value for inlining into SQL."""
    return "'" + str(s).replace("'", "''") + "'"


def source_columns(con, path):
    """The workbook's own first 36 column names, which we map to ours by position.

    Matching by name would be wrong: the AH header is '1 min ' in most files and
    '1 min' in the ten most recent ones.
    """
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_xlsx({sq(path)}, sheet={sq(SHEET)}, all_varchar=true)"
    ).fetchall()
    names = [r[0] for r in rows]
    if len(names) < N_COLS:
        raise ValueError(f"expected >= {N_COLS} columns, found {len(names)}")
    return names[:N_COLS]


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
    mtime = datetime.fromtimestamp(path.stat().st_mtime)

    con.execute("BEGIN TRANSACTION")
    try:
        if already:
            con.execute(f"DELETE FROM {TABLE} WHERE filename = ?", [name])
            con.execute(f"DELETE FROM {LEDGER} WHERE filename = ?", [name])
        con.execute(
            f"INSERT INTO {TABLE} SELECT\n       {selects},\n       {sq(name)} AS filename\n"
            f"FROM read_xlsx({sq(path)}, sheet={sq(SHEET)}, all_varchar=true)\n"
            f"WHERE {not_blank}"
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"DuckDB file (default {DEFAULT_DB})")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"workbook root (default {DEFAULT_DATA_DIR})")
    ap.add_argument("--date", help="load only the file for this race day (DD/MM/YYYY)")
    ap.add_argument("--file", help="load only this workbook")
    ap.add_argument("--force", action="store_true", help="reload files already loaded")
    ap.add_argument("--status", action="store_true", help="show what is loaded and exit")
    ap.add_argument("--schema", action="store_true", help="print the DDL and exit")
    args = ap.parse_args(argv)

    if args.schema:
        print(ddl())
        return 0

    con = connect(args.db)
    if args.status:
        show_status(con)
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
