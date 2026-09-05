# nagmeister

Horse racing data tooling.

## Layout

| Path | What it is |
| --- | --- |
| `rbd_results.py` | Downloads the daily results workbooks from racing-bet-data.com |
| `rbd_import.py` | Loads those workbooks into a DuckDB table |
| `data/` | Downloaded workbooks and the database (gitignored — reproducible from the scripts) |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Getting the results data

`rbd_results.py` pulls the daily results files to `data/results/<YYYY-MM>/`,
named as the site names them (`Results - 04092026.xlsx`).

```bash
python rbd_results.py sample      # public sample, no login
python rbd_results.py months      # list the archived months
python rbd_results.py today       # today's file (updates through the day)
python rbd_results.py archive 26H_Aug-26
python rbd_results.py backfill    # every month, skipping what's done
```

Everything except `sample` and `months` needs an Advanced membership:

```bash
export RBD_USER=... RBD_PASS=...
```

Flags: `--delay N` seconds between requests (default 3, or `$RBD_DELAY`),
`--force` to re-download files already on disk.

The site monitors download volume per IP, so requests are throttled and
backfills resume rather than refetch — finished past months are skipped
without a request, while the current month is always rechecked for new
race days. A full backfill is several hundred MB and takes roughly 15
minutes at the default delay.

Requires `requests` and `beautifulsoup4`.

## Loading the data into a database

`rbd_import.py` reads columns A–AJ of each workbook's `Results` sheet into a
single `race_results` table in a DuckDB file at `data/nagmeister.duckdb`, tagged
with the source `filename`. Columns after AJ are the raw in-play tick data and
are ignored.

```bash
.venv/bin/python rbd_import.py                    # load every file not yet loaded
.venv/bin/python rbd_import.py --date 04/09/2026  # just that race day
.venv/bin/python rbd_import.py --status           # what's loaded
.venv/bin/python rbd_import.py --schema           # print the DDL
```

Re-running is safe — files recorded in the `loaded_files` ledger are skipped, so
the routine is `rbd_results.py backfill` to fetch new days, then `rbd_import.py`
to load them. `--force` reloads a file that is already in, replacing its rows.
Each file loads in its own transaction, so a failure part-way leaves no partial
data and no ledger entry.

**DuckDB** was chosen because it is free and embedded (no server to run), reads
`.xlsx` natively, and is columnar — which suits the aggregate-heavy queries this
data exists for.

Every column is read as text and then explicitly cast, rather than letting the
reader infer types. Inference is actively wrong on this data: `Place` looks
numeric for the first hundred-odd rows of most files and then hits `UR`, `PU` or
`DSQ`. Types were derived by scanning all values in every workbook. Excel error
literals (`#N/A`, `#DIV/0!`), empty strings and the en-dash used for missing
ratings all land as `NULL`. Dates arrive as Excel serials in most files and as
`DD/MM/YYYY` text in others; both are handled.

```sql
-- e.g. strike rate by trainer
SELECT trainer, count(*) AS runs,
       sum(CASE WHEN place = '1' THEN 1 ELSE 0 END) AS wins
FROM race_results GROUP BY trainer ORDER BY runs DESC LIMIT 10;
```
