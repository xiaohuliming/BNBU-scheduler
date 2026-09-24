# Privacy update: scoped code review and verification

Date: 2026-09-24. Project: MAXCOURSE, public source at https://github.com/xiaohuliming/BNBU-scheduler.

This is a developer review assisted by automated tools. It is not an independent security audit, a university authorization, a penetration-test report or a guarantee of safety. The review covers the code paths below, not every part of MAXCOURSE or its infrastructure. OmniChat and upstream model providers are outside the full source-audit scope of this public repository.

## Data-flow findings

- One-off school login, binding and manual DDL sync submit the school password over HTTPS and use it transiently. These flows do not write the school password to the users table.
- Optional DDL automatic sync remains available. It stores a Fernet-encrypted password in the server database. The management UI and JSON settings response do not expose plaintext or ciphertext. The server holds the decryption key and can decrypt while syncing or reconnecting mail. Encryption must not be described as making the password inaccessible to the server.
- Mailbox cookies and session identifiers are retained only in process memory, bounded to eight hours and the account's current binding generation. They are never included in the browser's signed session or sent as AI prompt fields.
- A new authenticated homepage visit submits a CSRF-protected refresh once. GET polling is read-only. Concurrent work for the same binding is coalesced; queued work is bounded. The former four-hour refresh gate and twelve-hour result reuse gate are removed.
- Email text is passed to OmniChat's configured model service. This involves third-party processing; open source does not guarantee the third party's data-retention behavior. The brief cache stores encrypted highlights and selected source metadata, not raw bodies. Existing source limits and attachment/image exclusions remain visible in the privacy statement.

## Changes verified

1. Unlink is authenticated and requires a same-session CSRF token. It clears the caller's active saved password, binding, mail cache, imported iSpace todos and matching reminder records; manual todos and other accounts are preserved.
2. Unlink increments a binding generation. Queued mail jobs, each subsequent mail request, AI submission and cache writes check ownership/cancellation. A stale job cannot restore an earlier binding's cache after unlink/rebind.
3. Logout revokes in-memory mailbox access and queued jobs. A new login can start without an older cancelled job removing its state. Account deletion also clears the current shared SSO cookie/token to avoid immediate silent reattachment.
4. Scheduled and manual DDL writes re-check binding under a database write transaction, preventing an in-flight sync from restoring imported tasks after unlink.
5. School-mail redirects retain the exact school/Tencent host allowlist. Preview reads do not execute HTML, fetch embedded trackers, send messages or change read flags.
6. Email/model text is rendered as text, not HTML. Frontend account switching ignores late results from the previous account. Private JSON responses use no-store. Source and database files remain behind the static-file guard.
7. School crawler logs no longer print the supplied account identifier or exception URLs that could contain session keys. This does not claim that all infrastructure logs have been independently audited.
8. The public privacy/changelog pages identify the developer, public repository, optional encrypted storage and manual alternative, backup limitations, lack of university endorsement, and availability of most services without iSpace login.

## Reproducible checks

```sh
./venv/bin/python -m pytest tests/ -q
NODE_PATH=/path/to/jsdom/node_modules node --test tests/test_mail_summary_frontend.js
node precompile.js
```

Focused coverage lives in `tests/test_privacy_controls.py`, `tests/test_mail_digest.py`, `tests/test_ispace_auto_sync.py` and the frontend harness. Tests use disposable databases and fake school/model calls. Real-user unbinding is not used as a destructive acceptance test. Live checks cover publication, login/session reuse and visit-triggered summaries without exposing credentials or mail bodies in test artifacts.

Release validation: 270 backend tests and six frontend behavior tests passed locally. Desktop and 390px mobile layouts were inspected with synthetic account data.

## Limits and follow-up

Historical backups can retain earlier records and encrypted credentials. Active unlink is not a promise of immediate deletion from every historical backup or third-party processor. The public repository does not by itself prove exact deployed-source equivalence. Other account endpoints, dependency vulnerabilities, infrastructure permissions, independent penetration testing and third-party model processing need separate review. Users should avoid sharing school credentials unless necessary, including with this project, and use official school channels if they want to change their password.
