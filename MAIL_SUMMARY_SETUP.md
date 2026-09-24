# School inbox summaries

The `/mail-summary/` tool signs into BNBU MIS using the current user's bound iSpace account, then follows the authenticated school mailbox link. Each connection is private to a MAXCOURSE browser session and expires after ten minutes. Passwords are used for the connection only. Existing encrypted iSpace sync credentials are used only when the user selects that option.

The adapter requests inbox folder 1 with `flag=new`, extracts each page's unread message metadata, and fetches selected bodies through Tencent Exmail's `t=quickreadmail&mode=preview` endpoint. It does not use the normal read endpoint, send mail, change flags, download attachments or fetch remote images. The observed school login and Tencent HTML structure are covered by parser tests; upstream UI changes fail closed. This is a webmail integration, not an official stable Tencent mailbox API.

Each generation covers at most ten selected messages from the current page. The UI shows the unread total, the actual summarized count and source text. Only the first 8,000 characters of each body are included, with a visible truncation notice. Image contents and attachments are excluded. Older unread messages remain available through pagination.

## OmniChat integration

Deploy the sibling OmniChat `app/mail_summary.py` and route registration before enabling live summaries. MAXCOURSE calls `https://chat.bnbscheduler.top/api/integrations/mail-summary` with the current user's existing SSO bearer token. The shared identity is checked before forwarding. No API key is created or exposed. Both services must use their existing shared auth database and SSO configuration.

OmniChat uses its existing model routing, balance admission, usage accounting and refund implementation. Set `OMNICHAT_MAIL_SUMMARY_MODEL` in the OmniChat service environment to an available text model. The default is `gpt-6-luna`. Model selection cannot be supplied by the browser. This integration does not save email text to a conversation. Upstream processing still follows the configured model provider's data handling.

An identical selection reuses a successfully generated result within the current mailbox page and connection. Refreshing, reconnecting or moving to another page clears that cache. A network interruption after model submission can leave the charge outcome uncertain; the UI reports this and does not retry automatically.

Mailbox sessions and results are held only in process memory. Restarting the current single Flask process disconnects them. A future multi-worker deployment needs a deliberate encrypted shared-session design; never put mailbox cookies or text in Flask's client cookie. API responses use `Cache-Control: no-store`. All mail source modules remain protected by the existing static-source guard.

## Validation

```sh
./venv/bin/python -m pytest tests/test_mail_digest.py tests/test_ispace_auto_sync.py -q
NODE_PATH=/path/to/jsdom/node_modules node --test tests/test_mail_summary_frontend.js
node precompile.js
```

The frontend test dependency is jsdom 26, separate from runtime requirements. In OmniChat run `.venv/bin/python -m unittest discover -s tests -p test_mail_summary.py -q` and the existing open API tests. Use only synthetic data in committed fixtures. Live acceptance should confirm login, unread preservation, generation, billing, disconnect, and desktop/mobile rendering. Do not record passwords, SIDs, cookies or real mail bodies in fixtures or reports.
