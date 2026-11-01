# careers-page-jobs-feed

A free, standard-library Python script that reads the public job-board JSON behind **Workable**, **Recruitee**
and **Teamtailor** career pages, normalises every posting to one CSV schema, and writes only the jobs it has not
seen before. A GitHub Actions workflow runs it once a day and commits the result, so the repo becomes a daily
feed of new postings for the companies you list.

No API keys, no dependencies, no scraping of HTML pages: each ATS publishes the same job data its career page
uses as JSON, and the script reads that.

## What it reads

| ATS | Endpoint | Paging |
|---|---|---|
| Workable | `https://apply.workable.com/api/v1/widget/accounts/{account}?details=false` | none, the whole board in one response |
| Recruitee | `https://{company}.recruitee.com/api/offers/` (falls back to `/api/feeds/offers.xml` on 401/403) | none |
| Teamtailor | `https://{company}.teamtailor.com/jobs.json` (JSON Feed 1.1) | follows `next_url` |

SmartRecruiters is not supported on purpose: `api.smartrecruiters.com/robots.txt` disallows every crawler
except LinkedInBot.

## Quickstart

```bash
git clone https://github.com/fecklesspleb/careers-page-jobs-feed
cd careers-page-jobs-feed
python careers_feed.py --dry-run      # check how each line of sites.txt is understood, no network
python careers_feed.py                # fetch, write data/jobs.csv and data/new_jobs.csv
python careers_feed.py --keyword engineer --keyword data
```

Python 3.9 or newer. Put your own companies in `sites.txt`, one per line, either the board URL from your browser
or `ats:name`:

```text
https://apply.workable.com/huggingface/
https://bunq.recruitee.com
https://proxima.teamtailor.com
teamtailor:jobs.tibber.com         # a Teamtailor career site on the company's own domain
```

### Run it daily on GitHub

1. Fork or copy this repo. Schedules are off in forks until you enable Actions in the **Actions** tab.
2. Optional: add a repository variable `KEYWORDS` (Settings → Secrets and variables → Actions → Variables),
   for example `engineer, data`.
3. Run **Careers page jobs feed** once with **Run workflow**. After that it runs every day at 11:17 UTC.

Each run tests the script offline, fetches the boards, and commits `data/jobs.csv` (every job ever seen) and
`data/new_jobs.csv` (only the jobs that appeared in that run). Watch the repo or subscribe to its commits to
get notified.

## Output

Real run on November 1, 2026 with the `sites.txt` in this repo:

```text
$ python careers_feed.py
workable:huggingface: 6 listed
workable:blueground: 9 listed
recruitee:bunq: 25 listed
recruitee:jobs: 12 listed
teamtailor:proxima: 40 listed
teamtailor:career: 17 listed
109 jobs read from 6 of 6 boards in 11 requests, 109 new, written to data/new_jobs.csv
```

Run again straight after, it read the same 109 jobs and reported `0 new`.

`data/new_jobs.csv` from that run (three rows, one per ATS):

```csv
uid,ats,board,company,title,location,workplace,department,employment_type,posted_at,url,first_seen
workable:huggingface:81B46579FE,workable,huggingface,Hugging Face,Open-Source Machine Learning Engineer - EMEA Remote,"Paris, Île-de-France, France",remote,Open Source,Full-time,2026-05-29,https://apply.workable.com/j/81B46579FE,2026-11-01
recruitee:bunq:2800490,recruitee,bunq,bunq,Data Scientist Marketing,"Amsterdam, Noord-Holland, Netherlands",hybrid,Growth,fulltime_permanent,2026-10-28T13:21:58+00:00,https://careers.bunq.com/o/data-scientist-marketing-3,2026-11-01
teamtailor:proxima:8630029,teamtailor,proxima,Proxima,Principal Consultant - Generalist,"Düsseldorf, DE",,,FULL_TIME,2026-10-28T11:14:46+00:00,https://proxima.teamtailor.com/jobs/8630029-principal-consultant-generalist,2026-11-01
```

| Column | Meaning |
|---|---|
| `uid` | `ats:board:id`, stable across runs; used for de-duplication |
| `workplace` | `remote`, `hybrid`, `onsite` or blank when the source does not say |
| `employment_type` | as the ATS writes it (`Full-time`, `fulltime_permanent`, `FULL_TIME`) |
| `posted_at` | the ATS's publish date; Workable gives a date only |
| `first_seen` | the UTC date this script first saw the job |

## Being polite

- `robots.txt` is read once per host and obeyed, including `Crawl-delay` (up to 30 s). If it cannot be read
  because of a server error or the host answers with an HTML page, the host is skipped.
- At most one request per second per host, and one every 5 seconds to `apply.workable.com`.
- If a host answers HTTP 429 (Workable's Cloudflare front sometimes answers with "error code: 1015"), that host
  is skipped for the rest of the run instead of being retried; the other boards still run.
- The User-Agent names this project. Set `JOBS_FEED_USER_AGENT` to use your own.

## Limits

- No job descriptions, only the listing fields above.
- Teamtailor's `jobs.json` has no remote/hybrid field, so `workplace` is blank for Teamtailor (except `remote`
  when a posting is marked `TELECOMMUTE`) and `department` is blank too.
- Workable can answer HTTP 429 to cloud server IP addresses; it did to one of mine while I built this, though not
  on the day of the run above. A run that gets 429 skips Workable that day and picks the jobs up on a later run.
- Recruitee says calls to its Careers Site API without an `X-Careers-Sites-Token` header will return 401 after
  10 February 2027. The script then reads the board's public XML feed, which Recruitee says is not covered by the
  token.
- Removed or filled jobs are not tracked; `jobs.csv` only grows.
- GitHub disables scheduled workflows in public repositories after 60 days without repository activity. If that
  happens, re-enable the workflow in the Actions tab.
- Only boards that companies publish themselves are read. Check each company's terms before you reuse the data.

Disclosure: if you'd rather not host it, I built and sell a paid hosted version: [Career Site Jobs Feed on Apify](https://apify.com/fecklesspleb/career-site-jobs-feed), $0.002 per job, no start fee.

## Tests

```bash
python -m unittest -v              # offline, uses saved responses in tests/fixtures
LIVE=1 python -m unittest tests.test_live -v
```

## License

MIT
