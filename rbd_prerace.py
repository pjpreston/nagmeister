#!/usr/bin/env python3
"""Download the daily pre-race workbook from racing-bet-data.com.

The sibling of rbd_results.py. That tool takes the results published after
racing; this one takes the pre-race card published each morning, from
https://www.racing-bet-data.com/today/.

    python rbd_prerace.py                 # today's pre-race file
    python rbd_prerace.py sample          # public sample, no login
    python rbd_prerace.py --skip-existing # for a cron job: no-op if already have it
    python rbd_prerace.py months          # list the archived months
    python rbd_prerace.py backfill --from 01/09/2025
    python rbd_prerace.py archive 25I_Sep-25

Files land alongside the results in the same shape, under pre-race:

    data/pre-race/2026-09/Daily - 06092026.xlsx
    data/results/2026-09/Results - 06092026.xlsx

Sign-in, throttling and the spreadsheet check are imported from rbd_results
rather than reimplemented, so both tools authenticate identically and are
paced by the same limiter. Credentials come from RBD_USER / RBD_PASS and an
Advanced membership is required, as for the results file.

The month archive is imported for the same reason. /today/ and /results/ are
two pages of one WebForms app and render the same sidebar controls over the
same month values, so the backfill here is rbd_results' own, pointed at this
page and this output directory -- see rbd_results.Archive.
"""

import argparse
import copy
import re
import sys
from datetime import datetime
from pathlib import Path

from _venv import use_venv

use_venv()  # must precede the third-party imports below

from bs4 import BeautifulSoup  # noqa: E402

from rbd_results import (  # noqa: E402
    DEFAULT_DELAY,
    XLSX_MIME,
    Archive,
    DownloadError,
    backfill,
    download_archive,
    file_day,
    filename_from,
    hidden_fields,
    list_months,
    parse_date,
    postback,
    safe_name,
    save,
    session_from_env,
)

BASE = "https://www.racing-bet-data.com"
TODAY_URL = f"{BASE}/today/"
SAMPLE_URL = f"{BASE}/exceldl/RBD-Daily-Sample.xlsx"
OUTDIR = Path("data/pre-race")

SIGNIN_WARNING_ID = "sidebar_Label3"
FILE_RE = re.compile(r"\.(xlsx|xlsm|xls)$", re.I)
# "The date of the latest file for download is: 06/09/2026 07:25:34"
FILE_DATE_RE = re.compile(r"latest file for download is:\s*(\d{2}/\d{2}/\d{4})", re.I)


def today_page(session):
    r = session.get(TODAY_URL)
    r.raise_for_status()
    return r, BeautifulSoup(r.text, "html.parser")


def file_date(soup):
    """The date the page says the current file is for.

    Preferred over today's local date: the page states which file it is
    actually offering, so a run just after midnight, or on a day the site has
    not yet published, files the workbook under the right month either way.
    """
    m = FILE_DATE_RE.search(soup.get_text(" ", strip=True))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def main_column(soup):
    """The page with the sidebar removed.

    rbd_results learned this the hard way: searching a whole page for the
    first spreadsheet link finds the public sample, not the file you asked
    for, and it downloads happily because the content type is right. Here the
    sidebar holds the month archive and the main column holds a sample link,
    so both are excluded before anything is matched.
    """
    trimmed = copy.copy(soup)
    for el in trimmed.select('[id^="sidebar"]'):
        el.decompose()
    return trimmed


def find_download(soup):
    """Locate today's download control. Returns (kind, target, label).

    The control only renders for a signed-in Advanced member, so this cannot
    be pinned to one markup shape from the logged-out page. The three shapes
    WebForms uses are all handled, and anything sample-shaped is skipped.
    """
    page = main_column(soup)

    for a in page.find_all("a", href=True):
        href = a["href"]
        if "sample" in href.lower():
            continue
        if FILE_RE.search(href):
            return "href", href, a.get_text(strip=True) or Path(href).name
        m = re.search(r"""__doPostBack\(\s*['"]([^'"]+)""", href)
        if m and re.search(r"download|pre.?race|daily|file", a.get_text(), re.I):
            return "postback", m.group(1), a.get_text(strip=True)

    for i in page.find_all("input", {"type": ["submit", "image", "button"]}):
        label = i.get("value", "")
        if i.get("name") and re.search(r"download", label + (i.get("id") or ""), re.I):
            return "submit", i["name"], label

    return None, None, None


def fetch(session, soup, kind, target, label):
    if kind == "href":
        from urllib.parse import urljoin

        return session.get(urljoin(TODAY_URL, target), headers={"Referer": TODAY_URL})
    if kind == "submit":
        form = hidden_fields(soup)
        form[target] = label or "Download"
        return session.post(TODAY_URL, data=form, headers={"Referer": TODAY_URL})
    # postback defaults to the results page; soup came from /today/, and a
    # __VIEWSTATE is only valid for the page that issued it
    return postback(session, soup, target, url=TODAY_URL)


