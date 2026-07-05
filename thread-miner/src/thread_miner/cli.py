"""M8: click CLI. `tm ingest | fetch | analyze | report | run | status`."""

from __future__ import annotations

import logging
import sys

import click

from . import PipelineError, db, load_config, load_env
from .ingest import IngestError, batch_summary, ingest_file

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def _open(ctx):
    cfg = load_config(ctx.obj["config"])
    conn = db.connect(cfg["paths"]["db"])
    return cfg, conn


def _require_env(keys: list[str]) -> dict:
    env = load_env()
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise SystemExit(
            f"missing credentials: {', '.join(missing)}. Copy .env.example to .env "
            "and fill it in (see README, Manual Pre-Build Actions)."
        )
    return env


REDDIT_KEYS = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"]


@click.group()
@click.option("--config", default="config.yaml", show_default=True,
              help="Path to config.yaml")
@click.pass_context
def main(ctx, config):
    """Thread Miner: Reddit discussion intelligence pipeline."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@main.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--tag-col", default="tag", show_default=True,
              help="Spreadsheet column to use as the thread tag")
@click.pass_context
def ingest(ctx, file, tag_col):
    """Ingest a CSV/XLSX of Reddit post URLs. Prints the batch_id."""
    cfg, conn = _open(ctx)
    try:
        batch_id = ingest_file(
            conn, file, tag_col=tag_col,
            share_resolve_timeout=cfg["thresholds"]["SHARE_RESOLVE_TIMEOUT"],
        )
    except IngestError as exc:
        raise SystemExit(f"error: {exc}")
    summary = batch_summary(conn, batch_id)
    click.echo(f"batch_id: {batch_id}")
    click.echo(f"rows: {summary['rows']}, unique posts: {summary['unique_posts']}, "
               f"statuses: {summary['by_status']}")


@main.command()
@click.argument("batch_id")
@click.option("--force", is_flag=True, help="Re-fetch even inside the refetch TTL")
@click.option("--tier2-only", is_flag=True, help="Skip Tier 1 entirely (debugging)")
@click.pass_context
def fetch(ctx, batch_id, force, tier2_only):
    """Fetch every unique post in a batch."""
    from .router import fetch_batch

    cfg, conn = _open(ctx)
    env = _require_env(REDDIT_KEYS) if not tier2_only else load_env()
    summary = fetch_batch(conn, cfg, env, batch_id, force=force, tier2_only=tier2_only)
    click.echo(f"fetch summary: {summary}")
    if summary["failed"] and summary["fetched"]:
        click.echo("partial failures occurred; see fetch_log / tm status", err=True)
    if summary["to_fetch"] and not summary["fetched"]:
        sys.exit(1)


@main.command()
@click.argument("batch_id")
@click.option("--resume", "resume_run_id", default=None, metavar="RUN_ID",
              help="Resume an interrupted analysis run")
@click.option("--no-batch", is_flag=True, help="Force synchronous API calls")
@click.pass_context
def analyze(ctx, batch_id, resume_run_id, no_batch):
    """Run LLM extraction + synthesis + rollup for a fetched batch."""
    from .analysis import run_analysis
    from .rollup import run_rollup

    cfg, conn = _open(ctx)
    env = _require_env(["ANTHROPIC_API_KEY"])
    try:
        run_id = run_analysis(conn, cfg, env, batch_id,
                              resume_run_id=resume_run_id, no_batch=no_batch)
        run_rollup(conn, cfg, env, run_id)
    except PipelineError as exc:
        raise SystemExit(f"error: {exc}")
    click.echo(f"run_id: {run_id}")


@main.command()
@click.argument("run_id")
@click.pass_context
def report(ctx, run_id):
    """Render report.html and question_bank.csv for a finished run."""
    from .report import render_report

    cfg, conn = _open(ctx)
    try:
        path = render_report(conn, cfg, run_id)
    except PipelineError as exc:
        raise SystemExit(f"error: {exc}")
    click.echo(f"report: {path}")
    click.echo(f"question bank: {path.parent / 'question_bank.csv'}")


@main.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--tag-col", default="tag", show_default=True)
@click.pass_context
def run(ctx, file, tag_col):
    """All four stages: ingest, fetch, analyze, report."""
    from .analysis import run_analysis
    from .report import render_report
    from .rollup import run_rollup
    from .router import fetch_batch

    cfg, conn = _open(ctx)
    env = _require_env(REDDIT_KEYS + ["ANTHROPIC_API_KEY"])

    batch_id = ingest_file(
        conn, file, tag_col=tag_col,
        share_resolve_timeout=cfg["thresholds"]["SHARE_RESOLVE_TIMEOUT"],
    )
    click.echo(f"batch_id: {batch_id}")
    summary = fetch_batch(conn, cfg, env, batch_id)
    click.echo(f"fetch summary: {summary}")
    if not summary["fetched"]:
        raise SystemExit("no threads fetched; aborting before analysis")

    try:
        run_id = run_analysis(conn, cfg, env, batch_id)
        run_rollup(conn, cfg, env, run_id)
        path = render_report(conn, cfg, run_id)
    except PipelineError as exc:
        raise SystemExit(f"error: {exc}")
    click.echo(f"run_id: {run_id}")
    click.echo(f"report: {path}")


@main.command()
@click.argument("ident")
@click.pass_context
def status(ctx, ident):
    """Show stage and fetch-log summary for a run_id or batch_id."""
    cfg, conn = _open(ctx)
    run = db.get_run(conn, ident)
    batch_id = run["batch_id"] if run else ident
    if run:
        click.echo(f"run {run['run_id']}: stage={run['stage']} "
                   f"anthropic_batch={run['anthropic_batch_id'] or '-'} "
                   f"updated={run['updated_at']}")
    batch = db.get_batch(conn, batch_id)
    if batch is None:
        raise SystemExit("not a known run_id or batch_id")
    click.echo(f"batch {batch_id}: {batch['source_filename']} "
               f"({batch['row_count']} rows, created {batch['created_at']})")
    click.echo(f"rows: {batch_summary(conn, batch_id)['by_status']}")
    posts = db.fetched_posts_for_batch(conn, batch_id)
    events = db.fetch_log_summary(conn, [p["post_fullname"] for p in posts])
    for e in events:
        click.echo(f"  fetch_log: tier={e['tier']} {e['event']}: {e['n']}")


if __name__ == "__main__":
    main()
