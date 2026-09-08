# Cap verification for suspected automated API requests

CAPTCHA is a proof-of-work speed bump, not proof of a person's identity. Normal
browser requests remain challenge-free. Suspicious user agents and exhausted
visitor/IP buckets receive the `X-Maxcourse-Challenge: required` response marker
before any business handler executes. The shared first-party fetch wrapper
opens one accessible dialog and retries each original request at most once.
Ordinary authorization errors and unmarked rate limits are never auto-retried.
Hidden media-download frames request verification from their visible parent.
The parent checks the message origin and exact frame before granting a resume;
cancellation and remaining hard limits use the existing download-error channel.

## Verification boundary

- `capjs-core` 0.1.2 is the official verifier, running on loopback port 5068.
  Cap widget 0.1.57 and WASM 0.0.7 are committed under `vendor/cap/0.1.57`.
  No third-party scripts, tracking, or client instrumentation are enabled.
- `/api/human/challenge` issues a five-minute challenge scoped to a hash of a
  signed browser-cookie identifier, client IP, and user agent. This identifier
  is issued only by the challenge endpoint, so parallel API session responses
  cannot overwrite it during verification.
- `/api/human/redeem` checks the submitted PoW through the authenticated local
  verifier. A successful redemption consumes the challenge nonce once and sets
  a signed, HttpOnly, SameSite cookie valid for 15 minutes. Cookies are Secure
  on public hosts. The widget's client-side event/token is never authorization.
- A verifier restart rotates its challenge-signing key, so cleared in-memory
  nonce state cannot make old proofs replayable. Existing browser clearances
  remain valid until their own expiry.
- Same-origin checks, strict payload bounds, separate per-IP mint/redeem limits,
  and bounded nonce storage protect verification endpoints. Verifier failures
  return an error; they never grant clearance.
- Successful verification resets only that visitor's exhausted token bucket.
  The shared IP bucket and all existing API/auth/source-file protections remain.
  A verified client exceeding the hard quotas receives a normal retry-after
  response, rather than an endless series of verification dialogs.
- HTML is revalidated as a fresh response so old cached pages cannot omit the
  injected fetch wrapper. API and immutable-asset caching remain independent.

## Production installation

Run as root before restarting Flask:

```sh
MAXCOURSE_PROJECT_DIR=/www/wwwroot/maxcourse bash deploy/install-cap.sh
```

The installer uses the existing Node 22 runtime at `/opt/maxcourse-media/node`,
installs locked runtime dependencies to an immutable directory under
`/opt/maxcourse-cap/releases`, and atomically updates `current`. It creates an
independent service with a dynamic system user and CPU/memory limits. Bridge
credentials live in `/etc/maxcourse-cap.env`, mode 0600, loaded by both services.
Nothing under `cap_service` or `deploy` is publicly served.

The production deployment script must call this installer after pulling code
and before its tests/restart. The hook is conditional so older checkouts can
still be deployed. A failed setup leaves the existing Flask process running.
For rollback, deploy the prior commit; keep the isolated verifier available
until all old browser challenges have expired. Never copy the env file into Git.

## Validation

```sh
python -m pytest tests/ -q
cd cap_service && npm ci --ignore-scripts && npm test
cd .. && NODE_PATH=./cap_service/node_modules node tests/check_human_verification.cjs
```

Backend tests cover failed, forged, expired, wrong-browser and cross-site
submissions; source/auth protections; bounded minting; and shared-IP quotas.
Official-core tests solve isolated low-difficulty fixtures and check single-use
redemption. Synthetic DOM tests cover one dialog for parallel requests, server
confirmation, retained JSON/upload bodies, cancellation, and abort handling.
Browser acceptance must additionally exercise the actual widget on mobile and
desktop, plus an API navigation used for a download. Test state stays local and
never invokes notification dispatch or writes production user data.
