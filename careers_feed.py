#!/usr/bin/env python3
"""Daily feed of new jobs from Workable, Recruitee and Teamtailor career pages, written to CSV.

Reads the public job-board data each ATS publishes for company career pages:

  workable    GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=false
  recruitee   GET https://{company}.recruitee.com/api/offers/
              (on 401/403, the public XML feed https://{company}.recruitee.com/api/feeds/offers.xml)
  teamtailor  GET https://{host}/jobs.json, following next_url

Standard library only. Every job gets a stable key, uid = "<ats>:<board>:<job id>", and a job is
new when its uid is not already in data/jobs.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = os.environ.get(
    "JOBS_FEED_USER_AGENT",
    "careers-page-jobs-feed/1.0 (+https://github.com/fecklesspleb/careers-page-jobs-feed)",
)
COLUMNS = ["uid", "ats", "board", "company", "title", "location", "workplace", "department",
           "employment_type", "posted_at", "url", "first_seen"]
# Seconds between two requests to the same host. apply.workable.com rate-limits hard, so it gets 5 s.
MIN_INTERVAL = {"apply.workable.com": 5.0}
DEFAULT_INTERVAL = 1.0
MAX_CRAWL_DELAY = 30.0
TEAMTAILOR_MAX_PAGES = 50
REFUSED = {
    "smartrecruiters": "SmartRecruiters is not supported: robots.txt on api.smartrecruiters.com "
                       "disallows every user agent except LinkedInBot.",
}


# ---------------------------------------------------------------- HTTP

class Response:
    def __init__(self, url: str, status: int, headers: dict, body: bytes):
        self.url, self.status, self.body = url, status, body
        self.headers = {k.lower(): v for k, v in headers.items()}

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


def http(url: str, accept: str = "application/json", timeout: float = 30) -> Response:
    """GET. Returns a Response for 2xx, raises urllib.error.HTTPError otherwise. Follows redirects."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return Response(r.geturl(), r.status, dict(r.headers), r.read())


class HostBlocked(Exception):
    """The host rate-limited us (HTTP 429, Cloudflare error 1015) or its robots.txt could not be read.
    Every later board on that host is skipped for the rest of the run."""


class Disallowed(Exception):
    pass


class NotFound(Exception):
    pass


def _error_body(e: urllib.error.HTTPError) -> str:
    try:
        return e.read(2000).decode("utf-8", "replace")
    except Exception:
        return ""


