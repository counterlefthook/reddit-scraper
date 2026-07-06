# HANDOFF — Reddit Research (Thread Miner) → Platform Integration

**Date:** 2026-07-05
**Branch with all work:** `claude/reddit-scraper-url-list-yhc684` (not yet merged to main)
**Status:** Pipeline complete and verified with a real end-to-end run (CSV → Reddit fetch → Claude analysis → HTML report + question_bank.csv) on the owner's Windows PC. 54 automated tests pass.

**Purpose of this document:** the owner is opening a new chat session with the platform repo connected. That session should read this file first — it contains everything decided and built so far, so no context is lost.

---

## 1. The big picture

The owner (Chris) is building a multi-app marketing platform hosted on **Vercel** with **Supabase** (project exists; Supabase Auth already configured). Modules: media planning, measurement, coverage analysis, and **Reddit research** (this repo). Reddit research must appear as a section of the platform app: teammates upload a CSV/XLSX of Reddit post URLs, the pipeline runs automatically in the background, and they view/download the finished research report.

**The agreed architecture** (decided 2026-07-05):

```
Vercel platform app (UI, auth via Supabase)        <- user-facing
        |  upload CSV to Storage, insert job row,
        |  poll job row, link to artifacts
        v
Supabase (Postgres + Storage)                      <- the hub; ONLY interface
        ^
        |  poll for queued jobs, download CSV,
        |  push progress, upload report artifacts
        |
Thread Miner worker (always-on box: Railway ~$5/mo,
or the owner's home PC while Reddit approval is pending)
```

Why not everything on Vercel: research jobs run minutes-to-an-hour (rate-limited Reddit fetching + Claude Batch API polling) and need an always-on background process plus a persistent working disk — none of which Vercel's serverless model provides. The worker is invisible plumbing; it has **no public endpoints** and needs no user auth — it talks only to Supabase using the service-role key.

---

## 2. What exists in THIS repo

| Path | What it is |
|---|---|
| `thread-miner/` | **The real product.** Full pipeline built to a detailed spec ("Thread Miner v1.0"): ingest → fetch → LLM analysis → rollup → report. |
| `thread-miner/src/thread_miner/` | Pipeline modules: `ingest.py`/`normalize.py` (CSV/XLSX in, URL canonicalization), `fetch_tier1.py` (PRAW OAuth), `fetch_tier2.py` (Playwright + old.reddit fallback), `router.py` (transport routing), `analysis.py` (Claude extraction+synthesis, pydantic-validated, resumable), `rollup.py`, `metrics.py` (ALL math), `report.py` (self-contained HTML + question_bank.csv), `db.py` (SQLite, all SQL), `cli.py` (the `tm` command). |
| `thread-miner/webapp/` | Multi-user FastAPI web app + background worker: shared-password login, CSV upload, job queue in SQLite, live progress, report viewing/downloads, restart recovery. **`webapp/worker.py:process_job` is the template for the future Supabase worker** — its job-table logic maps 1:1. |
| `thread-miner/Dockerfile` | Deployable container (Railway-ready; see thread-miner/README.md "Hosted team web app"). |
| `thread-miner/tests/` | 54 tests incl. live-failure regressions and worker recovery. |
| Repo root (`app.py`, `reddit_scraper/`) | v0: a simple Flask scraper built before the spec arrived. Superseded by thread-miner; kept for reference. |

---

## 3. Decision log (what and why)