def download_today(session, skip_existing=False):
    resp, soup = today_page(session)
    day = file_date(soup)

    kind, target, label = find_download(soup)
    if kind is None:
        warning = soup.find(id=SIGNIN_WARNING_ID)
        note = warning.get_text(strip=True) if warning else ""
        if not note:
            text = soup.get_text(" ", strip=True)
            if re.search(r"Advanced Members only", text, re.I):
                note = "This feature is for Advanced Members only"
        raise DownloadError(
            "no pre-race download control on the page"
            + (f" -- site says {note!r}" if note else "")
            + f" (response saved to {save_debug(resp.text)})"
        )

    # The pre-race file is regenerated as declarations and non-runners change,
    # so a re-run normally replaces it. --skip-existing is for a cron job that
    # only wants the first copy of the day.
    if skip_existing and day:
        existing = expected_path(day)
        if existing.exists():
            print(f"{existing}  (have it)")
            return existing

    r = fetch(session, soup, kind, target, label)
    dest = month_dir(day) / prerace_name(filename_from(r, None), day)
    if skip_existing and dest.exists():
        print(f"{dest}  (have it)")
        return dest
    return save(r, dest)


def prerace_name(server_name, day=None):
    """What to call a pre-race workbook on disk: "Daily - 30092025.xlsx".

    The single place both `today` and the archive backfill get their filenames
    from, so the two can never file the same race day under two different names.

    Neither source offers a usable name. The today endpoint sends
    `Content-Disposition: filename=Daily.xlsx` every single day with no date in
    it, so saving under that would overwrite yesterday's file and leave a folder
    you cannot tell apart. The month archive lists the same workbooks as
    `Daily30092025.xlsx`, which does carry the date but in a different shape --
    and a backfill that saved those would refetch every day `today` had already
    filed under the spaced name. So the day is always stamped on in the one
    shape, matching how the results tool names its downloads
    ("Results - 06092026.xlsx").
    """
    ext = Path(server_name).suffix if server_name else ".xlsx"
    day = day or file_day(server_name or "")
    if day:
        return f"Daily - {day:%d%m%Y}{ext}"
    return safe_name(server_name or "Daily.xlsx")


PRERACE_ARCHIVE = Archive(TODAY_URL, OUTDIR, prerace_name)


def month_dir(day):
    """data/pre-race/<YYYY-MM>, mirroring the results layout."""
    if day is None:
        return OUTDIR / "unknown"
    return OUTDIR / f"{day.year:04d}-{day.month:02d}"


def expected_path(day):
    """Where today's file lands, without having to make the request first."""
    return month_dir(day) / prerace_name(None, day)


def save_debug(text):
    path = OUTDIR.parent / "debug" / "rbd-prerace-today.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def download_sample(session):
    return save(session.get(SAMPLE_URL), OUTDIR / "RBD-Daily-Sample.xlsx")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command", nargs="?", default="today",
                    choices=["today", "sample", "months", "backfill", "archive"],
                    help="default: today")
    ap.add_argument("values", nargs="*",
                    help="month values for `archive` (see `months`)")
    ap.add_argument("--delay", type=float, default=None,
                    help=f"seconds between requests (default {DEFAULT_DELAY}, or $RBD_DELAY)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="do nothing if today's file is already on disk")
    ap.add_argument("--from", dest="since", metavar="DATE",
                    help="backfill from this race day towards today"
                         " (DD/MM/YYYY or YYYY-MM-DD)")
    ap.add_argument("--force", action="store_true",
                    help="re-download files already on disk")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched, in order, and download nothing")
    args = ap.parse_args(argv)

    import os

    delay = args.delay if args.delay is not None else float(os.getenv("RBD_DELAY", DEFAULT_DELAY))

    since = parse_date(args.since) if args.since else None
    if since and since > datetime.now().date():
        raise SystemExit(f"--from {since:%d/%m/%Y} is in the future -- nothing to fill")

    arc = PRERACE_ARCHIVE
    try:
        if args.command == "sample":
            download_sample(session_from_env(delay, anonymous=True))
        elif args.command == "months":
            # the dropdown renders logged out; only the files behind it need auth
            for value, label in list_months(session_from_env(delay, anonymous=True), arc):
                print(f"{value:<16} {label}")
        elif args.command == "backfill":
            return backfill(session_from_env(delay), arc, force=args.force,
                            since=since, dry_run=args.dry_run)
        elif args.command == "archive":
            if not args.values:
                raise SystemExit("usage: archive <month-value> [...]  (see `months`)")
            session = session_from_env(delay)
            for value in args.values:
                download_archive(session, arc, value, force=args.force,
                                 since=since, dry_run=args.dry_run)
        else:
            download_today(session_from_env(delay), skip_existing=args.skip_existing)
    except DownloadError as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
