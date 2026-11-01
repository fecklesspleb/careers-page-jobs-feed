"""Live checks against public career-site endpoints. Skipped unless LIVE=1.
A host that rate-limits this machine (Workable often answers HTTP 429 to cloud IPs) skips its test."""
import os
import unittest
import urllib.robotparser

import careers_feed as cf


@unittest.skipUnless(os.environ.get("LIVE") == "1", "set LIVE=1 to run against real career sites")
class Live(unittest.TestCase):
    def setUp(self):
        self.client = cf.Client()

    def read(self, fn, line, **kw):
        try:
            return fn(self.client, cf.parse_board(line), **kw)
        except cf.HostBlocked as e:
            self.skipTest(str(e))

    def test_workable(self):
        rows = self.read(cf.read_workable, "workable:huggingface")
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(r["uid"].startswith("workable:huggingface:") and r["url"] for r in rows))

    def test_recruitee_json_and_xml_feed_agree(self):
        b = cf.parse_board("recruitee:bunq")
        from_json = cf.recruitee_rows(b, self.client.get_json(f"https://{b.host}/api/offers/")["offers"])
        xml = self.client.get(f"https://{b.host}/api/feeds/offers.xml", accept="application/xml, text/xml").body
        from_xml = cf.recruitee_rows(b, cf.recruitee_xml_offers(xml))
        self.assertGreater(len(from_json), 0)
        self.assertEqual({r["uid"] for r in from_xml}, {r["uid"] for r in from_json})

    def test_teamtailor_paging_matches_one_read(self):
        whole = self.read(cf.read_teamtailor, "teamtailor:proxima")
        if len(whole) <= 10:
            self.skipTest("board too small to page with per_page=10 today")
        paged = self.read(cf.read_teamtailor, "teamtailor:proxima", per_page=10)
        self.assertEqual({r["uid"] for r in paged}, {r["uid"] for r in whole})

    def test_smartrecruiters_api_still_disallowed(self):
        r = self.client.transport("https://api.smartrecruiters.com/robots.txt", accept="text/plain")
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.body.decode().splitlines())
        self.assertFalse(rp.can_fetch(cf.USER_AGENT, "https://api.smartrecruiters.com/v1/companies/x/postings"))


if __name__ == "__main__":
    unittest.main()
