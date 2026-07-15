# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MAXCOURSE (production: https://www.bnbscheduler.top) is a campus toolkit web app for UIC (北师港浸大 / United International College) students. A single Flask process serves both a JSON API and a bundle of static front-end tools: a course-credit optimizer, teacher ratings, a free-classroom finder, an iSpace DDL/todo manager with email reminders, and a no-watermark media downloader.

## Commands

```bash
# Run the server locally (serves on 0.0.0.0:5000)
./venv/bin/python app.py            # or: run.bat on Windows

# Tests (unittest-based, run via pytest or unittest)
./venv/bin/python -m pytest tests/ -q
./venv/bin/python -m pytest tests/test_app.py::AppTestCase::test_optimize_returns_real_course_units   # single test

# Course optimizer as a standalone CLI
./venv/bin/python maximize_credits.py --file "Course List and Timetable_Semester 1 of AY2026-27_20260709.xlsx" --courses AI1013 AI3013
```

`requirements.txt` lists the runtime deps: Flask, pandas, openpyxl, requests, beautifulsoup4, **numpy + scipy** (SkillPath PPR recommender, `/api/recommend`), and **pypdf** (transcript parsing `/api/parse-transcript` + the offline catalog build). `yt-dlp` is an **optional** dep imported lazily in `media_dl/ytdlp.py` (YouTube/Douyin/etc. resolution fails until it's installed). `pdfplumber` and `xlrd` are **build-time-only** (data-regeneration scripts) and are deliberately not in `requirements.txt` / not on the server. The committed `venv/` holds the local deps. Tests set `MAXCOURSE_SECRET_KEY` themselves and use a throwaway temp DB, so they need no server or real data.

## Architecture

### Backend: one monolithic Flask app (`app.py`, ~2700 lines)

- **The repo root is the web root.** The app is created with `static_folder='.', static_url_path=''`, so any file in the project is potentially served. The `block_sensitive_project_files` `before_request` hook is the security boundary — it 404s dotfiles, `.py`, `.db`, `.env`, `/tests`, `/venv`, etc. **Any new secret/source file type must be covered here**, or it becomes publicly downloadable.
- **SQLite (`maxcourse.db`, gitignored).** `init_db()` runs at import time. Schema migrations are idempotent `ALTER TABLE ... ADD COLUMN` calls wrapped in `try/except sqlite3.OperationalError` — follow that pattern to add columns rather than editing `CREATE TABLE`. Tables: `users`, `todos`, `teacher_ratings`, `page_views`, `media_dl_events`, `email_notification_deliveries`.
- **In-memory timetable cache.** The course Excel is parsed once by `get_df()` into the module global `df_cache`. This single DataFrame backs four features (optimizer, teacher directory, free classrooms, course explorer). `get_excel_file()` picks the `*.xlsx` whose name contains "Course List". If you swap the semester file, the cache is only cleared on process restart.
- **Auth is session-cookie based** (100-year lifetime, secret from `.flask_secret_key` or `MAXCOURSE_SECRET_KEY`). Two login paths: local username/password (werkzeug hash) and **iSpace login** (`/api/login/ispace`) which authenticates against the live UIC Moodle via `crawler.py` and lazily creates a passwordless "shadow user". `/api/todos/sync` refuses to run unless the submitted iSpace account matches the one bound to the logged-in user.
- `after_request` adds gzip + cache headers. API errors are funneled to JSON by the global `handle_api_errors` handler (only for `/api/*` paths).

### Core logic modules

- **`crawler.py`** — logs into iSpace (Moodle at `ispace.uic.edu.cn`), scrapes the `sesskey`, then calls Moodle's `core_calendar_get_action_events_by_timesort` AJAX service to pull upcoming DDLs (next 6 months, 2 weeks back). Returns normalized todo dicts. This is the only code that touches real student credentials — it holds them only for the request.
- **`maximize_credits.py`** — the scheduling solver. `maximize_credits()` groups course rows into sessions and runs a DFS with an upper-bound prune to find the session combination with maximum total units and **no meeting-time conflicts**, honoring optional time-range / blocked-slot / teacher constraints. Meeting times parse from strings like `"Mon 15:00-16:50"` via `parse_schedule`. `load_timetable()` re-headers the sheet because the real column names live in the first data row, not the Excel header. Also runnable as a CLI that emits a self-contained HTML calendar.
- **`media_dl/`** — a Flask **blueprint** (`/api/media-dl/*`) registered in `app.py`. `extractor.resolve()` dispatches by host to `bilibili.py` (native Bilibili API, avoids yt-dlp's 412s), `xhs.py` (Xiaohongshu), or `ytdlp.py` (everything else). `/resolve` returns direct media URLs; `/proxy` streams a remote file back through the server in **8 MB `Range` chunks with per-chunk retries** — needed for CDNs that require a `Referer` (e.g. Bilibili) and to survive mid-transfer drops. `/proxy` is locked to `_ALLOWED_PROXY_HOSTS` so it can't be used as an open relay. `analytics.py` best-effort-logs every event to `media_dl_events` and must never raise into the request path.

### Course catalog + AI course insights

The course explorer's detail panel (the `CourseInsightModal` in `index.html`) is fed by two committed static JSON files, served merged by `GET /api/course/<code>` (loaders in `app.py` cache both files by mtime, so regenerating them is picked up without a restart). These files are **generated**, not hand-edited, and are regenerated once per semester by a three-step pipeline (each script is a root-level `.py`, so the static-file guard blocks it from the web):

1. **`build_course_catalog.py`** → `course_catalog.json` (one entry per course code, ~1200). Parses the official **PDF course descriptions** (path hardcoded in the script — 990 catalog descriptions, regex-split on `CODE TITLE (n units) Pre-requisite(s): … Course Description: …`) and merges the semester **timetable xlsx** (`load_timetable` from `maximize_credits`) for teachers, sessions, offering unit/programme, `Requirements` (Chinese prereqs), and year. It also builds the **prerequisite graph** (`prereq_codes` from both sources, reverse edges as `unlocks`) and a deterministic same-dept `similar` seed.
2. **`split_enrich_batches.py`** → `enrich_work/batches/batch_NNN.json` (dept-homogeneous compact inputs, ~10 courses each; `enrich_work/` is gitignored).
3. **AI enrichment** → each batch is enriched by one subagent (Chinese `tagline`/`summary_zh`/`topics`/`difficulty`/`workload`/`career`/`further_study`/`recommend_for`/`tips`/`caution` + a `similar` list **chosen only from the batch's candidate codes**), writing `enrich_work/out/batch_NNN.json`. **`merge_enrichment.py`** validates every batch and merges into `course_enrichment.json` (keyed by code); it tolerates missing optional prose fields but rejects a course whose `similar` references a code outside the catalog. Re-run merge to see which batch ids still need work.

`/api/course/<code>` resolves the `prereqs`, `unlocks`, and `enrichment.similar` code lists into `{code, title, offered}` objects so the modal can render clickable course-to-course navigation. Enrichment is optional per course — the modal degrades gracefully (falls back to the English `description`) when a code has no enrichment yet. Two side files extend the endpoint, each with its own per-semester build script reading sources from `/tmp/srcdata/` (cp them there first — macOS TCC): **`build_textbooks.py`** → `course_textbooks.json` (per-course textbook list from the official textbook xls, rendered as Z-Library search links) and **`build_desc_extra.py`** → `course_descriptions_extra.json` (WPEC PDF via pdfplumber tables + CFL FE PDF via the main block regex; used as description fallback). The `UpdateNotice` banner in `index.html` announces releases once per user — bump its `NOTICE_VERSION` when shipping something announce-worthy.

**Programme degree maps.** `programme_requirements.json` (built by **`build_programmes.py`** from the ECM programme-handbook dump — 32 programmes × 2023/24/25 cohorts) powers the feature. The handbooks mark plan revisions with **red strikethrough** (struck = removed, red-not-struck = replacement), which plain text extraction cannot see — the parser therefore uses **pdfplumber geometry** (horizontal line/thin-rect through the middle band of a word) to drop struck course rows and struck unit cells, and validates listed-units vs declared section totals. It still recovers per-course Year/Sem placement from the grid columns and major-elective pools. Build-time-only deps: `pdfplumber`, `xlrd` (not in requirements.txt; not needed on the server). It powers `GET /api/programmes` and `POST /api/programme-map` `{programme, cohort, completed[]}`. The latter checks completed courses against each requirement section (Major Required / BBA Core / University Core exact-match; Major Electives against the pool; GE and Free Electives are **estimated** from leftover codes, GE by `GC|GT|GF` prefix) and returns per-section + overall unit progress. Front-end `ProgrammeMapView` (nav 选课 > 专业地图, BETA) renders it with transcript upload; profile persisted in localStorage.

**Course equivalences.** `course_equivalences.json` (built by **`build_equivalences.py`** purely from the committed catalog's prereq texts) stores symmetric pairs from three signals: 未曾修读过 mutual-exclusion lists (strongest — the registry's own substitution relation, e.g. DS1013↔AI1003), EN slash cross-listings (ACCT2003/ACCT2043), and 或者-alternative pairs where one code is a legacy code absent from the catalog (GCLA1903↔UCLC1013). No transitive closure. Used three ways: `/api/course` returns an `equivalents` list (modal 等价/互斥课程 section); `_resolve_course_refs` attaches `equiv` to legacy prereq chips (rendered as clickable "≈ current-code"); and `/api/programme-map` auto-satisfies an uncompleted requirement via a completed equivalent (`via` field, ≈ badge) — **guarded by title containment**, because mutual exclusion alone means overlap, not substitutability (the official audit counts DS1013 for AI1003 but does NOT count MATH1073 for MATH1123).

**Semester scopes.** The explorer's search scope selector is backed by `GET /api/semesters` plus `GET /api/courses?semester=<key>`: `current` (default, live from the timetable xlsx), `all` (current offerings + one synthesized card per not-offered catalog course), or a baked historical key (e.g. `2526S1`). Historical files `course_semester_<key>.json` + `semesters_index.json` are generated by **`build_semesters.py`** from the ECM timetables — copy the source xlsx/xls into `/tmp/sem/` first (macOS TCC: never point python at `~/Documents` directly, it can strip the session's Desktop read access). Non-current courses show a "本学期未开设" badge and can't be added to the cart.

### SkillPath: skills, careers, and the PPR recommender

A second data layer maps courses to job-market skills and careers, and drives a career-goal course recommender. It reuses extracted data from a sibling Big-Data project (`/Users/xhlm/Desktop/Study/大数据/小组项目` — course→skill extraction + a 124k-row LinkedIn jobs dataset). **`build_skillpath.py`** rebuilds clean joins around a curated, UIC-relevant set of ~46 target careers (keyword-matched against LinkedIn titles) and emits: `skillpath_courses.json` (code → skills + matched careers/salary), `skillpath_careers.json` (career → weighted skills, salary quartiles, top courses), `skillpath_skills.json`, plus a **Personalized-PageRank graph** as `skillpath_graph.npz` (a column-normalised scipy sparse transition matrix over course/skill/career nodes) with `skillpath_nodes.json` index maps.

At runtime `app.py` loads the `.npz` + node maps once (numpy/scipy only — no networkx, no pickle) and serves:
- `GET /api/course/<code>` — now also returns a `skillpath` block (skills + careers).
- `GET /api/careers` — the curated careers with salary/skills, for the picker.
- `POST /api/recommend` `{career, completed[], offered_only}` — runs live PPR (`_run_ppr`, ~60 power-iterations) with the teleport set biased toward the target career and its **skill gap** (career skills the student's completed courses don't already cover), then returns ranked courses with **bridge-skill explanations**, a skill-gap breakdown, and a "why-not" list (filtered: already taken / not offered). The front-end `CareerPlannerView` (nav key `career`, grouped under the "选课" nav submenu) renders this.
- `POST /api/parse-transcript` — accepts an uploaded BNBU/UIC **transcript or graduation-audit PDF** and returns the student's completed (passed) course codes, for one-click filling of the recommender's completed-courses field. `extract_completed_course_codes` handles both layouts (the "successfully completed" vs "failed/incomplete/to be taken" section structure, and the per-course passing-grade structure) and normalises `AI 2023`→`AI2023`. **Requires `pypdf`** — a runtime dependency (also used offline by `build_course_catalog.py`); install it in the server venv.

**Salary caveat:** the jobs data is a 2023-24 US LinkedIn dataset (USD/yr), surfaced with that disclaimer in the UI — it is not a Greater-Bay-Area local figure. Skills are LLM-extracted and carry some noise.

### Frontend: no build step

- **`index.html`** (~220 KB) is the main SPA: a single file using React 18 + in-browser Babel (`<script type="text/babel">`) + Tailwind + Lucide, all loaded from local `/vendor/` with a CDN `onerror` fallback. There is no bundler — edit the JSX directly inside `index.html`; **validate a change by running the babel-standalone transform over the `<script type="text/babel">` block via node, and headless-render the touched component with jsdom** (a single JSX error blanks the whole page). Views are switched by a `currentView` state string (`home`, `explorer`, `optimizer`, `career`, `programme`, `classrooms`, `ddl`, `teachers`, `toolbox`, `settings`); the top nav groups the four course views (课程/排课/职业规划/专业地图) under a `选课` submenu. Course picks live in a `selectedCodes` cart (`CartWidget`, persisted to `localStorage`); the rich course detail is `CourseInsightModal`. The visual system is a "paper/book" aesthetic (CSS vars `--paper`/`--ink`/`--signal`) for chrome + a neo-brutalist card style (thick black borders, hard shadows, brand green `#d6ff62`) inside the tool views.
- **Standalone static pages** served from their own directories, independent of the SPA: `stats/index.html` (analytics dashboard reading `/api/analytics/summary`), `media-dl/index.html` (downloader UI), `eatwhat/index.html` ("今天吃什么" food picker, Three.js), `print-setup/index.html` (campus printer setup guide), and the legacy `ddl.html` / `todolist.html`.
- **Two sibling apps on their own subdomains** (separate deployments, not served by this Flask app) are linked from the home chapter list + toolbox: **SlideCraft** (`ppt.bnbscheduler.top`, AI slide generation) and **OmniChat** (`chat.bnbscheduler.top`, multi-model chat + text-to-image/video), which share one account/credits system with each other.

### Standalone sub-projects (not wired into Flask)

- **`eatwhat/`** — its own scraper (`crawler_25doer.py`) + cleaner (`clean_data.py`) producing `food_25doer.db` and CSVs consumed by `eatwhat/index.html`.
- **`print-setup/`** — one-click campus-printing installers fetched via `irm | iex` (PowerShell) and `curl | bash` (macOS). Note: `app.py` registers `.ps1` and `.command` as `text/plain; charset=utf-8` specifically so their embedded Chinese doesn't get mangled by PowerShell 5.1's default Windows-1252 decoding.

### Email reminders

DDL reminder emails go out via SMTP, configured entirely through env vars (see `EMAIL_SETUP.md`); no SMTP secret is stored in the DB. The cron-triggered `POST /api/notifications/dispatch` (guarded by the `X-Notification-Secret` header) sends one reminder for the closest configured window (72/24/3/1h), deduped by a unique index on `email_notification_deliveries` and capped at 3 failed attempts. Times render in Beijing time; every email carries a one-click unsubscribe token.

### Deployment

Push to `main` triggers `.github/workflows/deploy-maxcourse.yml`, which SSHes to the production host (`103.106.188.87`, `/www/wwwroot/maxcourse/`, BT-panel/宝塔) and runs `/usr/local/bin/deploy-maxcourse.sh`: it backs up `maxcourse.db` and **refuses the deploy if the push modifies `maxcourse.db`**, then `git pull --ff-only`, `pip install -r requirements.txt`, runs the `unittest` suite, and `systemctl restart maxcourse.service`. The app runs as `python app.py` under **systemd** (not gunicorn), so a new runtime dep must land in `requirements.txt` or the endpoint using it 500s in prod (the loaders return `{}` / degrade, e.g. `/api/recommend` → 503 without scipy). The GitHub-Actions SSH step is occasionally flaky (`Connection reset by peer`); just re-run the failed job (`gh run rerun <id> --failed`). Data files (`course_catalog.json`, `course_enrichment.json`, `skillpath_*`, `programme_requirements.json`, `course_*.json`, `semesters_index.json`) are committed and shipped as-is; regenerate them per semester with the root-level `build_*.py` scripts.