class Client:
    """Per-host politeness: robots.txt (checked once per host, fails closed), a minimum gap between
    requests to the same host, Crawl-delay when it is longer, and a circuit breaker on HTTP 429."""

    def __init__(self, transport=http, sleep=time.sleep, clock=time.monotonic, intervals=None):
        self.transport, self.sleep, self.clock = transport, sleep, clock
        self.intervals = dict(MIN_INTERVAL if intervals is None else intervals)
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.blocked: dict[str, str] = {}
        self.last: dict[str, float] = {}
        self.requests = 0

    def _wait(self, host: str) -> None:
        gap = self.intervals.get(host, DEFAULT_INTERVAL)
        rp = self.robots.get(host)
        cd = rp.crawl_delay(USER_AGENT) if rp else None
        if cd:
            gap = max(gap, min(float(cd), MAX_CRAWL_DELAY))
        if host in self.last:
            left = self.last[host] + gap - self.clock()
            if left > 0:
                self.sleep(left)

    def _send(self, url: str, accept: str) -> Response:
        host = urllib.parse.urlsplit(url).hostname or ""
        self._wait(host)
        self.requests += 1
        try:
            return self.transport(url, accept=accept)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                body = _error_body(e)
                why = "HTTP 429" + (" (Cloudflare error 1015)" if "1015" in body else "")
                self.blocked[host] = why
                raise HostBlocked(f"{host} rate-limited this run: {why}") from None
            raise
        finally:
            self.last[host] = self.clock()

    def _robots(self, host: str) -> urllib.robotparser.RobotFileParser:
        if host in self.blocked:
            raise HostBlocked(f"{host} skipped for the rest of this run: {self.blocked[host]}")
        if host in self.robots:
            return self.robots[host]
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = self._send(f"https://{host}/robots.txt", accept="text/plain")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                rp.parse([])  # RFC 9309: an unavailable robots.txt (4xx) means no restrictions
                self.robots[host] = rp
                return rp
            self.blocked[host] = f"robots.txt answered HTTP {e.code}"
            raise HostBlocked(f"{host}: robots.txt answered HTTP {e.code}; skipped") from None
        except urllib.error.URLError as e:
            self.blocked[host] = f"robots.txt unreachable ({e.reason})"
            raise HostBlocked(f"{host}: robots.txt unreachable ({e.reason}); skipped") from None
        if r.content_type == "text/html" or r.body.lstrip()[:1] == b"<":
            # Unknown Recruitee names redirect robots.txt to recruitee.com's home page.
            self.blocked[host] = "robots.txt answered an HTML page"
            raise HostBlocked(f"{host}: robots.txt answered an HTML page, not a robots file "
                              f"(the board may not exist); skipped")
        rp.parse(r.body.decode("utf-8", "replace").splitlines())
        self.robots[host] = rp
        return rp

    def get(self, url: str, accept: str = "application/json") -> Response:
        host = urllib.parse.urlsplit(url).hostname or ""
        rp = self._robots(host)
        if not rp.can_fetch(USER_AGENT, url):
            raise Disallowed(f"robots.txt on {host} disallows {urllib.parse.urlsplit(url).path}")
        if host in self.blocked:
            raise HostBlocked(f"{host} skipped for the rest of this run: {self.blocked[host]}")
        try:
            return self._send(url, accept)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(f"{url} answered 404 (wrong board name, or the company left this ATS)") from None
            if e.code >= 500:  # one retry for a server error
                self.sleep(3)
                return self._send(url, accept)
            raise

    def get_json(self, url: str):
        r = self.get(url)
        try:
            return json.loads(r.body)
        except ValueError:
            raise ValueError(f"{url} did not answer JSON (content-type {r.content_type or 'none'})") from None


# ---------------------------------------------------------------- boards

class Board:
    def __init__(self, ats: str, slug: str, host: str, raw: str):
        self.ats, self.slug, self.host, self.raw = ats, slug, host, raw

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug}"

    def __repr__(self) -> str:
        return f"Board({self.key})"


SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _board(ats: str, value: str, raw: str) -> Board:
    value = value.strip().strip("/")
    if ats == "workable":
        if not SLUG.match(value):
            raise ValueError(f"{raw}: not a Workable account name")
        return Board("workable", value, "apply.workable.com", raw)
    if ats == "recruitee":
        if not SLUG.match(value):
            raise ValueError(f"{raw}: not a Recruitee company name")
        v = value.lower()
        return Board("recruitee", v, f"{v}.recruitee.com", raw)
    if ats == "teamtailor":
        v = value.lower()
        if SLUG.match(v):  # teamtailor:acme means acme.teamtailor.com
            return Board("teamtailor", v, f"{v}.teamtailor.com", raw)
        if HOST.match(v):  # a career site on the company's own domain, served by Teamtailor
            slug = v[: -len(".teamtailor.com")] if v.endswith(".teamtailor.com") else v
            return Board("teamtailor", slug, v, raw)
        raise ValueError(f"{raw}: not a Teamtailor name or host")
    raise ValueError(f"{raw}: unknown ATS {ats!r}")


