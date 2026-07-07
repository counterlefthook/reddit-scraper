# ROADMAP — Risks, Blind Spots, Multi-Channel Expansion, and Phased Plan

**Date:** 2026-07-05. Companion to `HANDOFF.md` (state + decisions). This file: where it breaks, what's missing, and where it goes.

---

## Part 1 — Pre-mortem: if this fails in 6–12 months, it was one of these

Ranked by (likelihood × damage), with the mitigation that belongs in the plan.

### 1. Reddit access dies (the existential risk)
Everything downstream depends on one upstream source that actively restricts access. Concrete ways it happens:
- **API application denied or approved-then-revoked.** Reddit now monetizes its data; "content strategy research for a business" may read as commercial use under a non-commercial approval. A mismatch can get the API app banned.
- **Tier 2 stops working.** old.reddit's retirement has been rumored for years; one markup change disables the fallback (it fails loudly by design, but it still fails). Reddit also keeps tightening anti-bot measures on datacenter *and* residential traffic.
- **Rate limits or pricing change** and the 90 QPM free tier shrinks or becomes paid.

**Mitigations:** (a) be honest in the API application about commercial intent — a denied application is recoverable, a banned account is not; (b) keep the raw gzipped archives forever — analysis can always be re-run without re-fetching; (c) treat multi-channel expansion (Part 3) not as a feature but as **risk diversification**; (d) budget line-item for a commercial data provider (Apify/Bright Data ~$50–500/mo) as the break-glass path.

### 2. Silent quality rot (the sneakiest risk)
Nothing crashes — the reports just quietly get worse. Causes: Anthropic retires `claude-sonnet-4-6`/`claude-haiku-4-5` and an auto-swap changes behavior; a prompt tuned for today's models mis-fires on tomorrow's; Reddit changes its JSON subtly; nobody is reading the reports closely enough to notice. In 9 months someone makes a content decision on a report that's been subtly broken for a quarter.

**Mitigations:** (a) a **golden-set eval**: one frozen fixture thread with known-good expected outputs, run after any model/prompt/dependency change (`tests/` already has the skeleton); (b) a monthly 10-minute ritual: open the newest report, click 5 citation links, sanity-check the sentiment bar against the actual thread; (c) model IDs live in `config.yaml` — upgrades are deliberate config changes, never silent.

### 3. Operational decay (the boring, most probable risk)
The worker is one container (or a PC in a closet). Twelve months of entropy: the disk fills with raw archives, the Railway card expires, a deploy leaves the worker crashed and **nobody is alerted** — teammates just see jobs stuck "queued", conclude the tool is broken, and stop using it. Two weeks of stuck jobs kills adoption permanently.

**Mitigations:** (a) heartbeat monitoring — worker pings a free service (healthchecks.io / UptimeRobot) each loop; you get an email when it stops; (b) retention job — delete uploads and raw archives older than N days (Supabase holds the deliverables); (c) a `RUNBOOK.md` with the 5 restart/diagnose commands a non-expert can follow; (d) treat the worker's disk as **disposable cache** — the durable record is Supabase (jobs, artifacts) + git (code). If the worker box burns down, you redeploy in 10 minutes and lose nothing.

### 4. The bus factor is 1 (you), and the maintainer is an AI session
Nobody on the team can code. Every fix requires reopening an AI session with enough context. If docs drift from reality, each fix session starts from archaeology, becomes expensive and error-prone, and eventually it's easier to abandon the tool.

**Mitigations:** (a) HANDOFF.md/ROADMAP.md/RUNBOOK.md **must be updated in the same commit as the change they describe** — stale docs are worse than none; (b) prefer boring managed services over clever code; (c) keep everything in git, including this file; (d) each AI session ends by updating the handoff, exactly like this one did.

### 5. Cost surprise
Cheap today ($5 hosting + cents per run) — but costs scale with team enthusiasm: a 500-thread upload, weekly re-runs, Sonnet everywhere. A surprise $300 Anthropic invoice in month 7 triggers a shutdown reflex.

**Mitigations:** (a) hard monthly spend limit in the Anthropic console (Settings → Limits) — do this **today**, it's a checkbox; (b) per-job guard: estimate cost before analyzing (comment count × threshold) and refuse jobs above a config cap with a friendly message; (c) show tokens-spent per job in the jobs table so cost is visible, not abstract.

### 6. Team adoption failure (the product risk)
The pipeline works but doesn't change anyone's Monday: reports get skimmed once, the question bank never reaches the people writing content, and by month 9 it's shelf-ware. This is the most common failure mode for internal tools and it has nothing to do with code.

