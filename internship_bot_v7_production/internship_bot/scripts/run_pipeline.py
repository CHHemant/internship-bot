"""
CLI entrypoint — run the full pipeline from terminal.

Usage:
  python scripts/run_pipeline.py --resume resume.pdf --prefs prefs.json
  python scripts/run_pipeline.py --resume resume.pdf --prefs prefs.json --no-dry-run

prefs.json example:
{
  "target_countries": ["usa", "germany", "canada"],
  "target_domains": ["ML research", "bioinformatics"],
  "min_deadline_days": 7,
  "work_auth": {"usa": false, "germany": false, "canada": false},
  "max_concurrent_apps": 3,
  "dry_run": true
}
"""

from __future__ import annotations
import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
import structlog

app = typer.Typer(help="Internship Auto-Apply Bot")
console = Console()
log = structlog.get_logger()


@app.command()
def run(
    resume: Path = typer.Option(..., help="Path to master resume PDF or TXT"),
    prefs: Path  = typer.Option(..., help="Path to prefs.json"),
    no_dry_run: bool = typer.Option(False, "--no-dry-run", help="Actually submit (default: dry run)"),
    max_apps: int = typer.Option(10, help="Max listings to process this run"),
    log_level: str = typer.Option("INFO", help="INFO | DEBUG | WARNING"),
):
    """Run the full internship application pipeline."""
    import logging
    logging.basicConfig(level=log_level)

    asyncio.run(_run(resume, prefs, no_dry_run, max_apps))


async def _run(resume_path: Path, prefs_path: Path, no_dry_run: bool, max_apps: int):
    from models.schemas import UserPrefs, Country
    from agents.job_discovery.agent import JobDiscoveryAgent

    console.rule("[bold blue]Internship Auto-Apply Bot")

    # ── Load prefs ────────────────────────────────────────────────────────────
    raw_prefs = json.loads(prefs_path.read_text())
    if no_dry_run:
        raw_prefs["dry_run"] = False
        console.print("[bold red]⚠  DRY RUN OFF — real submissions will be sent!")
    user_prefs = UserPrefs(**raw_prefs)

    # ── Parse master resume ───────────────────────────────────────────────────
    from agents.job_discovery.resume_parser import parse_resume
    console.print(f"Parsing resume: [cyan]{resume_path}[/cyan]")
    master_resume = await parse_resume(resume_path)
    console.print(f"  Skills found: {len(master_resume.skills)}")
    console.print(f"  Experiences:  {len(master_resume.experiences)}")

    # ── Discover jobs ─────────────────────────────────────────────────────────
    console.print("\nDiscovering internship listings...")
    discovery = JobDiscoveryAgent()
    listings = await discovery.run(master_resume, user_prefs)
    listings = listings[:max_apps]
    console.print(f"  Found {len(listings)} listings above threshold")

    # ── Run pipeline per listing (with concurrency limit) ─────────────────────
    from orchestrator.pipeline import run_application
    semaphore = asyncio.Semaphore(user_prefs.max_concurrent_apps)
    records = []

    async def process(listing):
        async with semaphore:
            console.print(f"  → {listing.title} @ {listing.company} ({listing.country.value.upper()})")
            return await run_application(listing, master_resume, user_prefs)

    records = await asyncio.gather(*[process(l) for l in listings], return_exceptions=True)

    # ── Summary table ─────────────────────────────────────────────────────────
    console.rule("[bold green]Results")
    table = Table("Company", "Role", "Country", "Status", "Confirmation")
    for r in records:
        if isinstance(r, Exception):
            table.add_row("ERROR", str(r)[:40], "-", "ERROR", "-")
        else:
            table.add_row(
                r.listing.company,
                r.listing.title[:30],
                r.listing.country.value.upper(),
                r.status.value,
                r.confirmation_id or "-",
            )
    console.print(table)


if __name__ == "__main__":
    app()
