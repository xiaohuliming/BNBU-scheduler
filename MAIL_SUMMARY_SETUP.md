# Automatic weekly homepage mail brief

The signed-in homepage shows at most four concise, source-linked highlights from the last seven days of the student's inbox. Both read and unread mail are considered. There is no toolbox item, separate mail UI, connect form, selection step, or generate button. The old `/mail-summary/` and `/mail-summary/index.html` addresses redirect to the homepage; manual `/api/mail-digest/*` handlers are retired.

A successful iSpace login or account binding schedules a background job using that verified password once. Login does not wait for MIS, mailbox reads, or AI generation. Returning users with an existing opt-in encrypted iSpace sync password can refresh automatically when their homepage loads. Old sessions without saved credentials begin on their next school login. The feature does not add password persistence.

## Data and read behavior

The server follows BNBU MIS's authenticated mailbox jump. It scans inbox pages in descending receipt order, filters the exact preceding seven days, and uses Tencent Exmail's `t=quickreadmail&mode=preview` endpoint for body text. It never marks messages read or loads remote images and attachments. This is an observed webmail protocol, not an official stable mailbox API.

A run covers up to 150 recent messages. Any cap or unparseable dates produce an explicit partial-coverage label. Long messages are bounded and flagged to the model. Headlines merge repeated notices, retain important dates and conditions, and omit stale or irrelevant announcements. The model must cite real source IDs. Only the brief, selected source metadata, and coverage dates are cached in SQLite. The payload is encrypted with a purpose-derived key from the app secret and binds the account ID and school identity inside the ciphertext. Raw messages are not stored.

The cache is refreshed no more than once per four hours. Unchanged mail reuses a brief for up to twelve hours; date interpretation is then refreshed. Failed jobs back off for an hour, preserve a usable previous result, and never block the homepage. Cached briefs older than a day are not displayed. At most three jobs execute concurrently, with a bounded queue of 24. Passwords awaiting execution are encrypted in memory and discarded with the job. Jobs check account binding before reading and before saving.

## OmniChat service integration

MAXCOURSE calls the fixed HTTPS endpoint `https://chat.bnbscheduler.top/api/integrations/mail-brief` with `X-Mail-Brief-Token`. Set the same private `MAXCOURSE_MAIL_BRIEF_TOKEN` in both systemd services. Browser cookies, SSO bearer tokens, and client-supplied model IDs cannot authorize this service endpoint. Do not expose its token to the frontend or commit it.

OmniChat uses its existing model/provider routing directly, with no tools, no chat persistence and no personal credit debit. Model costs belong to the operator's configured provider account. `OMNICHAT_MAIL_SUMMARY_MODEL` defaults to `gpt-6-luna`. The route bounds payload text, output size, concurrent generations and daily attempts. It logs only aggregate usage, not mail bodies or service credentials.

The iSpace login explanation discloses automatic DDL sync and mail summarization. Source details link to the official school mailbox and note that images and attachments were not read. Authentication errors or provider failures quietly omit an unavailable brief instead of adding another onboarding flow.

## Checks

```sh
./venv/bin/python -m pytest tests/test_mail_digest.py tests/test_ispace_auto_sync.py -q
NODE_PATH=/path/to/jsdom/node_modules node --test tests/test_mail_summary_frontend.js
node precompile.js
```

Frontend tests use jsdom 26 as a test-only dependency. In OmniChat run `.venv/bin/python -m unittest discover -s tests -p test_mail_summary.py -q` and the existing open API suite. Production tests must isolate all account databases and block real background jobs. Never save school passwords, SIDs, cookies or actual email bodies in fixtures or logs.