**Mitigations:** (a) the "self-contained results" decision was right for v1 but must be revisited — **structured results in Supabase that the media-planning module can query** is what makes this compounding rather than disposable (Phase 3); (b) instrument usage (jobs/month, report views) and look at it; (c) close the loop: a "mark this question as used in content" button turns research into a workflow, not a document.

### 7. Legal/compliance drift
Low probability, high cleanup cost. Scraped usernames are personal data (GDPR/CCPA if you serve EU/CA markets); Reddit's ToS restricts republishing user content; reports shared outside the team could leak quoted comments into client decks.

**Mitigations:** the spec already forbids author profiling — keep that line; add a data-retention statement (raw data deleted after N days); treat reports as internal work product; if the platform becomes client-facing, get an actual legal opinion before showing scraped content to clients.

---

## Part 2 — What you're not considering (blind spots)

1. **Freshness semantics.** A report is a snapshot; threads keep growing. There's no "re-run this job" or "this report is 4 months old" indicator. Decide what stale means, add a refetch/re-run button (the 14-day refetch TTL already half-supports this).
2. **Duplicate work across teammates.** Two people uploading overlapping URL lists silently double cost. The dedup exists per-batch, not across jobs. Cheap fix: warn when a URL was analyzed in the last N days, link to the prior report.
3. **Who watches the queue?** One-job-at-a-time is correct now; with 4 apps and a team it becomes hours of wait at some point. Know the escalation path (documented in Phase 3) so it's a planned upgrade, not a crisis.
4. **Backups are asymmetric.** Supabase has backups; the worker's SQLite doesn't need them **only if** you hold the "worker disk is disposable cache" line. The moment someone treats worker-local data as the record, you have an unbacked-up database. Write the line down (done — see risk 3) and don't cross it.
5. **Secrets lifecycle.** Service-role key, Anthropic key, Reddit secret — all long-lived, all in env vars. Calendar a 6-month rotation; know where each key lives (HANDOFF lists them).
6. **The spec is now partially fiction.** Thread Miner v1.0's spec says "local machine, no deployment scaffolding" — reality has moved. Fine, but the *current* truth must live in HANDOFF/ROADMAP, and the spec should be treated as historical context, not instructions.
7. **Nobody owns Mondays.** Every mitigation above that says "monthly ritual" needs a name and a calendar entry, or it won't happen. That's you until it's someone else — write it in the runbook.

---

## Part 3 — Adding other channels (websites, forums, YouTube, reviews)

**The good news: the architecture is already channel-shaped.** The pipeline is `ingest(URLs) → fetch(raw JSON) → store(posts + comments) → analyze → report`, and the analysis layer only ever sees the `posts`/`comments` tables — it doesn't know or care that they came from Reddit. Anything that maps to "a piece of content plus a tree (or list) of responses with author/score/time" rides the existing rails unchanged, including the LLM analysis, metrics, question bank, and report.

### The refactor that unlocks it (do once, ~Phase 2)

Introduce a **source adapter** interface; Reddit becomes the first adapter rather than the hard-coded assumption:

```
class SourceAdapter:
    name: str                                  # "reddit", "discourse", "youtube", ...
    def matches(self, url: str) -> bool        # which adapter claims this URL
    def normalize(self, url: str) -> str       # canonical form + dedup key
    def fetch(self, url: str) -> RawThread     # the tier1 raw-JSON shape:
                                               #   {post: {...}, comments: [...]}
```

Mechanical changes: dispatch `normalize.py` and `router.py` through the adapter registry; add a `source` column to `posts` and `ingest_rows` (default `'reddit'` — one migration); show source badges in the report. The `RawThread` shape already exists (`fetch_tier1.py` produces it; `fetch_tier2.py` already conforms to it — proof the seam works). Per-channel politeness (rate limits, delays) lives in each adapter.

### Channel difficulty map (ordered by value-for-effort)

| Channel | Method | Effort | Legal/stability | Notes |
|---|---|---|---|---|
| **Discourse forums** (many niche communities) | Public JSON API — append `.json`, like Reddit | Small | Solid | Closest cousin to Reddit; best first new adapter |
| **YouTube comments** | Official Data API (free quota) | Small-medium | Solid | Comments = flat threads; huge audience-language source |
| **App-store / G2 / Amazon reviews** | Via data-API vendors (Apify etc.) | Medium | Vendor handles ToS | A review = a depth-0 comment; fits the model perfectly |
| **Generic websites/blogs** | Article extraction (trafilatura); article = "post", usually 0 comments | Medium | Generally OK for analysis | Different value: it's *content* analysis, not *discussion* analysis — still useful for coverage analysis app |
| **Disqus comment sections** | Disqus API | Medium | OK | Covers many news/blog comment sections |
| **Quora / X / Facebook / Instagram / TikTok** | Hostile: paid APIs, aggressive anti-scraping, ToS bans | Large | Risky/expensive | Only via commercial data vendors, only with a strong business case |

