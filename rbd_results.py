#!/usr/bin/env python3
"""Download results Excel files from racing-bet-data.com.

The site is ASP.NET WebForms, so every stateful action is a form POST that
echoes back __VIEWSTATE / __EVENTVALIDATION from the page you were just on.
There is no API and no JS challenge -- a requests.Session is enough.

Credentials come from RBD_USER / RBD_PASS (Advanced membership required for
anything except the public sample). The site says it monitors download volume
per IP, so every request is throttled and backfills resume rather than refetch.

    python rbd_results.py sample                 # no login needed
    python rbd_results.py months                 # no login needed
    python rbd_results.py today
    python rbd_results.py archive 26H_Aug-26 26G_Jul-26
    python rbd_results.py backfill               # all months, skipping done
    python rbd_results.py backfill --from 01/09/2025

    --from DATE   start a backfill here and work towards today (DD/MM/YYYY
                  or YYYY-MM-DD). Without it a backfill starts at the
                  earliest month the site offers
    --dry-run     list what would be fetched, in order, and download nothing
    --delay N     seconds between requests (default 3, or $RBD_DELAY)
    --force       re-download files already on disk

Backfills run oldest month first so they always proceed towards the current
date, whether or not --from is given.

Each archived month holds one file per race day, so they land as
data/results/<YYYY-MM>/Results - 04092026.xlsx under the site's own names.
"""

import os
import random
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

from _venv import use_venv

use_venv()  # must precede the third-party imports below

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

BASE = "https://www.racing-bet-data.com"
RESULTS_URL = f"{BASE}/results/"
SIGNIN_URL = f"{BASE}/signin/"
SAMPLE_URL = f"{BASE}/exceldl/RBD-Results-Sample.xlsx"
OUTDIR = Path("data/results")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DEFAULT_DELAY = 3.0

# the sidebar panel a selected month's daily files render into. scoping link
# discovery to this is what keeps us off the sample link in the main column.
ARC_PANEL_ID = "sidebar_arcPanel"
SIGNIN_WARNING_ID = "sidebar_Label3"
FILE_RE = re.compile(r"\.(xlsx|xlsm|xls|zip|csv)$", re.I)
COMPLETE_MARKER = ".complete"
FILE_DATE_RE = re.compile(r"latest file for download is:\s*(\d{2}/\d{2}/\d{4})", re.I)
# the DDMMYYYY stamp both tools' daily files carry: "Results - 04092026.xlsx"
# from the results archive, "Daily04092026.xlsx" from the pre-race one
FILE_DAY_RE = re.compile(r"(\d{2})(\d{2})(\d{4})")


class DownloadError(Exception):
    """One file failed. A backfill logs these and keeps going."""


class Archive:
    """One tool's month archive: the page it is on, where files land, how the
    files are named.

    /results/ and /today/ are different pages of the same WebForms app and
    render the same sidebar controls -- #sidebar_monthsDDL and #sidebar_arcPanel
    -- listing the same month values ('25I_Sep-25'). So one set of archive
    functions serves both tools and only these three things differ.

    name_for(link_text, day) decides the filename on disk. The results archive
    already names its links usefully, but the pre-race one serves
    'Daily04092026.xlsx' where rbd_prerace's own `today` download saves
    'Daily - 04092026.xlsx'; without a hook here a backfill would refetch days
    already on disk under a second spelling.
    """

    def __init__(self, url, outdir, name_for=None):
        self.url = url
        self.outdir = outdir
        self.name_for = name_for or (lambda link_text, day: safe_name(link_text))


RESULTS_ARCHIVE = Archive(RESULTS_URL, OUTDIR)


