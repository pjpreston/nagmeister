<img src="web/logo.svg" alt="NagMeister" width="360">

Horse racing data tooling.

## Layout

| Path | What it is |
| --- | --- |
| `rbd_results.py` | Downloads the daily results workbooks from racing-bet-data.com |
| `rbd_prerace.py` | Downloads the daily pre-race workbook |
| `rbd_import.py` | Loads those workbooks into a DuckDB table |
| `rbd_web.py` + `web/` | Web service: Racing History table and Settings |
| `web/logo*.svg` | Brand assets (see [Logo](#logo)) |
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

## Getting the pre-race data

`rbd_prerace.py` is the sibling of `rbd_results.py`: same site, same sign-in,
same throttling, but the pre-race card published each morning rather than the
results published after racing. Files land in the same shape under `pre-race`:

```
data/pre-race/2026-09/Daily - 06092026.xlsx
data/results/2026-09/Results - 06092026.xlsx
```

```bash
python rbd_prerace.py                  # today's pre-race file
python rbd_prerace.py sample           # public sample, no login
python rbd_prerace.py --skip-existing  # for a cron job: no-op if already have it
```

The workbook holds a sheet per meeting plus `Combined` and `Selections`; the
`Combined` sheet is each runner's form history, which is the useful part for
comparing horses before a race.

The site sends every day's file as `Daily.xlsx` with no date in it, so the tool
stamps the file date on itself — taken from the page rather than the clock, so a
run just after midnight still files the workbook the site is actually offering.
Without that, each day would overwrite the last.

Sign-in and throttling are imported from `rbd_results.py` rather than
reimplemented, so both tools authenticate identically and share one rate limiter.

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

### Pre-race data

`rbd_import.py --prerace` loads the workbook that `rbd_prerace.py` downloads
into a separate `prerace_form` table.

```bash
python rbd_import.py --prerace                    # today's file
python rbd_import.py --prerace --date 06/09/2026  # a specific day
```

It is re-runnable by design: every load deletes whatever is already held for
that day and re-inserts, inside one transaction, so a failure part-way leaves
the previous load intact rather than a half-replaced day.

The workbook has a sheet per meeting — named after the racecourse, so the names
change daily — plus `Combined` and `Selections`. `Combined` is the union of the
per-meeting sheets and the only reliably named one, so that is what loads.

A row is **not** a runner in today's race. It is one *past run* by a horse
declared today, tagged with `todays_race` (`YORK / 06/09/26/17:00`). So the
table is the form book for today's card: each declared horse contributes one row
dated today plus one row per previous run. That is why the delete key is
`prerace_date`, the day the file is for, rather than `race_date`, which is
historic.

```sql
-- today's declared runners and how many past runs we hold for each
SELECT todays_race, horse, count(*) - 1 AS previous_runs
FROM prerace_form
WHERE prerace_date = current_date
GROUP BY 1, 2 ORDER BY 1, 2;
```

Types were derived the same way as the results table, by scanning every value.
The ones that bite: `place` and `lto_pos` carry `PU`, `F`, `UR`, `BD`, `DSQ`
alongside finishing positions; `race_rating` holds bands like `0-95`;
`up_in_trip` is `YES`/`NO`; and `-` and `NA` appear as missing markers in the
percentage columns.

```sql
-- e.g. strike rate by trainer
SELECT trainer, count(*) AS runs,
       sum(CASE WHEN place = '1' THEN 1 ELSE 0 END) AS wins
FROM race_results GROUP BY trainer ORDER BY runs DESC LIMIT 10;
```

## Browsing the data

`rbd_web.py` serves two tabs: **Racing History**, showing every column of
`race_results` with per-column filtering, sorting and a find-in-table search;
and **Settings**, which governs look and feel.

```bash
./nag.sh start        # start it, wait until it answers, print the URL
./nag.sh stop
./nag.sh status       # running? where? how many rows?
./nag.sh restart
```

`nag.sh` writes the pid to `.nag.pid` and output to `.nag.log`, both gitignored.
Override the defaults with environment variables:

```bash
PORT=9000 HOST=0.0.0.0 DB=/other/path.duckdb ./nag.sh start
```

Or run it in the foreground:

```bash
.venv/bin/python rbd_web.py            # http://127.0.0.1:8000
.venv/bin/python rbd_web.py --port 9000 --db data/nagmeister.duckdb
```

| Feature | How it works |
| --- | --- |
| Sort | Click any column header; click again to reverse |
| Filter | Type in the box under a header. Text columns match on substring, or `=exact`. Numeric, date and time columns also take `>5`, `<=2.5`, `1..9` |
| Search | Type in "Find in table"; Enter or ↓ for the next match, Shift+Enter or ↑ for the previous. Matches wrap at both ends |
| Reset | Clears every filter, the sort and the search |

The table is ~150k rows, so filtering, sorting, searching and paging all happen
in DuckDB rather than the browser. Search returns the ordinal position of every
matching cell within the current filtered and sorted result set, which is what
lets next/previous jump to a match on a page that is not loaded yet — the page
follows the match rather than the match being limited to the page.

Because the search ordinals and the page query are separate SQL statements,
every `ORDER BY` ends with `rowid`. Without a total ordering the two can break
ties differently — 16k+ groups share `(race_date, race_time, filename)`, since
every runner in a race does — and a match would then scroll to the wrong row.

The database is opened **read-only**, so several readers can attach at once.
DuckDB does not allow a reader alongside a writer, so close any `duckdb` CLI
session or `rbd_import.py` run before starting the server; it reports this
clearly if the file is locked.

## Logo

<img src="web/logo-mark.svg" alt="" width="72" align="left" hspace="14">

A deliberately goofy, happy horse — wall-eyed, buck-toothed, one ear flopped
over. NagMeister is meant to be fun to use and the mark should say so before a
single row of data loads.

<br clear="left">

| Asset | Use |
| --- | --- |
| `web/logo.svg` | Horizontal lockup: mark + wordmark + tagline. Page headers, README, docs |
| `web/logo-mark.svg` | The badge on its own. App header, avatars, anywhere square |
| `web/favicon.svg` | Simplified sibling of the mark, for browser tabs |
| `web/favicon.ico`, `web/favicon-32.png` | Raster fallbacks for older browsers |
| `web/apple-touch-icon.png` | 180×180 full-bleed, iOS home screen |
| `web/og-image.png` | 1200×630 social/link preview card |

Everything is hand-written SVG — no binary source file to lose, and it stays
crisp at any size. The rasters are generated from the SVGs; regenerate them with
Chromium and ImageMagick if the artwork changes.

`favicon.svg` exists because the full mark turns to mush below about 24px. It
drops the blaze, nostrils and mane and keeps only what survives at 16px: the
silhouette, two big eyes and the grin.

The wordmark colour is a CSS variable (`--brand`), so it shifts to a lighter
green in dark mode rather than going muddy.

## Settings

The **Settings** tab controls appearance. Both choices apply immediately and are
remembered the next time NagMeister is opened.

| Setting | Options |
| --- | --- |
| Theme | 13 themes — Auto (follows the OS), Light, Dark, Turf, Midnight, Slate, Nord, Solarized Light, Solarized Dark, Sepia, Rose, Mono, High Contrast |
| Table font | 9 stacks, from system sans through Georgia to Courier. Applies to the table data only, not the surrounding interface |
| Digit alignment | Tabular figures on or off, so prices line up in columns |

Preferences live in `localStorage` under `nagmeister.prefs`. They are per-browser
display choices, and the DuckDB file is opened read-only, so there is nowhere on
the server to write them without adding a second writable store. A small inline
script in `<head>` applies them before first paint, otherwise the default theme
flashes on every load.

Themes are defined in `web/themes.css`, one block of custom properties each.
Adding a theme means adding a block and an entry in the `THEMES` array in
`web/settings.js`; nothing else needs to know it exists. Keep the search
highlight (`--hit`, `--hit-fg`) legible — that is the one constraint. All 13 are
checked with an automated contrast pass: body text ≥ 4.5:1, muted text ≥ 3:1 and
search hits ≥ 4.5:1 against their own background.
