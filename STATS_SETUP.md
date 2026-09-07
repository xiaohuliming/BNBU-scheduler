# Dashboard metrics and maintenance

The live dashboard is `/stats/index.html`. Its only data dependency is the
read-only `/api/analytics/dashboard` endpoint in `site_analytics.py`.
Fonts, CSS, JavaScript, and SVG charts are hosted locally. No runtime Tailwind
compiler, external font service, chart CDN, or dashboard framework is required.
Run `node precompile.js` after dashboard asset edits; it stamps CSS and JavaScript
content hashes into the page so deployments invalidate browser caches.

## Metric contract

- The selected interval covers 1 to 365 Beijing calendar days. UTC bounds are
  calculated once and applied to all cards, daily series, rankings, and tables.
- If the interval includes today, the comparison ends at the same elapsed time
  in the preceding period. A partial day is not compared with an entire day.
- Period UV uses `COUNT(DISTINCT visitor_id)` over the full period. Daily UV
  must never be summed to estimate period UV.
- New visitors have their first observed visit in the selected period. They
  represent browser identifiers, not verified people or student accounts.
- Bot filtering is based on known user-agent markers and applies only to page
  visits. It is not proof that all remaining requests came from humans.
- Referrers are separated into direct, internal, external, and invalid/unknown.
  Only aggregate external domains are returned. Query strings stay private.
- Download attempts include `proxy`, `merge`, and `batch`. Transfer success means
  the server completed the transfer; it does not attest to browser saving or
  playback. Partial-transfer bytes remain part of bandwidth totals.
- Empty success-rate denominators return null. Dates before the first recorded
  source event also return null instead of an invented zero.
- P50 and P95 use the nearest-rank percentile over nonnegative recorded resolve
  elapsed times, including failed resolves. The number of observations is shown.
- Failure reasons are grouped into fixed categories. Raw error text, visitor IDs,
  user IDs, signed media URLs, and cookies are not exposed by the dashboard.
- All cards and breakdowns use one read-only SQLite transaction. A failed refresh
  keeps the last successful data and its original date range.

## Historical quality corrections

The September 7 inspection found 12,924 raw page-view rows but only 12,151 views
in the durable daily cache. Some historical days were stale because the old
summary refreshed only its recent tail. This dashboard queries raw events and
does not mix stale rollups into current charts. Existing history is not deleted.

The proxy unit-test fixtures also emitted three-byte events to the application
DB. There were 66 matching historical rows on September 7. Their exact footprint
is the `proxy` action, `bytes=3`, anonymous user, and the fixture host
`upos-sz-mirrorcosov.bilivideo.com`. Some have automatically assigned visitor IDs,
so a null visitor-ID requirement would miss newer test rows. These events are
excluded from the new dashboard, with the excluded count shown for each period.
Source rows remain unchanged.

New test requests are marked before handling, and the telemetry writer skips
marked requests even when a delayed streaming response closes after the test has
restored `app.testing`. Tests must not contaminate the real statistics again.

ZIP downloads now emit the `batch` action. Earlier ZIP events were logged as
`proxy`; they stay in the historical single-transfer bucket because the stored
rows cannot reliably distinguish them. All transfer modes count toward totals.

The old tracking endpoint replaced an empty document.referrer with the HTTP
Referer of the analytics fetch, which identifies the current page. New tracking
preserves explicit empty referrers as direct visits and marks their provenance
with the idempotently added `referrer_known` column. Legacy internal/empty
referrers are shown in a separate unresolved-history group. External domains
remain usable because that fallback could not invent an external referrer.
No old source attribution is guessed or rewritten.

## Validation

Run the test suite from an isolated checkout and DB. `tests/test_site_analytics.py`
checks period consistency, UTC+8 boundaries, same-elapsed comparisons, true period
UV, bot filtering, all transfer modes, exact fixture exclusion, null/empty states,
privacy, and prevention of delayed test telemetry.

Frontend QA includes date and tab changes, query races, refresh failure recovery,
CSV export, chart keyboard controls, and narrow/landscape layouts. CSV exports
contain daily aggregates, the bot-filter setting, and the exact data cutoff.

The legacy summary APIs remain available for older clients. The new page uses
only the unified endpoint. In-memory anti-scraping counters remain explicitly
labeled as since-restart counters and do not follow the date filter.