1. **Two-tier Reddit fetching.** Tier 1 = official OAuth API via PRAW (90 req/min, needs credentials). Tier 2 = old.reddit HTML via Playwright — break-glass fallback that works **only from residential IPs** (Reddit blocks datacenter IPs). Cloud deployments must use Tier 1 only (`TIER2_ENABLED=false`, the default).
2. **Reddit API approval is pending.** Reddit gated app creation behind their Responsible Builder Policy; the owner has applied for non-commercial API access. Until approved: fetching works only from the owner's home PC via Tier 2. **This is the go-live blocker for cloud fetching.**
3. **The determinism line (from the spec).** LLMs label/extract/summarize only; every number in reports is computed in `metrics.py` or SQL. Claude calls run at temperature 0 with pydantic schema validation; invented comment IDs are dropped so citations always resolve.
4. **Models are config, not code** (`thread-miner/config.yaml`): extraction = `claude-haiku-4-5`, synthesis = `claude-sonnet-4-6`. The owner sometimes sets both to haiku for budget runs (report-quality tradeoff; pennies of difference at small scale). ≤5 threads = synchronous API; more = Message Batches API (50% cheaper), resumable via persisted batch id.
5. **Bugs found in live testing, both fixed with regression tests:** (a) prompts must embed the actual JSON schema (`analysis.py:system_with_schema`) or the model invents field names; (b) the project `.env` must beat machine-wide env vars (a stale system-wide `ANTHROPIC_API_KEY` on the owner's PC caused mystery 401s; neutralized in code, but the variable still exists in his Windows system settings — deleting it needs admin rights).
6. **Hosted team app v1 = FastAPI + single worker thread + SQLite (WAL).** One job at a time (guarantees the Reddit rate cap), queue and stages persisted so container restarts resume mid-job. This app remains useful standalone/dev, but the **platform integration supersedes it as the user-facing surface**.
7. **Platform integration = Supabase as the only interface** (diagram above). The Vercel UI never talks to the worker directly. Results stay **self-contained per run** for now (report.html + question_bank.csv artifacts); pushing structured results into Postgres for cross-app queries (e.g. media planning reading audience vocabulary) is deliberately deferred.

---

## 4. Proposed integration contract (refine in the platform repo)

Not yet built — this is the starting design for the new session.

**Supabase table `research_jobs`:**
```
id uuid pk, created_at timestamptz, created_by uuid -> auth.users,
source_filename text, upload_path text,          -- Storage path of the CSV
status text check in ('queued','running','done','failed'),
phase text, pct int, error text,
report_path text, question_bank_path text,       -- Storage paths when done
started_at timestamptz, finished_at timestamptz
```
RLS: platform users can insert + read; only the service role updates.

**Storage buckets:** `research-uploads/` (CSV in), `research-reports/` (artifacts out).

**Worker loop** (adapt `webapp/worker.py`, using `supabase-py` with the service-role key): poll for `queued` → claim → download CSV → run the existing pipeline unchanged (its SQLite + raw archives stay on the worker's own disk) → stream `phase`/`pct` updates to the row → upload `report.html` + `question_bank.csv` → mark `done` (or `failed` with the error message). Progress-percentage logic to copy: `webapp/main.py:job_progress`.

**Platform UI module:** upload to Storage + insert row → jobs list / progress from the row (poll or Supabase Realtime) → render report from Storage (signed URL in an iframe) + download links.

---

## 5. How to run locally today (Windows PowerShell)

```powershell
git clone https://github.com/counterlefthook/reddit-scraper.git
cd reddit-scraper; git checkout claude/reddit-scraper-url-list-yhc684; cd thread-miner
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[web]"; pip install pytest
python -m pytest tests/          # expect: 54 passed
```

Secrets: `copy .env.example .env`, fill in `ANTHROPIC_API_KEY` (Reddit keys stay blank until approval).

**Command-line pipeline** (what the owner has already run successfully):
```powershell
tm ingest my_urls.csv                  # prints batch_id
tm fetch <batch_id> --tier2-only       # home-network only; drop the flag once Reddit creds exist
tm analyze <batch_id> --no-batch       # prints run_id
tm report <run_id>                     # data\reports\<run_id>\report.html
```

**Team web app** (full upload → progress → report experience, local):
```powershell
$env:APP_PASSWORD="pick-one"; $env:SECRET_KEY="any-long-random-text"; $env:TIER2_ENABLED="true"
uvicorn webapp.main:app --port 8000    # open http://localhost:8000
```

---

## 6. Next steps (for the new chat, in order)

1. **Supabase:** create the `research_jobs` table, the two Storage buckets, and RLS policies (section 4).
2. **Platform repo:** build the Reddit-research module — upload page, run history, progress view, report viewer/downloads — against the Supabase contract, matching the platform's existing look and auth.
3. **Worker:** add a Supabase mode to Thread Miner (e.g. new `webapp/supabase_worker.py`, reusing `process_job`'s structure; add `supabase` to the `web` extras). Keep the FastAPI app as a dev/standalone alternative.
4. **Deploy the worker:** Railway container (Dockerfile exists) once Reddit API credentials arrive; until then it can run on the owner's home PC with `TIER2_ENABLED=true` for real fetching.
5. **Env vars for the worker:** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`, `REDDIT_CLIENT_ID/SECRET/USER_AGENT`, `TIER2_ENABLED`.
6. **End-to-end test** with a small real CSV, then merge `claude/reddit-scraper-url-list-yhc684` to main (a PR was offered but not yet opened).
7. **Later:** structured results into Postgres for cross-app use; notifications; per-user roles.

## 7. Open items / gotchas

- **Reddit API approval pending** — the single external blocker for cloud fetching.
- An old invalid `ANTHROPIC_API_KEY` still sits in the owner's Windows *system* env vars (harmless now; needs admin rights to delete).
- Keep exactly **one** worker instance running (SQLite single-writer + Reddit rate cap).
- The Anthropic console holds the owner's real API key; spend limits can be set under Settings → Limits.
