# Campus knowledge service

The file center's **接入 Agent** control issues scoped read-only bearer tokens.
The same retrieval implementation is exposed through:

- `POST /mcp`: stateless Streamable HTTP, JSON responses, protocol versions
  `2025-03-26`, `2025-06-18`, `2025-11-25`.
- `POST /api/knowledge/search`: `query`, optional `kind`, `programme`,
  `cohort`, `semester`, `limit`.
- `POST /api/knowledge/read`: `document_id`, optional global chunk `offset`,
  `page`, `limit`. Follow `next_offset` until null.
- `POST /api/knowledge/documents`: filtered deterministic document pagination.
- `GET /api/knowledge/openapi.json`: public HTTP integration schema.

Configure `Authorization: Bearer YOUR_TOKEN`. Clients must support custom
headers; this release does not advertise OAuth. MCP POST requests accept both
`application/json` and `text/event-stream`, while responses use JSON. GET/DELETE
on `/mcp` return 405 because no persistent SSE stream or session is offered.
Interoperability is verified with the official `mcp==1.29.1` Python SDK.

## Sources and retrieval

`campus_knowledge.sqlite` is a generated, read-only FTS5 index, not the user DB.
It is blocked by the existing `.sqlite` static-file rule. It contains only:

1. The eight PDF files and eleven office entries explicitly listed in the
   committed `campus_docs.json`. Installers and the SSO station are not ingested.
2. Public Academic Registry programme handbook PDFs for 2023 through 2026 admission.
   Original URLs, page numbers and SHA-256 hashes are retained. Text extraction
   preserves table layout and removes strikethrough revisions. Empty pages are
   skipped only when they contain no PDF objects. Scanned pages fail the build.
3. Every course and meeting row from the committed current Course List workbook,
   with catalogue descriptions. Offering Programme is not student eligibility.
   These are file snapshots, not live MIS seats or enrollment status.

No user DDLs, messages, credentials, private archive folders or AI enrichment
prose are indexed. Queries run locally with BM25, Chinese bigrams and a curated
bilingual term dictionary. This is retrieval for RAG, not an embedding model or
an answer-generation service. Results require source/date/cohort checking.

## Rebuild

Use the app environment with build-only `pdfplumber` installed. No new runtime
dependencies or GPU are needed. Source downloads use verified TLS with system
curl, an explicit AR host allowlist, size/time limits and four workers.

```bash
python build_campus_knowledge.py --refresh-handbooks
python -m pytest tests/ -q
```

Downloads and extracted handbook pages are cached in
`/tmp/maxcourse-knowledge-sources`. Without `--refresh-handbooks`, a build reuses
this cache and reparses the current local campus documents and timetable.
Commit the rebuilt index with source changes. Corpus tests reject stale local
source fingerprints or a semester mismatch. For a new admission year, update
the builder's cohort selection and corpus expectations explicitly.

## Access controls

Browser sessions can create/list/revoke their own tokens. Raw tokens are returned
once and never persisted; only SHA-256 hashes and short display prefixes are
stored. Tokens expire after 90 days, with at most three active per account.
All keys on an account share a durable 60/minute and 1,500/day quota. An independent
300/minute IP cap also covers invalid-token requests. Search concurrency is
limited to three, and SQLite queries have a two-second execution deadline.

Only exact knowledge routes are exempt from the browser User-Agent filter. They
perform their own authorization and limits. Tokens cannot authenticate ordinary
site APIs or key-management routes. Responses use `no-store`; tokens must never
be put in URLs. Cross-origin browser requests are denied unless their exact
Origin is explicitly configured in `MAXCOURSE_KB_ALLOWED_ORIGINS`. Native Agent
clients normally omit Origin. No wildcard CORS policy is enabled.

The index is immutable to clients. Document text is evidence, never agent
instructions. Retrieval does not prevent an authorized client from copying
returned text; quotas bound use, not ownership or redistribution.

## Local smoke checks

```bash
python tests/serve_campus_qa.py
uv run --with 'mcp==1.29.1' python tests/verify_campus_mcp.py
```

The QA server binds only to `127.0.0.1:5017`, with a disposable DB and fake test
account. The SDK check issues and revokes a temporary key without printing it.
Do not use the QA entry point in production.