class ThrottledSession(requests.Session):
    """Paces every request, so no call site can accidentally hammer the site."""

    def __init__(self, delay=DEFAULT_DELAY):
        super().__init__()
        self.delay = delay
        self._last = 0.0

    def request(self, *args, **kwargs):
        if self.delay:
            # jitter keeps a long backfill from looking like a metronome
            wait = self._last + self.delay * random.uniform(0.85, 1.15) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        try:
            return super().request(*args, **kwargs)
        finally:
            self._last = time.monotonic()


def hidden_fields(soup):
    """Every __VIEWSTATE-style field on the page, which a POST must echo back."""
    return {
        i["name"]: i.get("value", "")
        for i in soup.select("input[type=hidden][name]")
    }


def new_session(delay=DEFAULT_DELAY):
    s = ThrottledSession(delay)
    s.headers["User-Agent"] = UA
    return s


def sign_in(session, user, password):
    soup = BeautifulSoup(session.get(SIGNIN_URL).text, "html.parser")
    form = hidden_fields(soup)
    form.update(
        {
            "ctl00$ContentPlaceHolder2$unameTextBox": user,
            "ctl00$ContentPlaceHolder2$pwordTextBox": password,
            "ctl00$ContentPlaceHolder2$submitButton": "Sign In",
        }
    )
    r = session.post(SIGNIN_URL, data=form, headers={"Referer": SIGNIN_URL})
    r.raise_for_status()
    page = r.text.lower()

    # "My Account" is in the nav menu whether or not you are signed in, so the
    # old check -- no "signout" AND no "my account" -- could never be true and
    # a bad password reported "sign-in successful", then failed further on with
    # a confusing message about the download control. What actually
    # distinguishes a rejection is that the site re-renders the login form:
    # verified by posting a deliberately wrong password.
    if "signout" not in page and "pwordtextbox" in page:
        raise SystemExit("sign-in failed -- check RBD_USER / RBD_PASS")
    print("signed in")
    return session


def session_from_env(delay=DEFAULT_DELAY, anonymous=False):
    s = new_session(delay)
    if anonymous:
        return s
    user, password = os.getenv("RBD_USER"), os.getenv("RBD_PASS")
    if not user or not password:
        raise SystemExit("set RBD_USER and RBD_PASS")
    return sign_in(s, user, password)


