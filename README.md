# nagmeister

Horse racing data tooling.

## Layout

| Path | What it is |
| --- | --- |
| `rbd_results.py` | Downloads the daily results workbooks from racing-bet-data.com |
| `data/` | Downloaded workbooks (gitignored — reproducible from the scripts) |

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
