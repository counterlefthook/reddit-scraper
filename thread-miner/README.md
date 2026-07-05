# Thread Miner

Reddit discussion intelligence pipeline. Ingests a spreadsheet of Reddit post
URLs, fetches each post and its full comment tree, runs an LLM extraction and
synthesis pass (Claude, temperature 0, schema-validated), and produces a
content research report: weighted sentiment, a ranked question bank, theme
clusters, audience vocabulary, and content angles, all traceable back to
source comments.

Built to the Thread Miner v1.0 build specification. Core rules:

- **The determinism line.** Pipelines compute all metrics (`metrics.py` and
  SQL). LLMs interpret and extract only, at temperature 0 with pydantic
  validation. No number in a report comes from an LLM.
- **Runs from a residential IP** (laptop or homelab). No deployment
  scaffolding by design; Reddit blocks datacenter IP ranges.
- **Tier 1 (OAuth via PRAW) is the primary transport.** Tier 2 (old.reddit
  HTML via Playwright) is break-glass fallback, activated by the router on
  repeated 403s.
- **Read-only.** No Reddit write operations exist in this repo.
- **Raw data preserved.** Every fetched thread lands in
  `data/raw/{post_id}.json.gz` before parsing, so analysis re-runs without
  re-fetching.

## Setup

Requires Python 3.11+.

```bash
cd thread-miner
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Manual pre-build actions (once)

1. Create a Reddit app at <https://www.reddit.com/prefs/apps>, type **script**.
   Record the client id and secret.
2. `cp .env.example .env` and fill in `REDDIT_CLIENT_ID`,
   `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` (format:
   `platform:app-id:version (by /u/USERNAME)`), and `ANTHROPIC_API_KEY`.
3. Tier 2 fallback only: `playwright install chromium`.

## Usage

Input: CSV or XLSX with a required `url` column and an optional tag column
(free text, groups threads in the report). Other columns are preserved but
ignored. Accepted URL forms: `reddit.com`/`old`/`new`/`np`/`m`/`sh` hosts,
`redd.it` short links, comment permalinks (the full thread is fetched and the
focused comment recorded), mobile `/s/` share links, and URLs with tracking
parameters.

```bash
tm run my_urls.xlsx --tag-col topic      # all four stages
```

Or stage by stage:

```bash
tm ingest my_urls.csv --tag-col topic    # prints batch_id
tm fetch  <batch_id> [--force]
tm analyze <batch_id> [--resume RUN_ID] [--no-batch]
tm report <run_id>
tm status <run_id|batch_id>
```

Outputs land in `data/reports/{run_id}/`:

- `report.html`: self-contained (zero external requests), with run summary,
  executive summary, cross-thread themes, a sortable question bank,
  per-thread cards (sentiment index and distribution, themes, content angles,
  questions, contrarian takes, source-comment citation links), and a fetch
  appendix.
- `question_bank.csv`: canonical questions ranked by
  `members x (1 + log10(1 + total member score))`.

### Local web UI (single user)

```bash
pip install -e ".[local]"
streamlit run app.py
```

One page: upload the spreadsheet, pick the tag column, run, download the
report and question bank, preview inline.

## Hosted team web app

A multi-user web app lives in `webapp/`: teammates log in with a shared
password, upload a CSV/XLSX, the pipeline runs as a background job (survives
closed tabs and container restarts), and the report + question bank are
viewable and downloadable in the browser, with run history.

Run it locally:

```bash
pip install -e ".[web]"
APP_PASSWORD=pick-a-password SECRET_KEY=any-long-random-string \
  uvicorn webapp.main:app --port 8000
```

(Windows PowerShell: `$env:APP_PASSWORD="pick-a-password"; $env:SECRET_KEY="any-long-random-string"; uvicorn webapp.main:app --port 8000`)

### Deploy to Railway (~$5-10/month)

1. Push this repository to GitHub.
2. In [Railway](https://railway.app): **New Project → Deploy from GitHub repo**.
   If the repo root is not `thread-miner/`, set the service's Root Directory
   to `thread-miner`. The Dockerfile is detected automatically.
3. Service → **Variables** — add:
   `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`,
   `ANTHROPIC_API_KEY`, `APP_PASSWORD` (what the team types to log in),
   `SECRET_KEY` (any long random string), `TIER2_ENABLED=false`.
4. Service → **Volume** — mount path `/app/data` (holds the database, raw
   thread archives, uploads, and reports across restarts and deploys).
5. Settings → Networking → **Generate Domain**. Share the URL and the team
   password. Keep replicas at 1 (SQLite requires a single writer).

**Fetching from the cloud requires Reddit API credentials** (OAuth, Tier 1).
Reddit blocks anonymous HTML scraping from datacenter IPs, so the Tier 2
browser fallback is disabled on servers; `TIER2_ENABLED=true` is only for
deployments on a residential IP (a home PC or homelab). Until your Reddit API
access is approved, uploads will fail at the fetch stage with a message
saying exactly that.

## Behavior notes

- Anonymous batches are rate limited client-side at 90 requests/minute on
  Tier 1 (below Reddit's 100 QPM OAuth cap); Tier 2 sleeps 2.5 to 4.5 seconds
  between navigations.
- Threads larger than the fetch budget (`MAX_MORE_CALLS`) are truncated and
  flagged, visibly, in the report.
- Posts already fetched within `REFETCH_TTL_DAYS` (14) are skipped unless
  `--force`.
- Interrupted analysis runs resume with `tm analyze <batch_id> --resume
  <run_id>`; Batch API state is persisted in `analysis_runs`.
- Thresholds and model ids live in `config.yaml`; secrets live in `.env`.

## Tests

```bash
pip install pytest && python -m pytest tests/
```

`tests/fixtures/urls_mixed.csv` covers every accepted URL form, the duplicate
pair, an invalid URL, and a 404 id (acceptance test A1).

## Scope guarantees (v1)

Explicit post URLs only: no subreddit crawling, keyword search, scheduling,
warehouse export, author profiling, proxy rotation, or anti-detection
tooling. Usernames are stored for attribution and traceability only.
