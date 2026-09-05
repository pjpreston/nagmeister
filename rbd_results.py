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

    --delay N   seconds between requests (default 3, or $RBD_DELAY)
    --force     re-download files already on disk

Each archived month holds one file per race day, so they land as
data/results/<YYYY-MM>/Results - 04092026.xlsx under the site's own names.
"""

import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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


class DownloadError(Exception):
    """One file failed. A backfill logs these and keeps going."""


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
    if "signout" not in r.text.lower() and "my account" not in r.text.lower():
        raise SystemExit("sign-in failed -- check RBD_USER / RBD_PASS")
    else:
        print("sign-in successful")
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


def month_dir(label):
    """'Sep-26' -> data/results/2026-09, so months sort chronologically on disk."""
    try:
        d = datetime.strptime(label, "%b-%y")
    except ValueError:
        return OUTDIR / safe_name(label)
    return OUTDIR / f"{d.year:04d}-{d.month:02d}"


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


def results_page(session):
    r = session.get(RESULTS_URL)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def list_months(session):
    """Archive dropdown values, e.g. ('26H_Aug-26', 'Aug-26')."""
    soup = results_page(session)
    ddl = soup.find("select", id="sidebar_monthsDDL")
    if ddl is None:
        raise SystemExit("month dropdown not found -- page layout changed")
    return [
        (o["value"], o.get_text(strip=True))
        for o in ddl.find_all("option")
        if not o.has_attr("disabled")
    ]


def postback(session, soup, target, extra=None):
    """Replay an __doPostBack against the results page."""
    form = hidden_fields(soup)
    form["__EVENTTARGET"] = target
    form["__EVENTARGUMENT"] = ""
    form.update(extra or {})
    return session.post(
        RESULTS_URL,
        data=form,
        headers={"Referer": RESULTS_URL},
        allow_redirects=True,
    )


def download_today(session):
    """Trigger the daily download button and save whatever file comes back.

    Never skipped: this file is rewritten through the day as races run.
    """
    soup = results_page(session)
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
    return save(r, OUTDIR / safe_name(filename_from(r, "rbd-results-today.xlsx")))


def select_month(session, value):
    """Postback the month dropdown. Returns (response, page, arcPanel)."""
    r = postback(
        session,
        results_page(session),
        "ctl00$sidebar$monthsDDL",
        {"ctl00$sidebar$monthsDDL": value},
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


def fetch_panel_file(session, value, kind, target, link_text):
    """Retrieve one file from a month panel, by link or by postback."""
    if kind == "href":
        return session.get(urljoin(RESULTS_URL, target), headers={"Referer": RESULTS_URL})

    # a postback consumes the VIEWSTATE it was issued with, so re-select the
    # month to get a fresh one before asking for each file
    _, page, _ = select_month(session, value)
    extra = {"ctl00$sidebar$monthsDDL": value}
    if kind == "submit":
        form = hidden_fields(page)
        form.update(extra)
        form[target] = link_text
        return session.post(RESULTS_URL, data=form, headers={"Referer": RESULTS_URL})
    return postback(session, page, target, extra)


def download_archive(session, value, label=None, force=False):
    """Download every daily file listed for one archived month.

    A month holds one file per race day ("Results - 04092026.xlsx"), so these
    land in data/results/<YYYY-MM>/ under the site's own names.
    """
    label = label or value.split("_", 1)[-1]
    dest = month_dir(label)
    marker = dest / COMPLETE_MARKER
    if not force and marker.exists():
        print(f"{dest}/  (complete)")
        return []

    r, page, panel = select_month(session, value)
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

    dest.mkdir(parents=True, exist_ok=True)
    got, failed = [], []
    print(f"{label}: {len(files)} file(s)")
    for link_text, (kind, target) in files:
        name = safe_name(link_text)
        path = dest / name
        if not force and path.exists():
            print(f"{path}  (have it)")
            got.append(path)
            continue
        try:
            resp = fetch_panel_file(session, value, kind, target, link_text)
            if not FILE_RE.search(name):
                path = dest / safe_name(filename_from(resp, name + ".xlsx"))
            got.append(save(resp, path))
        except (DownloadError, requests.RequestException) as e:
            print(f"  {link_text}: {e}", file=sys.stderr)
            failed.append(link_text)

    if failed:
        raise DownloadError(f"{label}: {len(failed)} of {len(files)} file(s) failed")
    # a past month never gains new days, so mark it done and skip it next run.
    # the current month deliberately stays unmarked so new days get picked up.
    if not is_current_month(dest):
        marker.write_text(f"{len(got)} files\n")
    return got


def backfill(session, force=False):
    """Every archived month, skipping completed ones. Safe to re-run after a failure."""
    months = list_months(session)
    todo = [m for m in months if force or not (month_dir(m[1]) / COMPLETE_MARKER).exists()]
    print(f"{len(months)} months, {len(todo)} to check\n")

    failed = []
    for value, label in months:
        try:
            download_archive(session, value, label, force=force)
        except (DownloadError, requests.RequestException) as e:
            print(f"{e}", file=sys.stderr)
            failed.append(label)

    print(f"\ndone -- {len(months) - len(failed)} ok, {len(failed)} failed")
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


def main(argv):
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]

    delay = float(os.getenv("RBD_DELAY", DEFAULT_DELAY))
    if "--delay" in argv:
        i = argv.index("--delay")
        delay = float(argv[i + 1])
        del argv[i : i + 2]

    cmd = argv[0] if argv else "sample"
    rest = argv[1:]

    try:
        if cmd == "sample":
            download_sample(session_from_env(delay, anonymous=True))
        elif cmd == "months":
            # the dropdown renders logged out; only the files behind it need auth
            for value, label in list_months(session_from_env(delay, anonymous=True)):
                print(f"{value:<16} {label}")
        elif cmd == "today":
            download_today(session_from_env(delay))
        elif cmd == "archive":
            if not rest:
                raise SystemExit("usage: archive <month-value> [...]  (see `months`)")
            session = session_from_env(delay)
            for value in rest:
                download_archive(session, value, force=force)
        elif cmd == "backfill":
            return backfill(session_from_env(delay), force=force)
        else:
            raise SystemExit(__doc__)
    except DownloadError as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
