# Media downloader maintenance

Install the repository's `requirements.txt` in the application virtual environment.
The downloader requires the tested yt-dlp release and its default dependencies,
including yt-dlp-ejs. The former optional installation allowed production to stay
on an obsolete YouTube extractor even after application deployments.

YouTube also needs a supported JavaScript runtime. This installation uses the
dedicated Node 22 runtime at `/opt/maxcourse-media/node/bin/node`. It does not
replace the server's global Node installation. Set `MEDIA_DL_NODE` to use another
executable. Otherwise the downloader also discovers Deno and Node on `PATH`.
The runtime installed on 2026-09-06 is Node v22.23.2 from the official Node.js
distribution, verified against its published SHA-256 checksum.

References: [yt-dlp release](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19)
and [JavaScript runtime requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

Optional `MEDIA_DL_COOKIE_FILE` accepts an operator-owned Netscape cookie file
for yt-dlp sites that require a session. Store that file outside the web root and
restrict its filesystem permissions. The application never reads personal
browser profiles. Douyin uses its public mobile share-page token protocol and
does not require personal cookies. Human verification challenges are reported
without attempting to solve them.

`MAXCOURSE_PROXY` remains the preferred outbound proxy setting, with
`HTTPS_PROXY` as a fallback. An outbound proxy is independent of the same-origin
download proxy: YouTube downloads always use the latter, even on a server that
can reach YouTube directly.

All extraction and transfer HTTP requests use `media_dl/http.py`. It validates
URLs, checks every DNS answer, connects to the validated IP, and preserves the
original Host header and TLS hostname. Redirects pass through the same checks.
Do not reintroduce an unguarded requests, urllib, curl, or ffmpeg URL fetch.
The tested transport needs requests 2.32.5 or later. ffmpeg receives guarded
binary streams through pipes and cannot open remote URLs itself.

Merge requests are limited to three concurrent jobs and 1 GB per input, with a
15 minute overall deadline. Ordinary downloads remain streamed and support
chunk retries. Browser resume requests intentionally restart from byte zero.
Transfer failures return an error HTTP status. The page uses separate hidden
frames and validates their message sources to report failures inline while
keeping browser-managed large-file downloads.

Selecting several items now prepares one streamed ZIP. This avoids browser
restrictions that can silently block the second automatic download. Preparation
tokens expire after five minutes, are bound to the requesting session, and can
be used once. Archives allow up to 50 selected files and 2 GB in total, with
three concurrent transfers. Member names are flattened and deduplicated. The
server does not buffer entire videos or archives in memory or on disk.

Run the Python test suite from an isolated checkout with a temporary database.
After frontend edits, run `node precompile.js` and validate the page in a real
browser. Exercise a complete downloaded file, not only a successful resolve.

The September 2026 regression set includes:

- YouTube `jNQXAC9IVRw`, video and audio merged into one playable MP4.
- Douyin `7638586788907223488` and `7664074986541627818`, complete source downloads.
- Bilibili `BV1RYtV6NEwU`, native extraction and its existing 412 recovery.
- Bilibili `BV1xx411c7mD`, a complete 98,264,686-byte transfer.
- Private addresses, unsafe redirects, DNS rebinding, invalid payload types,
  first-range alignment, premature EOF, merge failure, and iframe error feedback.
