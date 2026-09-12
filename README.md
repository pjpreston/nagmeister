<img src="web/logo.svg" alt="NagMeister" width="360">

Horse racing data tooling.

## Layout

| Path | What it is |
| --- | --- |
| `rbd_results.py` | Downloads the daily results workbooks from racing-bet-data.com |
| `rbd_prerace.py` | Downloads the daily pre-race workbook |
| `rbd_import.py` | Loads those workbooks into a DuckDB table |
| `rbd_web.py` + `web/` | Web service: Racing History, Racing Form, Races and Settings tabs |
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
python rbd_results.py backfill --from 01/09/2025   # just from there onwards
```

Everything except `sample` and `months` needs an Advanced membership:

```bash
export RBD_USER=... RBD_PASS=...
```

Flags: `--delay N` seconds between requests (default 3, or `$RBD_DELAY`),
`--force` to re-download files already on disk, `--from DATE` to start a
backfill at a given race day, `--dry-run` to list what a backfill would fetch
without downloading anything.

The site monitors download volume per IP, so requests are throttled and
backfills resume rather than refetch — finished past months are skipped
without a request, while the current month is always rechecked for new
race days. A full backfill is several hundred MB and takes roughly 15
minutes at the default delay.

### Backfilling from a date

A backfill always runs **oldest month first**, so it proceeds towards the
current date. The site's month dropdown is newest-first, which is the wrong
direction when you are filling a gap.

`--from` takes `DD/MM/YYYY` or `YYYY-MM-DD` and filters to the day, not just
the month — `--from 15/09/2025` starts at `Results - 15092025.xlsx` and skips
the fourteen earlier files September 2025 also holds.

A month that `--from` only took part of is deliberately **not** marked
`.complete`, so a later full backfill still collects the days it skipped.

`--dry-run` lists the months and files that would be fetched, in order, and
downloads nothing. Worth using before committing to a long run:

```bash
python rbd_results.py backfill --from 15/09/2025 --dry-run
```

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
python rbd_prerace.py months           # list the archived months
python rbd_prerace.py backfill --from 01/09/2025
python rbd_prerace.py archive 25I_Sep-25
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

### Backfilling the pre-race history

`/today/` and `/results/` are two pages of the same WebForms app and render the
same sidebar month archive over the same month values, so the backfill here *is*
the results one, pointed at a different page and output directory — see
`Archive` in `rbd_results.py`. `backfill`, `archive`, `months`, `--from`,
`--force` and `--dry-run` therefore behave exactly as they do there, including
running oldest-month-first.

Only the naming differs. The archive lists these workbooks as
`Daily30092025.xlsx` while `python rbd_prerace.py` files today's download as
`Daily - 30092025.xlsx`. Both go through one `prerace_name()`, so a backfill
recognises days already on disk instead of refetching them under a second
spelling — which matters against a site that meters downloads per IP.

Without a backfill the pre-race history was only ever the days the tool happened
to be run on. The archive goes back to Aug-20, about 30 files a month at roughly
7MB each, so pull what you need rather than all of it:

```bash
python rbd_prerace.py backfill --from 01/09/2025 --dry-run   # check first
python rbd_prerace.py backfill --from 01/09/2025
```

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
.venv/bin/python rbd_import.py --rederive         # recompute the derived columns
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

### Derived columns

Three numbers the workbook does not carry are computed as each row is inserted
and stored at the end of `race_results`. The source columns are text that is
awkward to do arithmetic on, and every query that wanted these was re-doing the
same parsing.

| Column | From | Meaning |
| --- | --- | --- |
| `dist_yds` | `Distance` | race length in yards. `1m3½f` → 1760 + 3.5 × 220 = **2530** |
| `win_dist_len` | `WinDist` | lengths behind the **winner**. `0.0` for the winner |
| `one_pnd_win` | `Place`, `Ind SP` | what £1 to win returned, stake included. `0` if it lost |

`Distance` is `<miles>m<furlongs>f` with either part optional and the furlongs
optionally fractional — `7f`, `1m`, `2m½f`, `1m3½f`, `7½f`. Only quarters occur
across the whole dataset, so only quarters are handled.

`WinDist` is either a bare margin (`1¼`) or `gap [cumulative]` (`½ [3½]`, written
`½-[3½]` in older files). The bracketed figure is the distance behind the winner,
which is the one worth storing — the bare number is only the gap to the horse in
front. Racing also writes sub-length margins as body parts rather than numbers,
so those are mapped to their conventional values:

| `nse` | `shd` / `sht-hd` | `hd` | `snk` | `nk` | `dht` |
| --- | --- | --- | --- | --- | --- |
| 0.01 | 0.05 | 0.10 | 0.20 | 0.25 | 0.0 |

It is a whitelist: anything else becomes `NULL` rather than a guess. That matters
because the files with shifted columns (see below) leave prices and even header
text in `WinDist`, and a plausible-looking wrong margin is worse than a null.

`one_pnd_win` needs no odds parsing — the workbook's own `Ind SP Decimal` is
already the stake-inclusive figure (`5/1` → `6.0`), including for `Evens` and the
`F` favourite suffix.

Despite the name, **`win_dist_len` is in lengths, not yards**. A length is about
2.7 yards, so do not compare it with `dist_yds` without converting.

Because all three are pure functions of columns already in the table, adding or
changing one does not mean re-downloading or re-reading any workbook:

```bash
.venv/bin/python rbd_import.py --rederive    # recompute in place, every row
```

That is also the upgrade path for a database loaded before these columns
existed: opening it adds the columns, and `--rederive` fills them in.

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

### The race card

Loading a pre-race file also rebuilds `races`, the card for that day: one row
per race actually taking place, with `race_date`, `track`, `race_time`,
`race_type` and `distance`. Around 37 races a day.

```sql
-- today's card with field sizes
SELECT r.track, r.race_time, r.race_type, r.distance,
       count(DISTINCT f.horse) AS runners
