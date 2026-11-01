"""A stand-in for careers_feed.http that serves saved responses by URL and records every request."""
import io
import json
import urllib.error
from pathlib import Path

import careers_feed as cf

FIX = Path(__file__).parent / "fixtures"
ROBOTS_OPEN = "User-agent: *\nDisallow:\n"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeHTTP:
    """routes: url -> (status, content_type, body) or a callable returning that tuple.
    Any robots.txt not in routes answers an open robots file."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, url, accept="application/json", timeout=30):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None and url.endswith("/robots.txt"):
            route = (200, "text/plain", ROBOTS_OPEN)
        if route is None:
            route = (404, "application/json", '{"error":"Not Found"}')
        if callable(route):
            route = route()
        status, ctype, body = route
        body = body.encode() if isinstance(body, str) else body
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "error", {"Content-Type": ctype}, io.BytesIO(body))
        return cf.Response(url, status, {"Content-Type": ctype}, body)

    def api_calls(self):
        return [u for u in self.calls if not u.endswith("/robots.txt")]


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(fake, clock=None):
    clock = clock or FakeClock()
    return cf.Client(transport=fake, sleep=clock.sleep, clock=clock), clock


WORKABLE_HF = "https://apply.workable.com/api/v1/widget/accounts/huggingface?details=false"
RECRUITEE_BUNQ = "https://bunq.recruitee.com/api/offers/"
RECRUITEE_BUNQ_XML = "https://bunq.recruitee.com/api/feeds/offers.xml"
TT_PROXIMA = "https://proxima.teamtailor.com/jobs.json"


def standard_routes():
    """Real responses saved on 2026-11-01 (descriptions removed, see fixtures/README.md).
    Proxima's feed was saved with per_page=15, so its first URL here serves page 1 of 3."""
    return {
        WORKABLE_HF: (200, "application/json", fixture("workable_huggingface.json")),
        RECRUITEE_BUNQ: (200, "application/json", fixture("recruitee_bunq.json")),
        TT_PROXIMA: (200, "application/feed+json", fixture("teamtailor_proxima_p1.json")),
        "https://proxima.teamtailor.com/jobs.json?page=2&per_page=15":
            (200, "application/feed+json", fixture("teamtailor_proxima_p2.json")),
        "https://proxima.teamtailor.com/jobs.json?page=3&per_page=15":
            (200, "application/feed+json", fixture("teamtailor_proxima_p3.json")),
    }


def json_body(obj) -> str:
    return json.dumps(obj)
