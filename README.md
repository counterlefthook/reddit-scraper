# Reddit Thread Scraper

Scrape Reddit threads — the post plus all comments, replies, and discussion —
from a list of post URLs in an **Excel file** (CSV and plain text work too),
and save everything to a clean Excel workbook.

No Reddit account or API key is needed: the tool uses Reddit's public JSON
endpoints and politely rate-limits itself.

## Features

- **Upload an Excel URL list** — links can be in any cell, any column, any
  sheet; duplicates are removed automatically. Full permalinks, `redd.it`
  short links, `old.reddit.com` links, and links with tracking parameters all
  work.
- **Full comment trees** — nested replies with depth and parent tracking, and
  optional expansion of comments hidden behind "load more comments".
- **Excel output** with three sheets:
  - `Posts` — title, author, subreddit, score, upvote ratio, comment count, date, selftext, flair, NSFW flag, permalink
  - `Comments` — one row per comment: body, author, score, date, depth, parent comment, whether the author is the OP, permalink
  - `Errors` — any URLs that failed (deleted posts, bad links) and why
- **Resilient** — one bad URL never kills the run; automatic retries with
  backoff on rate limits and network hiccups.
- Two ways to use it: a **web page** (upload → progress bar → download) or a
  **command line** tool.

## Setup

Requires Python 3.10+.

```bash
git clone <this repo>
cd reddit-scraper
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Option 1: Web app

```bash
python app.py
```

Open <http://127.0.0.1:5000>, upload your Excel file, watch the progress bar,
then download the results workbook.

## Option 2: Command line

```bash
python -m reddit_scraper.cli my_urls.xlsx -o results.xlsx
```

Useful flags:

| Flag | Default | Meaning |
|---|---|---|
| `-o / --output` | `reddit_results_<timestamp>.xlsx` | Where to save results |
| `--delay` | `6.5` | Seconds between requests to Reddit |
| `--no-expand-more` | off | Skip "load more comments" stubs (much faster on huge threads) |
| `--max-more` | `20` | Max extra requests per thread when expanding hidden comments |

## Preparing your URL list

Any `.xlsx`, `.csv`, or `.txt` file works. The scraper scans every cell and
extracts anything that looks like a Reddit link, so your existing spreadsheet
layout is fine as-is. To generate a template:

```bash
python examples/make_sample_urls.py   # writes examples/sample_urls.xlsx
```

## Notes on rate limits

Reddit allows anonymous JSON access at roughly **10 requests per minute**.
The default 6.5-second delay stays under that. Each thread costs 1 request,
plus 1 request per 100 hidden "load more" comments when expansion is on. A
list of 50 typical threads takes roughly 10–20 minutes — leave the tab open,
progress updates live.

If you see `403 (blocked)` errors, Reddit is throttling your network: raise
`--delay`, retry later, or run from a different network.

## Running tests

```bash
pip install pytest
python -m pytest
```

## Legal / etiquette

Only public data is accessed. Be considerate: keep delays high, scrape only
what you need, and review [Reddit's User Agreement](https://www.redditinc.com/policies/user-agreement)
and API terms before using scraped data commercially.