FROM races r
JOIN prerace_form f
  ON f.race_date = r.race_date AND f.track = r.track AND f.race_time = r.race_time
WHERE r.race_date = current_date
GROUP BY 1, 2, 3, 4 ORDER BY r.race_time;
```

Only races on the card date go in. A file's other rows are the declared horses'
form history — roughly 4,900 historic races per file — which are not races
taking place that day.

`(race_date, track, race_time)` is the primary key: it identifies a race, and
was verified unique across the loaded cards. The column types are derived from
`prerace_form` rather than restated, so they cannot drift from the table the
card is built out of.

The rebuild happens inside the same transaction as the pre-race load, so the
card can never describe a different day's data than `prerace_form` holds —
either both land or neither does — and re-running replaces the day rather than
duplicating it.

```sql
-- e.g. strike rate by trainer
SELECT trainer, count(*) AS runs,
       sum(CASE WHEN place = '1' THEN 1 ELSE 0 END) AS wins
FROM race_results GROUP BY trainer ORDER BY runs DESC LIMIT 10;
```

## Browsing the data

`rbd_web.py` serves three tabs:

| Tab | Shows |
| --- | --- |
| **Racing History** | every column of `race_results` |
| **Racing Form** | every column of `prerace_form` |
| **Races** | the `races` card, with a race-card drill-down |
| **Settings** | look and feel |

Both data tabs are the same grid against a different table, so they have
identical sorting, filtering and find-in-table search, and keep their own
filters, sort, page and search independently of each other. A grid queries its
table only when its tab is first opened.

Adding another table means adding an entry to `DATASETS` in `rbd_web.py` — the
tab, the panel and the column headers all follow from the API. A dataset can
also ask for `compact=True` (show about ten rows and scroll, rather than filling
the viewport) and a `detail` drill-down.

### The Races tab

Shows the `races` table about ten rows at a time. Selecting a race and pressing
**View race card** lists that race's runners beneath the table — horse, stall,
age, pace, weight, jockey, trainer, SP favouritism and industry SP — in market
order, so the favourite leads.

The runners come from `/api/racecard?date=&track=&time=`, which matches
`prerace_form` on `(race_date, track, race_time)` — the key of the `races`
table. For a race on the card date those rows are the declared runners.

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
| Reset | Clears every filter, the sort and the search for that tab |

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
