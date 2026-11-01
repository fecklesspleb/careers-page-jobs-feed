import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import careers_feed as cf
from tests.fake_http import (RECRUITEE_BUNQ, RECRUITEE_BUNQ_XML, TT_PROXIMA, WORKABLE_HF, FakeHTTP, client,
                             fixture, standard_routes)

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
CF_1015 = ("<html><title>Access denied | apply.workable.com used Cloudflare to restrict access</title>"
           "error code: 1015</html>")


def run(tmp, fake, *args, sites=("workable:huggingface", "recruitee:bunq", "teamtailor:proxima")):
    sf = Path(tmp) / "sites.txt"
    sf.write_text("# comment\n" + "\n".join(sites) + "\n")
    c, _ = client(fake)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cf.main(["--sites-file", str(sf), "--out-dir", str(Path(tmp) / "data"), *args], client=c, now=NOW)
    return rc, out.getvalue(), err.getvalue()


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class ParseBoard(unittest.TestCase):
    def test_urls_and_names(self):
        cases = {
            "https://apply.workable.com/huggingface/": ("workable", "huggingface", "apply.workable.com"),
            "https://apply.workable.com/huggingface/j/81B46579FE/": ("workable", "huggingface", "apply.workable.com"),
            "workable:blueground": ("workable", "blueground", "apply.workable.com"),
            "https://bunq.recruitee.com/": ("recruitee", "bunq", "bunq.recruitee.com"),
            "recruitee:Bunq": ("recruitee", "bunq", "bunq.recruitee.com"),
            "https://proxima.teamtailor.com/jobs": ("teamtailor", "proxima", "proxima.teamtailor.com"),
            "teamtailor:career": ("teamtailor", "career", "career.teamtailor.com"),
            "teamtailor:jobs.tibber.com": ("teamtailor", "jobs.tibber.com", "jobs.tibber.com"),
        }
        for line, want in cases.items():
            b = cf.parse_board(line)
            self.assertEqual((b.ats, b.slug, b.host), want, line)

    def test_refused_and_unknown(self):
        for bad in ("https://jobs.smartrecruiters.com/Acme", "smartrecruiters:Acme"):
            with self.assertRaisesRegex(ValueError, "robots.txt"):
                cf.parse_board(bad)
        for bad in ("https://example.org/jobs", "greenhouse:airbnb", "workable:../x", "https://apply.workable.com/"):
            with self.assertRaises(ValueError):
                cf.parse_board(bad)

    def test_sites_file_comments_and_repo_sites(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.txt"
            p.write_text("# header\n\nrecruitee:bunq   # note\n")
            self.assertEqual(cf.read_sites(p), ["recruitee:bunq"])
        boards = [cf.parse_board(x) for x in cf.read_sites(ROOT / "sites.txt")]
        self.assertEqual({b.ats for b in boards}, {"workable", "recruitee", "teamtailor"})
        self.assertEqual(len({b.key for b in boards}), len(boards))


class Normalise(unittest.TestCase):
    def test_workable(self):
        b = cf.parse_board("workable:huggingface")
        rows = cf.workable_rows(b, json.loads(fixture("workable_huggingface.json")))
        self.assertEqual(len(rows), 6)
        r = rows[0]
        self.assertEqual(r["uid"], "workable:huggingface:81B46579FE")
        self.assertEqual(r["company"], "Hugging Face")
        self.assertEqual(r["location"], "Paris, Île-de-France, France")
        self.assertEqual((r["workplace"], r["employment_type"], r["posted_at"]), ("remote", "Full-time", "2026-05-29"))
        self.assertEqual(r["url"], "https://apply.workable.com/j/81B46579FE")

    def test_recruitee_json(self):
        b = cf.parse_board("recruitee:bunq")
        rows = cf.recruitee_rows(b, json.loads(fixture("recruitee_bunq.json"))["offers"])
        self.assertEqual(len(rows), 4)
        r = rows[0]
        self.assertEqual(r["uid"], "recruitee:bunq:2800490")
        self.assertEqual((r["company"], r["workplace"]), ("bunq", "hybrid"))
        self.assertEqual(r["posted_at"], "2026-10-28T13:21:58+00:00")
        self.assertEqual(r["location"], "Amsterdam, Noord-Holland, Netherlands")
        self.assertTrue(r["url"].startswith("https://careers.bunq.com/o/"))

    def test_recruitee_xml_matches_json(self):
        b = cf.parse_board("recruitee:bunq")
        from_json = cf.recruitee_rows(b, json.loads(fixture("recruitee_bunq.json"))["offers"])
        from_xml = cf.recruitee_rows(b, cf.recruitee_xml_offers(fixture("recruitee_bunq_feed.xml")))
        self.assertEqual(from_xml, from_json)

    def test_xml_with_entities_is_refused(self):
        with self.assertRaises(ValueError):
            cf.recruitee_xml_offers(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><offers/>')

    def test_unpublished_recruitee_offer_dropped(self):
        b = cf.parse_board("recruitee:bunq")
        rows = cf.recruitee_rows(b, [{"id": 1, "title": "A", "status": "closed"}, {"id": 2, "title": "B"}])
        self.assertEqual([r["uid"] for r in rows], ["recruitee:bunq:2"])

    def test_teamtailor(self):
        b = cf.parse_board("teamtailor:career")
        d = json.loads(fixture("teamtailor_career.json"))
        rows = cf.teamtailor_rows(b, d["items"], d["title"])
        self.assertEqual(len(rows), 17)
        r = rows[0]
        self.assertRegex(r["uid"], r"^teamtailor:career:\d+$")
        self.assertIn(f"/jobs/{r['uid'].rsplit(':', 1)[1]}-", r["url"])
        self.assertEqual(r["company"], "Teamtailor")
        self.assertEqual(r["location"], "Stockholm, SE; Paris, FR")
        self.assertEqual(r["employment_type"], "FULL_TIME")


class Reading(unittest.TestCase):
    def test_teamtailor_follows_next_url(self):
        fake = FakeHTTP(standard_routes())
        c, _ = client(fake)
        rows = cf.read_teamtailor(c, cf.parse_board("teamtailor:proxima"))
        self.assertEqual(len(rows), 40)
        self.assertEqual(len({r["uid"] for r in rows}), 40)
        self.assertEqual(len(fake.api_calls()), 3)

    def test_teamtailor_ignores_next_url_on_another_host(self):
        page = json.loads(fixture("teamtailor_proxima_p1.json"))
        page["next_url"] = "https://evil.example/jobs.json?page=2"
        fake = FakeHTTP({TT_PROXIMA: (200, "application/feed+json", json.dumps(page))})
        c, _ = client(fake)
        rows = cf.read_teamtailor(c, cf.parse_board("teamtailor:proxima"))
        self.assertEqual(len(rows), 15)
        self.assertEqual(fake.api_calls(), [TT_PROXIMA])

    def test_teamtailor_stops_when_a_page_repeats(self):
        page = json.loads(fixture("teamtailor_proxima_p1.json"))
        page["next_url"] = TT_PROXIMA
        fake = FakeHTTP({TT_PROXIMA: (200, "application/feed+json", json.dumps(page))})
        c, _ = client(fake)
        self.assertEqual(len(cf.read_teamtailor(c, cf.parse_board("teamtailor:proxima"))), 15)
        self.assertEqual(len(fake.api_calls()), 2)

    def test_teamtailor_html_answer_is_not_a_feed(self):
        fake = FakeHTTP({TT_PROXIMA: (200, "text/html", "<html></html>")})
        c, _ = client(fake)
        with self.assertRaises(cf.NotFound):
            cf.read_teamtailor(c, cf.parse_board("teamtailor:proxima"))

    def test_recruitee_falls_back_to_xml_on_401(self):
        routes = {RECRUITEE_BUNQ: (401, "application/json", '{"error":"Unauthorized"}'),
                  RECRUITEE_BUNQ_XML: (200, "text/xml", fixture("recruitee_bunq_feed.xml"))}
        fake = FakeHTTP(routes)
        c, _ = client(fake)
        rows = cf.read_recruitee(c, cf.parse_board("recruitee:bunq"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(fake.api_calls(), [RECRUITEE_BUNQ, RECRUITEE_BUNQ_XML])

    def test_404_is_an_error_not_an_empty_board(self):
        fake = FakeHTTP({})
        c, _ = client(fake)
        rows, counts, errors = cf.fetch([cf.parse_board("recruitee:nope")], [], c)
        self.assertEqual((rows, counts), ([], {}))
        self.assertIn("404", errors[0])


class Politeness(unittest.TestCase):
    def test_robots_disallow_skips_without_api_call(self):
        routes = standard_routes()
        routes["https://bunq.recruitee.com/robots.txt"] = (200, "text/plain", "User-agent: *\nDisallow: /api/\n")
        fake = FakeHTTP(routes)
        c, _ = client(fake)
        rows, counts, errors = cf.fetch([cf.parse_board("recruitee:bunq")], [], c)
        self.assertEqual(rows, [])
        self.assertIn("disallows /api/offers/", errors[0])
        self.assertEqual(fake.api_calls(), [])

    def test_robots_for_our_agent_only(self):
        routes = standard_routes()
        routes["https://bunq.recruitee.com/robots.txt"] = (
            200, "text/plain", "User-agent: careers-page-jobs-feed\nDisallow: /\n\nUser-agent: *\nDisallow:\n")
        fake = FakeHTTP(routes)
        c, _ = client(fake)
        _, _, errors = cf.fetch([cf.parse_board("recruitee:bunq")], [], c)
        self.assertEqual(len(errors), 1)

    def test_robots_4xx_means_no_rules_5xx_and_html_skip(self):
        routes = standard_routes()
        routes["https://bunq.recruitee.com/robots.txt"] = (404, "text/plain", "")
        c, _ = client(FakeHTTP(routes))
        self.assertEqual(len(cf.read_recruitee(c, cf.parse_board("recruitee:bunq"))), 4)
        for status, ctype, body in ((503, "text/plain", ""), (200, "text/html", "<!doctype html><html>")):
            routes["https://bunq.recruitee.com/robots.txt"] = (status, ctype, body)
            fake = FakeHTTP(routes)
            c, _ = client(fake)
            _, _, errors = cf.fetch([cf.parse_board("recruitee:bunq")], [], c)
            self.assertIn("skipped", errors[0])
            self.assertEqual(fake.api_calls(), [])

    def test_workable_429_skips_the_host_for_the_rest_of_the_run(self):
        routes = standard_routes()
        routes[WORKABLE_HF] = (429, "text/html", CF_1015)
        fake = FakeHTTP(routes)
        c, _ = client(fake)
        boards = [cf.parse_board(x) for x in ("workable:huggingface", "workable:blueground", "recruitee:bunq")]
        rows, counts, errors = cf.fetch(boards, [], c)
        self.assertEqual(list(counts), ["recruitee:bunq"])
        self.assertEqual(len(errors), 2)
        self.assertIn("Cloudflare error 1015", errors[0])
        self.assertIn("skipped for the rest of this run", errors[1])
        workable_calls = [u for u in fake.calls if "workable" in u]
        self.assertEqual(workable_calls, ["https://apply.workable.com/robots.txt", WORKABLE_HF])
        self.assertEqual(len(rows), 4)

    def test_robots_429_blocks_the_host(self):
        fake = FakeHTTP({"https://apply.workable.com/robots.txt": (429, "text/plain", "error code: 1015")})
        c, _ = client(fake)
        _, counts, errors = cf.fetch([cf.parse_board("workable:huggingface")], [], c)
        self.assertEqual(counts, {})
        self.assertIn("1015", errors[0])

    def test_workable_requests_are_five_seconds_apart(self):
        routes = standard_routes()
        routes["https://apply.workable.com/api/v1/widget/accounts/blueground?details=false"] = routes[WORKABLE_HF]
        fake = FakeHTTP(routes)
        c, clock = client(fake)
        cf.fetch([cf.parse_board("workable:huggingface"), cf.parse_board("workable:blueground")], [], c)
        # robots.txt, account 1, account 2: two waits of 5 s on a clock that does not move by itself
        self.assertEqual(clock.sleeps, [5.0, 5.0])

    def test_crawl_delay_is_honoured(self):
        routes = standard_routes()
        routes["https://proxima.teamtailor.com/robots.txt"] = (200, "text/plain", "User-agent: *\nCrawl-delay: 2\n")
        c, clock = client(FakeHTTP(routes))
        cf.read_teamtailor(c, cf.parse_board("teamtailor:proxima"))
        # robots.txt, then three feed pages, each 2 s after the request before it
        self.assertEqual(clock.sleeps, [2.0, 2.0, 2.0])

    def test_server_error_is_retried_once(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                return (502, "text/plain", "bad gateway")
            return (200, "application/json", fixture("workable_huggingface.json"))
        fake = FakeHTTP({WORKABLE_HF: flaky})
        c, _ = client(fake)
        self.assertEqual(len(cf.read_workable(c, cf.parse_board("workable:huggingface"))), 6)
        self.assertEqual(len(attempts), 2)

    def test_user_agent_names_the_project(self):
        self.assertIn("github.com/fecklesspleb/careers-page-jobs-feed", cf.USER_AGENT)


class EndToEnd(unittest.TestCase):
    def test_first_run_then_nothing_new(self):
        with tempfile.TemporaryDirectory() as t:
            rc, out, err = run(t, FakeHTTP(standard_routes()))
            self.assertEqual(rc, 0, err)
            self.assertIn("50 jobs read from 3 of 3 boards", out)
            new = read_csv(Path(t) / "data" / "new_jobs.csv")
            self.assertEqual(len(new), 50)
            self.assertEqual(list(new[0]), cf.COLUMNS)
            self.assertEqual({r["first_seen"] for r in new}, {"2026-11-01"})
            rc, out, err = run(t, FakeHTTP(standard_routes()))
            self.assertIn(", 0 new", out)
            self.assertEqual(read_csv(Path(t) / "data" / "new_jobs.csv"), [])
            self.assertEqual(len(read_csv(Path(t) / "data" / "jobs.csv")), 50)

    def test_new_job_appears(self):
        with tempfile.TemporaryDirectory() as t:
            run(t, FakeHTTP(standard_routes()))
            routes = standard_routes()
            d = json.loads(fixture("workable_huggingface.json"))
            d["jobs"].append({**d["jobs"][0], "shortcode": "NEW0000001", "title": "Data Engineer",
                              "url": "https://apply.workable.com/j/NEW0000001"})
            routes[WORKABLE_HF] = (200, "application/json", json.dumps(d))
            rc, out, _ = run(t, FakeHTTP(routes))
            new = read_csv(Path(t) / "data" / "new_jobs.csv")
            self.assertEqual([r["uid"] for r in new], ["workable:huggingface:NEW0000001"])
            self.assertEqual(len(read_csv(Path(t) / "data" / "jobs.csv")), 51)

    def test_keywords(self):
        with tempfile.TemporaryDirectory() as t:
            rc, out, _ = run(t, FakeHTTP(standard_routes()), "--keyword", "engineer", "--keyword", "DATA")
            new = read_csv(Path(t) / "data" / "new_jobs.csv")
            self.assertTrue(new)
            self.assertTrue(all("engineer" in r["title"].lower() or "data" in r["title"].lower() for r in new))
            self.assertIn("match the keywords", out)

    def test_failed_board_does_not_stop_the_others(self):
        routes = standard_routes()
        del routes[RECRUITEE_BUNQ]
        with tempfile.TemporaryDirectory() as t:
            rc, out, err = run(t, FakeHTTP(routes))
            self.assertEqual(rc, 0)
            self.assertIn("error: recruitee:bunq", err)
            self.assertIn("46 jobs read from 2 of 3 boards", out)

    def test_every_board_failing_exits_1(self):
        with tempfile.TemporaryDirectory() as t:
            rc, _, err = run(t, FakeHTTP({}))
            self.assertEqual(rc, 1)
            self.assertEqual(err.count("error:"), 3)

    def test_dry_run_makes_no_requests_and_bad_lines_are_reported(self):
        fake = FakeHTTP(standard_routes())
        with tempfile.TemporaryDirectory() as t:
            rc, out, err = run(t, fake, "--dry-run", sites=("recruitee:bunq", "smartrecruiters:Acme", "recruitee:bunq"))
            self.assertEqual(rc, 0)
            self.assertEqual(out.count("recruitee:bunq on bunq.recruitee.com"), 1)
            self.assertIn("SmartRecruiters is not supported", err)
        self.assertEqual(fake.calls, [])

    def test_no_valid_board_exits_2(self):
        with tempfile.TemporaryDirectory() as t:
            rc, _, _ = run(t, FakeHTTP({}), sites=("https://example.org/jobs",))
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