def save(response, path):
    """Write a response to disk, refusing anything that isn't actually a workbook."""
    ctype = response.headers.get("Content-Type", "")
    if XLSX_MIME not in ctype and "excel" not in ctype and "octet-stream" not in ctype:
        raise DownloadError(
            f"expected a spreadsheet, got {ctype!r} "
            "(usually means the session is not signed in or lacks Advanced access)"
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # write via a temp file so an interrupted run never leaves a half file
    # that the next run would mistake for a completed download
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(response.content)
    tmp.replace(path)
    print(f"{path}  ({len(response.content):,} bytes)")
    return path


def safe_name(name):
    """A server-supplied filename, stripped of anything that could escape OUTDIR."""
    name = Path(name.strip()).name
    return re.sub(r"[^\w \-.()]", "_", name) or "unnamed.xlsx"


def month_start(label):
    """'Sep-26' -> date(2026, 9, 1), or None if the label is not a month.

    Sorting months on this is what lets a backfill run oldest-first, and so
    proceed towards the current date rather than away from it: the site's
    dropdown renders newest-first.
    """
    try:
        return datetime.strptime(label, "%b-%y").date().replace(day=1)
    except ValueError:
        return None


def file_day(name):
    """The race day a daily file is for, or None.

    'Results - 04092026.xlsx' and 'Daily04092026.xlsx' both carry DDMMYYYY,
    which is what lets --from filter within a month rather than only between
    them.
    """
    m = FILE_DAY_RE.search(name)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def parse_date(text):
    """A --from date, in either the site's format or ISO."""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    raise SystemExit(f"unrecognised date {text!r} -- try DD/MM/YYYY or YYYY-MM-DD")


def month_dir(arc, label):
    """'Sep-26' -> <outdir>/2026-09, so months sort chronologically on disk."""
    start = month_start(label)
    if start is None:
        return arc.outdir / safe_name(label)
    return arc.outdir / f"{start.year:04d}-{start.month:02d}"


def is_current_month(path):
    return path.name == datetime.now().strftime("%Y-%m")


def save_debug(text, value):
    """Keep the HTML behind a parse failure so the panel markup can be inspected."""
    path = OUTDIR.parent / "debug" / f"rbd-{safe_name(value)}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def download_sample(session):
    return save(session.get(SAMPLE_URL), OUTDIR / "RBD-Results-Sample.xlsx")


def archive_page(session, arc):
    r = session.get(arc.url)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def list_months(session, arc):
    """Archive dropdown values, e.g. ('26H_Aug-26', 'Aug-26')."""
    soup = archive_page(session, arc)
    ddl = soup.find("select", id="sidebar_monthsDDL")
    if ddl is None:
        raise SystemExit("month dropdown not found -- page layout changed")
    return [
        (o["value"], o.get_text(strip=True))
        for o in ddl.find_all("option")
        if not o.has_attr("disabled")
    ]


def postback(session, soup, target, extra=None, url=RESULTS_URL):
    """Replay an __doPostBack against the page `soup` came from.

    `url` has to match that page: a __VIEWSTATE is only valid for the page that
    issued it, so posting the /today/ page's back to /results/ is rejected.
    """
    form = hidden_fields(soup)
    form["__EVENTTARGET"] = target
    form["__EVENTARGUMENT"] = ""
    form.update(extra or {})
    return session.post(
        url,
        data=form,
        headers={"Referer": url},
        allow_redirects=True,
    )


def download_today(session):
    """Trigger the daily download button and save whatever file comes back.

    Never skipped: this file is rewritten through the day as races run.

    Filed under data/results/<YYYY-MM>/ with the date stamped on, exactly as
    the archive does. The server sends `filename=results.xlsx` with no date in
    it, so saving under the server's name dropped a single undated file in the
    root of data/results/ that every subsequent run overwrote, and that
    rbd_import.py would then load under a filename carrying no date. The date
    is taken from the page rather than the clock, so a run just after midnight
    still files the workbook the site is actually offering.
    """
    soup = archive_page(session, RESULTS_ARCHIVE)
    button = soup.find(
        lambda t: t.name in ("input", "a")
        and re.search(r"download", t.get("value", "") + t.get_text(), re.I)
        and "dl" in (t.get("id") or "").lower()
    )
    if button is None:
        raise DownloadError(
            "download control not found -- sign in first, or the page changed"
        )
    name = button.get("name") or button["id"].replace("_", "$")
    if button.name == "a":
        r = postback(session, soup, name)
    else:
        form = hidden_fields(soup)
        form[name] = button.get("value", "Download")
        r = session.post(RESULTS_URL, data=form, headers={"Referer": RESULTS_URL})
    day = page_file_date(soup)
    if day:
        dest = OUTDIR / f"{day.year:04d}-{day.month:02d}" / f"Results - {day:%d%m%Y}.xlsx"
    else:
        dest = OUTDIR / safe_name(filename_from(r, "rbd-results-today.xlsx"))
    return save(r, dest)


def page_file_date(soup):
    """The date the page says its current file is for, or None.

    "The date of the latest file for download is: 04/09/2026 22:24:22"
    """
    m = FILE_DATE_RE.search(soup.get_text(" ", strip=True))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def select_month(session, arc, value):
    """Postback the month dropdown. Returns (response, page, arcPanel)."""
    r = postback(
        session,
        archive_page(session, arc),
        "ctl00$sidebar$monthsDDL",
        {"ctl00$sidebar$monthsDDL": value},
        url=arc.url,
    )
    page = BeautifulSoup(r.text, "html.parser")
    return r, page, page.find(id=ARC_PANEL_ID)


def panel_files(panel):
    """The daily files a month's panel lists, as (link text, (kind, target)).

    Only ever looks inside #sidebar_arcPanel -- searching the whole page picks
    up the sample-file link in the main column instead.
    """
    files = []
    for a in panel.find_all("a"):
        href = a.get("href", "")
        if FILE_RE.search(href):
            files.append((Path(href).name, ("href", href)))
            continue
        # bs4 has already unescaped &#39; to a real quote by the time we see it
        m = re.search(r"""__doPostBack\(\s*['"]([^'"]+)""", href)
        if m:
            files.append((a.get_text(strip=True), ("postback", m.group(1))))
    # some WebForms skins render the same links as submit buttons
    for i in panel.find_all("input", {"type": ["submit", "image", "button"]}):
        if i.get("name"):
            files.append((i.get("value") or i["name"], ("submit", i["name"])))
    return files


def fetch_panel_file(session, arc, value, kind, target, link_text):
    """Retrieve one file from a month panel, by link or by postback."""
    if kind == "href":
        return session.get(urljoin(arc.url, target), headers={"Referer": arc.url})

    # a postback consumes the VIEWSTATE it was issued with, so re-select the
    # month to get a fresh one before asking for each file
    _, page, _ = select_month(session, arc, value)
    extra = {"ctl00$sidebar$monthsDDL": value}
    if kind == "submit":
        form = hidden_fields(page)
        form.update(extra)
        form[target] = link_text
        return session.post(arc.url, data=form, headers={"Referer": arc.url})
    return postback(session, page, target, extra, url=arc.url)


def download_archive(session, arc, value, label=None, force=False,
                     since=None, dry_run=False):
    """Download every daily file listed for one archived month.

    A month holds one file per race day ("Results - 04092026.xlsx"), so these
    land in <outdir>/<YYYY-MM>/ named by arc.name_for.

    `since` drops files for race days before that date, which is what gives
    --from day precision rather than only month precision. In practice only the
    first month of a backfill has any such days, so applying it to every month
    costs nothing and keeps the caller simple.
    """
    label = label or value.split("_", 1)[-1]
    dest = month_dir(arc, label)
    marker = dest / COMPLETE_MARKER
    if not force and marker.exists():
        print(f"{dest}/  (complete)")
        return []

    r, page, panel = select_month(session, arc, value)
    if panel is None:
        raise DownloadError(
            f"{label}: panel #{ARC_PANEL_ID} missing -- page layout changed"
        )

    files = panel_files(panel)
    if not files:
        warning = page.find(id=SIGNIN_WARNING_ID)
        note = warning.get_text(strip=True) if warning else ""
        raise DownloadError(
            f"{label}: panel listed no files"
            + (f" -- site says {note!r}" if note else "")
            + f" (response saved to {save_debug(r.text, value)})"
        )

    # oldest day first, to match the month order a backfill walks in. A day
    # whose date cannot be read sorts last rather than being dropped.
    files.sort(key=lambda f: (file_day(f[0]) is None, file_day(f[0]) or date.min))

    wanted, before = [], 0
    for link_text, how in files:
        day = file_day(link_text)
        if since and day and day < since:
            before += 1
            continue
        wanted.append((link_text, how, day))

    note = f", {before} before {since:%d/%m/%Y}" if before else ""
    print(f"{label}: {len(wanted)} file(s){note}")

    got, failed = [], []
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for link_text, (kind, target), day in wanted:
        name = arc.name_for(link_text, day)
        path = dest / name
        if not force and path.exists():
            print(f"{path}  (have it)")
            got.append(path)
            continue
        if dry_run:
            print(f"{path}  (would fetch)")
            got.append(path)
            continue
        try:
            resp = fetch_panel_file(session, arc, value, kind, target, link_text)
            if not FILE_RE.search(name):
                path = dest / safe_name(filename_from(resp, name + ".xlsx"))
            got.append(save(resp, path))
        except (DownloadError, requests.RequestException) as e:
            print(f"  {link_text}: {e}", file=sys.stderr)
            failed.append(link_text)

    if failed:
        raise DownloadError(f"{label}: {len(failed)} of {len(wanted)} file(s) failed")
    # a past month never gains new days, so mark it done and skip it next run.
    # the current month deliberately stays unmarked so new days get picked up,
    # and neither does a month --from only took part of -- marking that complete
    # would make a later full backfill skip the days it never fetched.
    if not dry_run and not before and not is_current_month(dest):
        marker.write_text(f"{len(got)} files\n")
    return got


def backfill(session, arc, force=False, since=None, dry_run=False):
    """Every archived month from `since` onwards, skipping completed ones.

    Oldest month first, so the run proceeds towards the current date. The site's
    dropdown is newest-first, which is the opposite of what you want when
    filling a gap. Safe to re-run after a failure.
    """
    months = list_months(session, arc)
    # unparseable labels sort last rather than being dropped: better to fetch a
    # month we cannot date than to silently ignore it
    months.sort(key=lambda m: (month_start(m[1]) is None, month_start(m[1]) or date.min))

    todo = months
    if since:
        todo = [m for m in months
                if month_start(m[1]) is None or month_start(m[1]) >= since.replace(day=1)]
        print(f"{len(months)} months, {len(todo)} from {since:%d/%m/%Y} onwards")
    else:
        print(f"{len(months)} months")
    if dry_run:
        print("dry run -- nothing will be downloaded")
    print()

    failed = []
    for value, label in todo:
        try:
            download_archive(session, arc, value, label,
                             force=force, since=since, dry_run=dry_run)
        except (DownloadError, requests.RequestException) as e:
            print(f"{e}", file=sys.stderr)
            failed.append(label)

    print(f"\ndone -- {len(todo) - len(failed)} ok, {len(failed)} failed")
    if failed:
        print(f"failed: {', '.join(failed)}\nre-run to retry just these", file=sys.stderr)
        return 1
    return 0


def filename_from(response, fallback):
    match = re.search(
        r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
        response.headers.get("Content-Disposition", ""),
    )
    return match.group(1) if match else fallback


def take_option(argv, name):
    """Pull `--name VALUE` out of argv, returning the value or None."""
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv):
        raise SystemExit(f"{name} needs a value")
    value = argv[i + 1]
    del argv[i : i + 2]
    return value


