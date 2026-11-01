# Fixtures

Real responses saved on November 1, 2026 with the script's User-Agent, then trimmed:

| File | Request | Trimmed |
|---|---|---|
| `workable_huggingface.json` | `GET https://apply.workable.com/api/v1/widget/accounts/huggingface?details=false` | account description shortened |
| `recruitee_bunq.json` | `GET https://bunq.recruitee.com/api/offers/` | first 4 of 25 offers; only the fields the script reads |
| `recruitee_bunq_feed.xml` | `GET https://bunq.recruitee.com/api/feeds/offers.xml` | first 4 of 25 offers; description, requirements, highlight and mailbox_email removed |
| `teamtailor_proxima_p1.json` to `_p4.json` | `GET https://proxima.teamtailor.com/jobs.json?page=N&per_page=15` | descriptions removed |
| `teamtailor_career.json` | `GET https://career.teamtailor.com/jobs.json` | descriptions removed |

The Workable HTTP 429 response used in the rate-limit test is written in the test itself, modelled on
Cloudflare's "error code: 1015" page; it is not a saved response.