**Recommendation:** after platform integration stabilizes, add **Discourse** first (structurally almost free given the adapter seam), then **YouTube**. Both are legally clean, stable, and immediately useful. Treat social networks as "buy, don't build."

### What multi-channel changes upstream
- Upload CSV stays the same — mixed URLs are fine; the registry routes each row to its adapter, unknown domains get a clear `unsupported_source` status.
- Prompts need only a light touch (a "source: YouTube comments" line in the chunk header); the schemas don't change.
- Cross-channel synthesis ("what are people asking about X across Reddit + YouTube + forums") is a Phase 4 rollup change, not a rewrite — the rollup already groups by tag; grouping by topic across sources is the same shape.

---

## Part 4 — Phased plan with implementation

### Phase 0 — Platform integration (now → ~2 weeks) — already specced in HANDOFF.md §6
Supabase `research_jobs` + buckets + RLS → platform UI module → Supabase-polling worker → deploy → end-to-end test → **merge branch to main**.
*Exit criteria:* a teammate uploads a CSV in the platform and gets a report without touching PowerShell.

### Phase 1 — Production hardening (month 1–2) — do BEFORE inviting the whole team
1. **Anthropic spend limit** in console (5 minutes, do immediately).
2. **Heartbeat monitoring**: worker pings healthchecks.io each loop; alert email on silence. (~20 lines.)
3. **Retention job**: on worker start + daily, delete uploads/raw archives older than `RETENTION_DAYS` (config; suggest 90). Report artifacts in Supabase are kept.
4. **Cost guard**: pre-analysis estimate (comments × chars ÷ tokens × price) vs `MAX_JOB_COST_USD` config; refuse with a clear message.
5. **Golden-set eval**: script that runs the fixture thread through analysis with the live model and diffs structure/sanity (not exact text); run after any model/prompt change.
6. **`RUNBOOK.md`**: restart worker, read logs, common failures (401 = key, fetch_failed = Reddit, stuck queue = worker down), who to call (i.e., which AI chat to open with which repos attached).
7. **Secrets rotation calendar** (6-month) + key inventory check.
*Exit criteria:* the worker can crash on a Friday and you know by Friday dinner, not month-end.

### Phase 2 — Channel adapter seam + first new channel (month 2–4)
1. Adapter interface + registry; move Reddit tier1/tier2 behind it; `source` column migration; report badges.
2. **Discourse adapter** (JSON API, politeness delay, same RawThread shape).
3. Unsupported-domain handling in ingest (clear per-row status, not job failure).
4. Tests: adapter contract tests with fixtures per channel (mirror the existing Reddit fixture pattern).
*Exit criteria:* one CSV mixing Reddit + Discourse URLs produces one unified report.

### Phase 3 — From tool to platform asset (month 4–6)
1. **Structured results into Supabase Postgres** (reverses the deliberate v1 deferral): `research_questions`, `research_themes`, `research_vocabulary` tables written by the worker per run — this is what lets media planning query audience language directly.
2. **Re-run/refresh**: "refresh this report" button (new job re-using the URL list; refetch TTL handles economy); staleness label on old reports.
3. **Cross-job dedup warning** ("6 of these URLs were analyzed 3 weeks ago → prior report").
4. **Usage + cost visibility**: per-job token/cost column; simple monthly usage query.
5. Queue scale-up **only if wait times actually hurt**: worker-side parallel analysis (fetch stays serialized per source's rate limit) before any Postgres/queue-service migration.
*Exit criteria:* another platform module consumes research data without a human copying anything.

### Phase 4 — Compounding value (month 6–12)
1. **YouTube adapter**; evaluate a review-data vendor for app-store/G2 if business case appears.
2. **Cross-channel topic reports**: one synthesis across sources for a named topic.
3. **Trend tracking**: scheduled monthly re-runs of a saved URL/topic list; report deltas ("new questions since last month") — this is where the tool stops being a snapshot machine and becomes a listening system.
4. Revisit: per-user roles, notifications (job-done email/Slack), client-facing report styling + the legal review that must precede it.

### Standing rituals (all phases)
- **Monthly (15 min):** open latest report, click 5 citations, glance at Anthropic spend + heartbeat status.
- **Quarterly (1 session):** dependency + model refresh with the golden-set eval as the gate; update HANDOFF/ROADMAP to match reality.
- **Every change session:** ends by updating the docs it invalidated.