def main(argv):
    argv = list(argv)
    force = "--force" in argv
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a not in ("--force", "--dry-run")]

    delay = float(os.getenv("RBD_DELAY", DEFAULT_DELAY))
    given = take_option(argv, "--delay")
    if given is not None:
        delay = float(given)

    since = take_option(argv, "--from")
    since = parse_date(since) if since is not None else None
    if since and since > date.today():
        raise SystemExit(f"--from {since:%d/%m/%Y} is in the future -- nothing to fill")

    cmd = argv[0] if argv else "sample"
    rest = argv[1:]
    arc = RESULTS_ARCHIVE

    try:
        if cmd == "sample":
            download_sample(session_from_env(delay, anonymous=True))
        elif cmd == "months":
            # the dropdown renders logged out; only the files behind it need auth
            for value, label in list_months(session_from_env(delay, anonymous=True), arc):
                print(f"{value:<16} {label}")
        elif cmd == "today":
            download_today(session_from_env(delay))
        elif cmd == "archive":
            if not rest:
                raise SystemExit("usage: archive <month-value> [...]  (see `months`)")
            session = session_from_env(delay)
            for value in rest:
                download_archive(session, arc, value, force=force,
                                 since=since, dry_run=dry_run)
        elif cmd == "backfill":
            return backfill(session_from_env(delay), arc,
                            force=force, since=since, dry_run=dry_run)
        else:
            raise SystemExit(__doc__)
    except DownloadError as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