def parse_board(line: str) -> Board:
    """Accepts `ats:name` (workable:huggingface, recruitee:bunq, teamtailor:proxima,
    teamtailor:jobs.tibber.com) or a board URL copied from the browser."""
    raw = line.strip()
    m = re.match(r"^([a-z]+)\s*:\s*(?!//)(.+)$", raw, re.I)
    if m and not raw.lower().startswith(("http:", "https:")):
        ats = m.group(1).lower()
        if ats in REFUSED:
            raise ValueError(f"{raw}: {REFUSED[ats]}")
        return _board(ats, m.group(2), raw)
    u = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if host == "apply.workable.com" and parts and parts[0] not in ("api", "j"):
        return _board("workable", parts[0], raw)
    if host.endswith(".workable.com") and host.split(".")[0] not in ("apply", "www", "jobs"):
        return _board("workable", host.split(".")[0], raw)
    if host.endswith(".recruitee.com") and host.count(".") == 2:
        return _board("recruitee", host.split(".")[0], raw)
    if host.endswith(".teamtailor.com") and host.split(".")[0] not in ("www", "app", "api", "support", "partner"):
        return _board("teamtailor", host, raw)
    if "smartrecruiters.com" in host:
        raise ValueError(f"{raw}: {REFUSED['smartrecruiters']}")
    raise ValueError(f"{raw}: not a Workable, Recruitee or Teamtailor board "
                     f"(for a Teamtailor site on its own domain write teamtailor:<host>)")


def read_sites(path: Path) -> list[str]:
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = re.split(r"(?:^|\s)#", ln, maxsplit=1)[0].strip()  # drop comments
        if ln:
            out.append(ln)
    return out


# ---------------------------------------------------------------- normalising

def _clean(v) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def _join(parts) -> str:
    return ", ".join(p for p in (_clean(x) for x in parts) if p)


def _unique(items) -> list[str]:
    return [x for x in dict.fromkeys(items) if x]


def _iso(value: str) -> str:
    """Normalise the three ATSs' date formats to ISO 8601.
    Workable '2026-05-29' stays a date; Recruitee '2026-10-28 13:21:58 UTC' becomes
    '2026-10-28T13:21:58+00:00'; Teamtailor already sends ISO 8601 with an offset."""
    v = _clean(value)
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC", v)
    if m:
        return f"{m.group(1)}T{m.group(2)}+00:00"
    return v


def _row(board: Board, job_id, **fields) -> dict:
    return {"uid": f"{board.ats}:{board.slug}:{job_id}", "ats": board.ats, "board": board.slug,
            **{k: _clean(v) for k, v in fields.items()}}


def workable_rows(board: Board, data: dict) -> list[dict]:
    company = data.get("name")
    rows = []
    for j in data.get("jobs") or []:
        if not j.get("shortcode") or not j.get("title"):
            continue
        locs = _unique(_join((l.get("city"), l.get("region"), l.get("country")))
                       for l in j.get("locations") or [] if not l.get("hidden"))
        main = _join((j.get("city"), j.get("state"), j.get("country")))
        locs = _unique([main, *locs])
        rows.append(_row(board, j["shortcode"], company=company, title=j["title"], location="; ".join(locs),
                         # Workable only says whether a job is remote; false does not mean on-site.
                         workplace="remote" if j.get("telecommuting") else "",
                         department=j.get("department"), employment_type=j.get("employment_type"),
                         posted_at=_iso(j.get("published_on") or j.get("created_at")),
                         url=j.get("url") or j.get("shortlink")))
    return rows


def _recruitee_workplace(o: dict) -> str:
    for flag, name in (("remote", "remote"), ("hybrid", "hybrid"), ("on_site", "onsite")):
        if o.get(flag) is True:
            return name
    return ""


def recruitee_rows(board: Board, offers: list[dict]) -> list[dict]:
    rows = []
    for o in offers:
        if o.get("status") not in (None, "published") or not o.get("id") or not o.get("title"):
            continue
        locs = _unique(_join((l.get("city") or l.get("name"), l.get("state"), l.get("country")))
                       for l in o.get("locations") or [])
        rows.append(_row(board, o["id"], company=o.get("company_name"), title=o["title"],
                         location="; ".join(locs) or o.get("location"), workplace=_recruitee_workplace(o),
                         department=o.get("department"), employment_type=o.get("employment_type_code"),
                         posted_at=_iso(o.get("published_at") or o.get("created_at")),
                         url=o.get("careers_url")))
    return rows


