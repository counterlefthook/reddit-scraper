"""Command-line interface: scrape an Excel/CSV list of Reddit URLs to Excel."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .excel_io import read_urls, write_results
from .scraper import RedditScraper


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reddit-scraper",
        description="Scrape Reddit threads (post + all comments) from a list of URLs "
                    "in an Excel/CSV/text file, and save the results to Excel.",
    )
    parser.add_argument("input", help="Path to .xlsx/.csv/.txt file containing Reddit post URLs")
    parser.add_argument(
        "-o", "--output",
        help="Output .xlsx path (default: reddit_results_<timestamp>.xlsx)",
    )
    parser.add_argument(
        "--delay", type=float, default=6.5,
        help="Seconds between requests to Reddit (default: 6.5; lower risks rate-limiting)",
    )
    parser.add_argument(
        "--no-expand-more", action="store_true",
        help="Skip fetching comments hidden behind 'load more comments' (faster)",
    )
    parser.add_argument(
        "--max-more", type=int, default=20,
        help="Max extra requests per thread to expand hidden comments (default: 20)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only print the summary")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        urls = read_urls(args.input)
    except FileNotFoundError:
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2
    if not urls:
        print("error: no Reddit URLs found in the input file", file=sys.stderr)
        return 2
    print(f"Found {len(urls)} Reddit URL(s) in {args.input}")

    scraper = RedditScraper(
        delay=args.delay,
        expand_more=not args.no_expand_more,
        max_more_requests=args.max_more,
        timeout=args.timeout,
    )
    results = scraper.scrape_all(urls)

    output = Path(args.output) if args.output else Path(
        f"reddit_results_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    )
    write_results(output, results)

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    total_comments = sum(len(r.comments) for r in ok)
    print(f"\nDone: {len(ok)} thread(s) scraped, {total_comments} comments total.")
    if failed:
        print(f"{len(failed)} URL(s) failed - see the 'Errors' sheet:")
        for r in failed:
            print(f"  - {r.source_url}: {r.error}")
    print(f"Results saved to: {output}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
