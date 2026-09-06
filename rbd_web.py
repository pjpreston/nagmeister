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
from pathlib import Path

from _venv import use_venv

use_venv()  # must precede the third-party imports below

import duckdb  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from rbd_import import COLUMNS, TABLE  # noqa: E402

DEFAULT_DB = Path("data/nagmeister.duckdb")
WEB_DIR = Path(__file__).parent / "web"
MAX_MATCHES = 20_000  # cap on cells returned by one search
PAGE_MAX = 500

# db column -> (display label, type). The label is the workbook's own header,
# which is what the user recognises. filename is ours, not from the sheet.
COLUMN_META = [(db, header, typ) for _, header, db, typ in COLUMNS]
COLUMN_META.append(("filename", "Source File", "VARCHAR"))
COLUMN_NAMES = [c[0] for c in COLUMN_META]
COLUMN_TYPE = {c[0]: c[2] for c in COLUMN_META}
NUMERIC = {"INTEGER", "DOUBLE"}

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


def quote(col):
    """Validate a column name against the whitelist and quote it."""
    if col not in COLUMN_NAMES:
        raise HTTPException(400, f"unknown column {col!r}")
    return f'"{col}"'


def filter_clause(col, expr):
    """SQL + params for one column filter.

    Text columns match on substring, or exactly when the filter starts with
    '=' -- without that, a Place filter of "1" also matches 10, 11 and 12.
    Numeric and date columns additionally understand >, >=, <, <=, = and
    `a..b`, so you can ask for `bsp <5` or `race_date 2026-01-01..2026-03-31`.
    """
    expr = expr.strip()
    if not expr:
        return None, []
    q = quote(col)
    typ = COLUMN_TYPE[col]

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


def build_where(filters):
    """AND together the per-column filters. Returns (sql, params)."""
    clauses, params = [], []
    for col, expr in filters.items():
        sql, ps = filter_clause(col, expr)
        if sql:
            clauses.append(sql)
            params.extend(ps)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def order_by(sort, direction):
    """A *total* ordering, always.

    The search endpoint numbers rows with ROW_NUMBER() and the rows endpoint
    pages with LIMIT/OFFSET. Those are separate queries, so if the ORDER BY
    leaves ties the two can break them differently and a match ordinal then
    points at the wrong row. 16k+ groups share (race_date, race_time, filename)
    -- every runner in a race does -- so rowid is appended to make the order
    unique and reproducible across both queries.
    """
    if sort and sort not in COLUMN_NAMES:
        raise HTTPException(400, f"unknown sort column {sort!r}")
    dirn = "DESC" if str(direction).lower() == "desc" else "ASC"
    if not sort:
        return "ORDER BY race_date, race_time, filename, rowid"
    return f"ORDER BY {quote(sort)} {dirn} NULLS LAST, race_date, race_time, rowid"


def parse_filters(request_params):
    """Pull f_<column>=expr pairs out of the query string."""
    out = {}
    for key, val in request_params:
        if key.startswith("f_") and val:
            out[key[2:]] = val
    return out


@app.get("/api/columns")
def api_columns():
    return {"columns": [{"name": n, "label": l, "type": t} for n, l, t in COLUMN_META]}


# The two data endpoints read the raw query string rather than declaring
# parameters, because the per-column filters arrive as arbitrary f_<column> keys.
@app.get("/api/rows")
def rows(request: Request):
    offset = int(request.query_params.get("offset", 0) or 0)
    limit = min(max(int(request.query_params.get("limit", 100) or 100), 1), PAGE_MAX)
    sort = request.query_params.get("sort", "")
    direction = request.query_params.get("dir", "asc")
    filters = parse_filters(request.query_params.multi_items())

    where, params = build_where(filters)
    cur = db()
    total = cur.execute(f"SELECT count(*) FROM {TABLE}{where}", params).fetchone()[0]
    cols = ", ".join(quote(c) for c in COLUMN_NAMES)
    data = cur.execute(
        f"SELECT {cols} FROM {TABLE}{where} {order_by(sort, direction)} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "columns": COLUMN_NAMES,
        "rows": [[None if v is None else str(v) for v in row] for row in data],
    }


@app.get("/api/search")
def search(request: Request):
    """Ordinal position of every cell matching q, in result-set order.

    Returns [[row_ordinal, column_name], ...] so the page can jump straight to
    the row containing the nth match even if it is not currently loaded.
    """
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return {"query": "", "total": 0, "matches": [], "truncated": False}

    sort = request.query_params.get("sort", "")
    direction = request.query_params.get("dir", "asc")
    filters = parse_filters(request.query_params.multi_items())
    where, params = build_where(filters)

    casts = ", ".join(f"CAST({quote(c)} AS VARCHAR) AS {quote(c)}" for c in COLUMN_NAMES)
    onlist = ", ".join(quote(c) for c in COLUMN_NAMES)
    rn = f"ROW_NUMBER() OVER ({order_by(sort, direction)}) - 1"
    sql = f"""
        WITH ordered AS (SELECT {rn} AS __rn, {casts} FROM {TABLE}{where})
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


@app.get("/api/stats")
def stats():
    cur = db()
    rows_, files = cur.execute(
        f"SELECT count(*), count(DISTINCT filename) FROM {TABLE}"
    ).fetchone()
    lo, hi = cur.execute(f"SELECT min(race_date), max(race_date) FROM {TABLE}").fetchone()
    return {"rows": rows_, "files": files, "from": str(lo), "to": str(hi)}


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