def _text(el, path: str):
    x = el.find(path)
    return None if x is None or x.text is None else (x.text.strip() or None)


def recruitee_xml_offers(body: bytes) -> list[dict]:
    """The public XML feed, mapped to the JSON API's offer fields this script uses.
    The feed's per-job mailbox_email is not read."""
    if b"<!DOCTYPE" in body[:2000] or b"<!ENTITY" in body[:2000]:
        raise ValueError("refusing an XML feed that declares a DOCTYPE or entities")
    root = ET.fromstring(body)
    out = []
    for o in root.iter("offer"):
        flag = lambda t: _text(o, t) == "true"  # noqa: E731
        out.append({
            "id": _text(o, "id"), "title": _text(o, "title"), "company_name": _text(o, "company_name"),
            "location": _text(o, "location"), "department": _text(o, "department"),
            "employment_type_code": _text(o, "employment_type_code"), "published_at": _text(o, "published_at"),
            "created_at": _text(o, "created_at"), "careers_url": _text(o, "careers_url"),
            "remote": flag("remote"), "hybrid": flag("hybrid"), "on_site": flag("on_site"),
            "locations": [{"city": _text(l, "city"), "name": _text(l, "name"), "state": _text(l, "state"),
                           "country": _text(l, "country")} for l in o.findall("locations/location")],
        })
    return out


def teamtailor_rows(board: Board, items: list[dict], feed_title: str | None) -> list[dict]:
    rows = []
    for it in items:
        jp = it.get("_jobposting") or {}
        url = it.get("url") or ""
        m = re.search(r"/jobs/(\d+)", url)
        jid = (jp.get("identifier") or {}).get("value") or (m.group(1) if m else None) or it.get("id")
        if not jid or not it.get("title"):
            continue
        locs = _unique(_join(((p.get("address") or {}).get("addressLocality"),
                              (p.get("address") or {}).get("addressCountry")))
                       for p in jp.get("jobLocation") or [] if isinstance(p, dict))
        et = jp.get("employmentType")
        rows.append(_row(board, jid, company=(jp.get("hiringOrganization") or {}).get("name") or feed_title,
                         title=it["title"], location="; ".join(locs),
                         # The JSON feed has no remote status (jobs.rss has <remoteStatus>), except this flag.
                         workplace="remote" if jp.get("jobLocationType") == "TELECOMMUTE" else "",
                         department="",
                         employment_type=", ".join(et) if isinstance(et, list) else et,
                         posted_at=_iso(it.get("date_published") or jp.get("datePosted")), url=url))
    return rows


# ---------------------------------------------------------------- reading one board

def read_workable(client: Client, board: Board) -> list[dict]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{urllib.parse.quote(board.slug)}?details=false"
    return workable_rows(board, client.get_json(url))


def read_recruitee(client: Client, board: Board) -> list[dict]:
    try:
        data = client.get_json(f"https://{board.host}/api/offers/")
        return recruitee_rows(board, data.get("offers") or [])
    except urllib.error.HTTPError as e:
        # Recruitee's docs set 10 February 2027 as the date the Careers Site API starts requiring a
        # per-company token (401 without it); they say the XML offer feed is not covered by the token.
        if e.code not in (401, 403):
            raise
    r = client.get(f"https://{board.host}/api/feeds/offers.xml", accept="application/xml, text/xml")
    return recruitee_rows(board, recruitee_xml_offers(r.body))


