"""reddit_scraper - scrape Reddit threads (post + comments) from a list of URLs."""

from .scraper import RedditScraper, Post, Comment, ThreadResult, ScrapeError

__all__ = ["RedditScraper", "Post", "Comment", "ThreadResult", "ScrapeError"]
__version__ = "0.1.0"