def read_teamtailor(client: Client, board: Board, per_page: int | None = None) -> list[dict]:
    url, rows, seen, title = f"https://{board.host}/jobs.json", [], set(), None
    if per_page:
        url += f"?per_page={per_page}"
    for _ in range(TEAMTAILOR_MAX_PAGES):
        r = client.get(url)
        if r.content_type not in ("application/feed+json", "application/json"):
            raise NotFound(f"{url} is not a Teamtailor jobs feed (content-type {r.content_type or 'none'})")
        d = json.loads(r.body)
        title = title or d.get("title")
        fresh = [x for x in teamtailor_rows(board, d.get("items") or [], title) if x["uid"] not in seen]
        seen.update(x["uid"] for x in fresh)
        rows.extend(fresh)
        nxt = d.get("next_url")
        if not fresh or not nxt or urllib.parse.urlsplit(nxt).hostname != board.host:
            return rows
        url = nxt
    print(f"warning: {board.key}: stopped after {TEAMTAILOR_MAX_PAGES} pages", file=sys.stderr)
    return rows


READERS = {"workable": read_workable, "recruitee": read_recruitee, "teamtailor": read_teamtailor}


def matches(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return not keywords or any(k.lower() in t for k in keywords)


def fetch(boards: list[Board], keywords: list[str], client: Client):
    """Returns (rows, per-board counts, errors). A failed board is reported, never treated as empty."""
    rows, counts, errors = [], {}, []
    for b in boards:
        try:
            got = READERS[b.ats](client, b)
        except (HostBlocked, Disallowed, NotFound, ValueError, ET.ParseError) as e:
            errors.append(f"{b.key}: {e}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(f"{b.key}: {getattr(e, 'reason', e)}")
            continue
        kept = [r for r in got if matches(r["title"], keywords)]
        counts[b.key] = (len(got), len(kept))
        rows.extend(kept)
    return rows, counts, errors


# ---------------------------------------------------------------- dedupe and CSV

def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {row["uid"] for row in csv.DictReader(f) if row.get("uid")}


def select_new(rows: list[dict], seen: set[str], today: str) -> list[dict]:
    new, batch = [], set()
    for r in rows:
        if r["uid"] in seen or r["uid"] in batch:
            continue
        batch.add(r["uid"])
        new.append({**r, "first_seen": today})
    return new


def write_csv(path: Path, rows: list[dict], append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append and path.exists() and path.stat().st_size > 0 else "w"
    with path.open(mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- CLI

def main(argv=None, client: Client | None = None, now: datetime | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sites-file", default="sites.txt", type=Path)
    p.add_argument("--keyword", action="append", default=[], help="keep titles containing any of these (repeatable)")
    p.add_argument("--out-dir", default="data", type=Path)
    p.add_argument("--dry-run", action="store_true", help="print the parsed boards and their endpoints, no network")
    a = p.parse_args(argv)

    boards, keys = [], set()
    for line in read_sites(a.sites_file):
        try:
            b = parse_board(line)
        except ValueError as e:
            print("skip:", e, file=sys.stderr)
            continue
        if b.key not in keys:
            keys.add(b.key)
            boards.append(b)
    if not boards:
        print(f"No Workable, Recruitee or Teamtailor board in {a.sites_file}", file=sys.stderr)
        return 2
    if a.dry_run:
        for b in boards:
            print(f"{b.raw} -> {b.key} on {b.host}")
        return 0

    client = client or Client()
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    rows, counts, errors = fetch(boards, a.keyword, client)
    for key, (listed, kept) in counts.items():
        print(f"{key}: {listed} listed" + (f", {kept} match the keywords" if a.keyword else ""))
    for e in errors:
        print("error:", e, file=sys.stderr)
    all_path, new_path = a.out_dir / "jobs.csv", a.out_dir / "new_jobs.csv"
    new = select_new(rows, load_seen(all_path), today)
    write_csv(all_path, new, append=True)
    write_csv(new_path, new, append=False)
    print(f"{len(rows)} jobs read from {len(counts)} of {len(boards)} boards in {client.requests} requests, "
          f"{len(new)} new, written to {new_path}")
    return 1 if errors and not counts else 0


if __name__ == "__main__":
    sys.exit(main())
