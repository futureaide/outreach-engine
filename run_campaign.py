"""
CLI Orchestrator — chains all pipeline steps.
Source (w/ ICP Pretest) → Verify → Enrich → Personalize → QA → Deploy → Notify

Supports --resume {run_id} to pick up from the last completed step
using cached intermediates in .tmp/{run_id}/.
"""

import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import click
import structlog
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from execution.campaign_strategy import (
    DEFAULT_CAMPAIGN_BRIEF_PATH,
    RUN_SCHEMA_VERSION,
    get_launch_settings,
    load_campaign_strategy,
)

load_dotenv()

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()
console = Console()

PROJECT_ROOT = Path(__file__).parent
TMP_DIR = PROJECT_ROOT / ".tmp"
PRETEST_SAMPLE_COUNT = 100
LOW_YIELD_MIN_VERIFIED = 10
LOW_YIELD_VERIFIED_RATIO = 0.03

# Per-lead API cost estimates used only for the in-run cost cap. These are
# not billed by us — they reflect what the upstream vendor charges. If the
# vendor changes their pricing, update here (or set the env var) so the cost
# cap stays accurate.
APIFY_COST_PER_LEAD = float(os.getenv("APIFY_COST_PER_LEAD", "0.02"))
REOON_COST_PER_LEAD = float(os.getenv("REOON_COST_PER_LEAD", "0.0012"))


# ---------------------------------------------------------------------------
# Intermediate file helpers (save + load for resume)
# ---------------------------------------------------------------------------

def _save_intermediate(run_id: str, step: str, data) -> Path:
    """Save intermediate data to .tmp/{run_id}/{step}.json"""
    run_dir = TMP_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{step}.json"

    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    return path


def _load_intermediate(run_id: str, step: str):
    """Load cached intermediate data. Returns None if not found."""
    path = TMP_DIR / run_id / f"{step}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("cache.load_error", step=step, error=str(e))
        return None


def _save_run_state(run_id: str, costs: dict, pretest_info: dict | None = None):
    """Persist cost tracking and pretest info so they survive a resume."""
    run_dir = TMP_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    with open(run_dir / "costs.json", "w") as f:
        json.dump(costs, f, indent=2)

    if pretest_info is not None:
        with open(run_dir / "pretest_info.json", "w") as f:
            json.dump(pretest_info, f, indent=2, default=str)


def _save_runtime_config(run_id: str, runtime_config: dict):
    """Persist schema + campaign brief metadata for safe resumes."""
    run_dir = TMP_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "runtime.json", "w") as f:
        json.dump(runtime_config, f, indent=2, default=str)


def _update_runtime_config(run_id: str, updates: dict):
    """Merge updates into runtime.json without dropping existing fields."""
    runtime_config = _load_runtime_config(run_id)
    runtime_config.update(updates)
    _save_runtime_config(run_id, runtime_config)


def _load_runtime_config(run_id: str) -> dict:
    """Load schema + campaign brief metadata from a previous run."""
    path = TMP_DIR / run_id / "runtime.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_run_state(run_id: str) -> tuple[dict, dict]:
    """Load persisted costs and pretest info from a previous run."""
    costs = {"apify_cost": 0.0, "reoon_cost": 0.0, "linkedin_cost": 0.0, "perplexity_cost": 0.0, "claude_cost": 0.0}
    pretest_info = {}

    costs_path = TMP_DIR / run_id / "costs.json"
    if costs_path.exists():
        try:
            with open(costs_path) as f:
                costs.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass

    pretest_path = TMP_DIR / run_id / "pretest_info.json"
    if pretest_path.exists():
        try:
            with open(pretest_path) as f:
                pretest_info = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    return costs, pretest_info


def _collect_iteration_qa_results(run_id: str) -> tuple[list[dict], list[dict], int]:
    """Rebuild partial QA progress from iterN caches when final 05 files are stale."""
    run_dir = TMP_DIR / run_id
    iteration_numbers = set()

    for path in run_dir.glob("iter*_05_qa_passed.json"):
        match = re.match(r"iter(\d+)_05_qa_passed\.json$", path.name)
        if match:
            iteration_numbers.add(int(match.group(1)))

    for path in run_dir.glob("iter*_05_qa_failed.json"):
        match = re.match(r"iter(\d+)_05_qa_failed\.json$", path.name)
        if match:
            iteration_numbers.add(int(match.group(1)))

    if not iteration_numbers:
        return [], [], 0

    passed_by_email: dict[str, dict] = {}
    failed_by_email: dict[str, dict] = {}

    for iteration in sorted(iteration_numbers):
        iter_passed = _load_intermediate(run_id, f"iter{iteration}_05_qa_passed") or []
        iter_failed = _load_intermediate(run_id, f"iter{iteration}_05_qa_failed") or []

        for lead in iter_passed:
            email = lead.get("email")
            if not email:
                continue
            passed_by_email[email] = lead
            failed_by_email.pop(email, None)

        for lead in iter_failed:
            email = lead.get("email")
            if not email or email in passed_by_email:
                continue
            failed_by_email[email] = lead

    max_iteration = max(iteration_numbers)
    return list(passed_by_email.values()), list(failed_by_email.values()), max_iteration


def _print_summary_table(stats: dict):
    """Print a rich summary table at the end of the run."""
    table = Table(title="Campaign Run Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    for key, value in stats.items():
        table.add_row(key, str(value))

    console.print(table)


def _resolve_cost_cap(strategy: dict, deploy_target: int, cli_max_cost: float | None) -> float:
    if cli_max_cost is not None:
        return cli_max_cost

    env_cap = float(os.getenv("MAX_COST_EUR", "50"))
    launch_settings = get_launch_settings(strategy)
    recommended = float(launch_settings.get("recommended_min_cost_eur", 90))
    if deploy_target >= 1000:
        return max(env_cap, recommended)
    return env_cap


def _resolve_collection_target(strategy: dict, deploy_target: int, dry_run: bool) -> tuple[int, int]:
    if dry_run:
        return deploy_target, 0

    launch_settings = get_launch_settings(strategy)
    ratio = float(launch_settings.get("upload_buffer_ratio", 0.0))
    minimum = int(launch_settings.get("upload_buffer_min", 0))
    buffer_size = 0
    if deploy_target >= 500 and ratio > 0:
        buffer_size = max(minimum, int(deploy_target * ratio))
    return deploy_target + buffer_size, buffer_size


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--query", type=str, help="Search query for Apify Leads Finder")
@click.option("--location", type=str, default=None, help="Geography filter (defaults to campaign brief)")
@click.option("--count", type=int, default=100, help="Max leads to fetch")
@click.option("--campaign-name", type=str, help="Instantly campaign name")
@click.option("--campaign-brief", "campaign_brief_path", type=str, help="Path to a campaign brief JSON file")
@click.option("--skip-linkedin", is_flag=True, help="Skip LinkedIn enrichment")
@click.option("--dry-run", is_flag=True, help="Full pipeline without Instantly deployment")
@click.option("--max-cost", type=float, default=None, help="Override EUR cost cap")
@click.option("--csv", "csv_path", type=str, help="Import leads from CSV instead of Apify")
@click.option("--reuse-run", type=str, help="Reuse a previous Apify run ID")
@click.option("--skip-verify", is_flag=True, help="Skip email verification (pass all leads through)")
@click.option("--resume", "resume_run_id", type=str, help="Resume a previous run by ID (skips completed steps)")
def main(query, location, count, campaign_name, campaign_brief_path, skip_linkedin, skip_verify, dry_run, max_cost, csv_path, reuse_run, resume_run_id):
    """Outreach Engine v2 — automated cold email campaign pipeline."""

    # --- Determine run_id: new or resumed ---
    runtime_config = {}
    if resume_run_id:
        run_id = resume_run_id
        run_dir = TMP_DIR / run_id
        if not run_dir.exists():
            console.print(f"[red]No cached data found for run {run_id} in .tmp/[/red]")
            sys.exit(1)

        runtime_config = _load_runtime_config(run_id)
        cached_schema = runtime_config.get("schema_version")
        if cached_schema != RUN_SCHEMA_VERSION:
            console.print(
                f"[red]Run {run_id} was created with schema {cached_schema or 'unknown'}, "
                f"but this code expects {RUN_SCHEMA_VERSION}. Refusing resume.[/red]"
            )
            sys.exit(1)

        resume_brief_path = runtime_config.get("campaign_brief_path")
        if campaign_brief_path and resume_brief_path and campaign_brief_path != resume_brief_path:
            console.print("[red]Resume run uses a different campaign brief than the cached run. Refusing resume.[/red]")
            sys.exit(1)

        campaign_brief_path = resume_brief_path or campaign_brief_path
        campaign_brief, campaign_strategy = load_campaign_strategy(campaign_brief_path)
        costs, pretest_info = _load_run_state(run_id)
        cached_count = runtime_config.get("requested_count")
        if cached_count and count == 100:
            count = int(cached_count)
        console.print(f"\n[bold]Outreach Engine v2 — RESUMING[/bold]")
        console.print(f"Run ID: {run_id}")
        cached_files = sorted(f.name for f in run_dir.glob("*.json") if f.stem not in ("costs", "pretest_info"))
        console.print(f"Cached steps: {', '.join(cached_files)}")
    else:
        if not query and not csv_path:
            console.print("[red]Error: must provide either --query or --csv[/red]")
            sys.exit(1)
        run_id = str(uuid.uuid4())[:8]
        costs = {"apify_cost": 0.0, "reoon_cost": 0.0, "linkedin_cost": 0.0, "perplexity_cost": 0.0, "claude_cost": 0.0}
        pretest_info = {}
        campaign_brief, campaign_strategy = load_campaign_strategy(campaign_brief_path)

    if not campaign_name:
        campaign_name = runtime_config.get("campaign_name") or campaign_brief.get("campaign_name") or f"Campaign {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    campaign_date_suffix = runtime_config.get("campaign_date_suffix") or datetime.now().strftime('%Y-%m-%d')
    _update_runtime_config(
        run_id,
        {
            "schema_version": RUN_SCHEMA_VERSION,
            "campaign_brief_path": campaign_brief.get("_path", str(DEFAULT_CAMPAIGN_BRIEF_PATH)),
            "campaign_name": campaign_name,
            "channel": campaign_brief.get("channel", "email"),
            "requested_count": count,
            "campaign_date_suffix": campaign_date_suffix,
        },
    )

    if not location:
        location = (campaign_brief.get("icp", {}).get("locations") or ["United States"])[0]

    cost_cap = _resolve_cost_cap(campaign_strategy, count, max_cost)
    collection_target, collection_buffer = _resolve_collection_target(campaign_strategy, count, dry_run)

    if not resume_run_id:
        console.print(f"\n[bold]Outreach Engine v2[/bold]")
        console.print(f"Run ID: {run_id}")
    console.print(f"Campaign: {campaign_name}")
    console.print(f"Cost cap: EUR {cost_cap}")
    if collection_buffer:
        console.print(f"Upload buffer: +{collection_buffer} QA-passed leads to protect the actual in-campaign target")
    if dry_run:
        console.print("[yellow]DRY RUN — no Instantly deployment[/yellow]")
    console.print()

    # -----------------------------------------------------------------------
    # Target loop: run steps 1-5 in iterations until we reach `count`
    # QA-passed leads deployed to Instantly. Max 10 iterations.
    # -----------------------------------------------------------------------
    MAX_ITERATIONS = 10
    deploy_target = count

    # Query rotation: ICP-aligned variations for full-service performance marketing agencies.
    # The first entry is always the user's --query. When a query is exhausted,
    # we pop the next one, run a fresh pretest, and keep sourcing.
    QUERY_ROTATION = campaign_brief.get("icp", {}).get("query_rotation", [])

    all_passed = []
    all_failed = []
    all_processed_emails = set()
    winning_params_for_topup = None
    total_sourced = 0
    query_rotation_idx = 0
    current_query_exhausted = False
    start_iteration = 1

    # Check for cached final results first (fully completed run)
    cached_passed = _load_intermediate(run_id, "05_qa_passed")
    cached_failed = _load_intermediate(run_id, "05_qa_failed")
    if cached_passed is not None and cached_failed is not None:
        all_passed = cached_passed
        all_failed = cached_failed
        all_processed_emails = {l["email"] for l in all_passed + all_failed}
        if not pretest_info:
            _, pretest_info = _load_run_state(run_id)
        winning_params_for_topup = pretest_info.get("winning_params")
        total_sourced = len(all_processed_emails)
        if len(all_passed) >= collection_target:
            console.print(f"  Steps 1-5: [dim]loaded {len(all_passed)} QA-passed, {len(all_failed)} QA-failed from cache[/dim]")
        else:
            iter_passed, iter_failed, max_cached_iteration = _collect_iteration_qa_results(run_id)
            if iter_passed or iter_failed:
                all_passed = iter_passed
                all_failed = iter_failed
                all_processed_emails = {l["email"] for l in all_passed + all_failed}
                total_sourced = len(all_processed_emails)
            start_iteration = (max_cached_iteration + 1) if max_cached_iteration else 1
            if max_cached_iteration > 1:
                last_verified = _load_intermediate(run_id, f"iter{max_cached_iteration}_02_verified") or []
                if winning_params_for_topup and len(last_verified) < LOW_YIELD_MIN_VERIFIED:
                    current_query_exhausted = True
            console.print(
                f"  Steps 1-5: [dim]loaded partial cache with {len(all_passed)} QA-passed, "
                f"{len(all_failed)} QA-failed; continuing at iteration {start_iteration}[/dim]"
            )

    if len(all_passed) < collection_target:
        from execution.source_leads import source_leads
        from execution.verify_emails import verify_emails
        from execution.enrich_leads import enrich_leads
        from execution.personalize import personalize_leads, regenerate_failed_leads
        from execution.qa_check import qa_check_all_with_coherence

        for iteration in range(start_iteration, MAX_ITERATIONS + 1):
            still_needed = collection_target - len(all_passed)
            if still_needed <= 0:
                break

            is_topup = iteration > 1
            # CSV mode cannot source more leads on iteration 2+ — the whole
            # CSV was consumed on iteration 1. Anything beyond the original
            # file is either a fresh Apify query or a reuse-run reprocess.
            # Without this guard the pipeline crashes with "Must provide
            # either --query or --csv" mid-run after Reoon drops invalid
            # emails and the first iteration lands short of the target.
            if is_topup and csv_path:
                console.print(
                    f"\n  [yellow]Iteration {iteration}: CSV exhausted with {len(all_passed)}/{collection_target} leads. "
                    f"CSV mode can't source more — proceeding with what we have.[/yellow]"
                )
                break

            if is_topup:
                observed_rate = len(all_passed) / total_sourced if total_sourced else 0.5
                source_count = int(still_needed / max(observed_rate, 0.1)) + 50
                console.print(
                    f"\n  [bold]Iteration {iteration}[/bold]: {len(all_passed)}/{collection_target} leads ready, "
                    f"sourcing ~{source_count} more"
                )
            else:
                source_count = count

            # --- Step 1: Source ---
            iter_cache = f"iter{iteration}_01_sourced"
            iter_leads = _load_intermediate(run_id, iter_cache)
            if iter_leads is not None:
                console.print(f"  Step 1 (iter {iteration}): [dim]loaded {len(iter_leads)} leads from cache[/dim]")
                if not pretest_info:
                    _, pretest_info = _load_run_state(run_id)
            else:
                if not is_topup and not query and not csv_path and not reuse_run:
                    console.print("[red]Error: must provide --query or --csv for a fresh run[/red]")
                    sys.exit(1)

                # Decide whether to use winning_params (topup) or a new rotated query
                use_query_profile = None
                use_winning_params = winning_params_for_topup if (is_topup and not current_query_exhausted) else None
                use_query = query if not current_query_exhausted else None

                # If current query is exhausted, rotate to the next one
                if current_query_exhausted and is_topup:
                    if query_rotation_idx < len(QUERY_ROTATION):
                        rotated = QUERY_ROTATION[query_rotation_idx]
                        query_rotation_idx += 1
                        industry = rotated["company_industry"][0]
                        titles = rotated["contact_job_title"]
                        console.print(f"  [cyan]Rotating to new query: {', '.join(titles)} in {industry}[/cyan]")

                        # New query families must pass the 100-lead ICP gate before full fetch.
                        use_query_profile = dict(rotated)
                        use_winning_params = None
                        use_query = None
                        current_query_exhausted = False
                    else:
                        console.print(f"  [yellow]All query rotations exhausted — deploying with {len(all_passed)} leads[/yellow]")
                        break

                with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
                    task_label = "Step 1: Sourcing leads + ICP pretest..." if not is_topup else f"Step 1 (iter {iteration}): Sourcing more leads..."
                    task = progress.add_task(task_label, total=None)
                    try:
                        iter_leads, iter_pretest = source_leads(
                            query=use_query,
                            location=location,
                            count=source_count,
                            csv_path=csv_path if not is_topup else None,
                            reuse_run_id=reuse_run if not is_topup else None,
                            exclude_emails=all_processed_emails if all_processed_emails else None,
                            winning_params=use_winning_params,
                            query_profile=use_query_profile,
                            icp=campaign_brief.get("icp"),
                        )
                    except RuntimeError as e:
                        progress.update(task, completed=True)
                        console.print(f"[red]Source failed (iter {iteration}): {e}[/red]")
                        if all_passed:
                            console.print(f"[yellow]Proceeding with {len(all_passed)} leads already collected[/yellow]")
                            break
                        _notify_abort(run_id, campaign_name, str(e))
                        sys.exit(1)
                    progress.update(task, completed=True)

                if not use_winning_params:
                    winning_rotation_idx = iter_pretest.get("winning_query_rotation_index")
                    if winning_rotation_idx is not None:
                        query_rotation_idx = max(query_rotation_idx, winning_rotation_idx + 1)
                    winning_params_for_topup = iter_pretest.get("winning_params")

                if not use_winning_params:
                    pretest_info = iter_pretest
                    if csv_path:
                        costs["apify_cost"] = 0.0
                    else:
                        pretest_attempts = pretest_info.get("attempts", 1)
                        costs["apify_cost"] += (PRETEST_SAMPLE_COUNT * pretest_attempts + len(iter_leads)) * APIFY_COST_PER_LEAD
                    pass_rate = pretest_info.get("pass_rate", 0)
                    console.print(f"  Sourced: {len(iter_leads)} leads")
                    console.print(f"  ICP pretest: {pass_rate * 100:.1f}% pass rate ({pretest_info.get('attempts', 1)} attempt(s))")
                    winning_profile = pretest_info.get("winning_profile")
                    if winning_profile:
                        console.print(f"  Winning query profile: {winning_profile}")
                else:
                    costs["apify_cost"] += len(iter_leads) * APIFY_COST_PER_LEAD
                    console.print(f"  Sourced: {len(iter_leads)} NEW leads (iter {iteration})")

                _save_intermediate(run_id, iter_cache, iter_leads)
                _save_run_state(run_id, costs, pretest_info)

            if not iter_leads:
                console.print(f"  [yellow]No new leads found (iter {iteration}) — query exhausted, rotating...[/yellow]")
                current_query_exhausted = True
                winning_params_for_topup = None
                continue

            total_sourced += len(iter_leads)
            all_processed_emails.update(l["email"] for l in iter_leads)

            # --- Step 2: Verify ---
            iter_verified_cache = f"iter{iteration}_02_verified"
            iter_verified = _load_intermediate(run_id, iter_verified_cache)
            if iter_verified is not None:
                console.print(f"  Step 2 (iter {iteration}): [dim]loaded {len(iter_verified)} verified from cache[/dim]")
            elif skip_verify:
                iter_verified = iter_leads
            else:
                with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
                    task = progress.add_task(f"Step 2 (iter {iteration}): Verifying emails...", total=None)
                    iter_verified = verify_emails(iter_leads)
                    progress.update(task, completed=True)

                reoon_count = len(iter_leads) - sum(1 for l in iter_leads if l.get("email_status") == "validated")
                costs["reoon_cost"] += reoon_count * REOON_COST_PER_LEAD
                console.print(f"  Verified: {len(iter_verified)} leads (iter {iteration})")
                _save_intermediate(run_id, iter_verified_cache, iter_verified)
                _save_run_state(run_id, costs)

            if is_topup and use_winning_params:
                low_yield_threshold = max(LOW_YIELD_MIN_VERIFIED, int(source_count * LOW_YIELD_VERIFIED_RATIO))
                if len(iter_verified) < low_yield_threshold:
                    console.print(
                        f"  [yellow]Verified yield too thin ({len(iter_verified)} < {low_yield_threshold}) — "
                        f"rotating query family next iteration[/yellow]"
                    )
                    current_query_exhausted = True
                    winning_params_for_topup = None

            _check_cost_cap(costs, cost_cap)

            # --- Step 3: Enrich ---
            iter_enriched_cache = f"iter{iteration}_03_enriched"
            iter_enriched = _load_intermediate(run_id, iter_enriched_cache)
            if iter_enriched is not None:
                console.print(f"  Step 3 (iter {iteration}): [dim]loaded {len(iter_enriched)} enriched from cache[/dim]")
            else:
                with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
                    task = progress.add_task(f"Step 3 (iter {iteration}): Enriching...", total=None)
                    iter_enriched, enrich_costs = enrich_leads(iter_verified, skip_linkedin=skip_linkedin)
                    progress.update(task, completed=True)

                costs["linkedin_cost"] += enrich_costs.get("linkedin_cost", 0.0)
                costs["perplexity_cost"] += enrich_costs.get("perplexity_cost", 0.0)
                costs.setdefault("website_cost", 0.0)
                costs["website_cost"] += enrich_costs.get("website_cost", 0.0)
                costs.setdefault("linkedin_posts_cost", 0.0)
                costs["linkedin_posts_cost"] += enrich_costs.get("linkedin_posts_cost", 0.0)
                console.print(f"  Enriched: {len(iter_enriched)} leads (iter {iteration})")
                _save_intermediate(run_id, iter_enriched_cache, iter_enriched)
                _save_run_state(run_id, costs)

            _check_cost_cap(costs, cost_cap)

            # --- Step 4: Personalize ---
            iter_pers_cache = f"iter{iteration}_04_personalized"
            iter_personalized = _load_intermediate(run_id, iter_pers_cache)
            if iter_personalized is not None:
                console.print(f"  Step 4 (iter {iteration}): [dim]loaded {len(iter_personalized)} personalized from cache[/dim]")
            else:
                with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
                    task = progress.add_task(f"Step 4 (iter {iteration}): Personalizing...", total=None)
                    iter_personalized, claude_cost = personalize_leads(iter_enriched, campaign_strategy=campaign_strategy)
                    progress.update(task, completed=True)

                costs["claude_cost"] += claude_cost
                console.print(f"  Personalized: {len(iter_personalized)} leads (iter {iteration})")
                _save_intermediate(run_id, iter_pers_cache, iter_personalized)
                _save_run_state(run_id, costs)

            _check_cost_cap(costs, cost_cap)

            # --- Step 5: QA ---
            iter_passed_cache = f"iter{iteration}_05_qa_passed"
            iter_failed_cache = f"iter{iteration}_05_qa_failed"
            iter_passed = _load_intermediate(run_id, iter_passed_cache)
            iter_failed = _load_intermediate(run_id, iter_failed_cache)
            if iter_passed is not None and iter_failed is not None:
                console.print(f"  Step 5 (iter {iteration}): [dim]loaded {len(iter_passed)} passed, {len(iter_failed)} failed from cache[/dim]")
            else:
                with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
                    task = progress.add_task(f"Step 5 (iter {iteration}): QA checks + flow coherence...", total=None)
                    iter_passed, iter_failed, coherence_cost = qa_check_all_with_coherence(
                        iter_personalized,
                        campaign_strategy=campaign_strategy,
                    )
                    progress.update(task, completed=True)

                costs["claude_cost"] += coherence_cost
                pass_rate_qa = len(iter_passed) / len(iter_personalized) * 100 if iter_personalized else 0
                console.print(f"  QA: {len(iter_passed)} passed, {len(iter_failed)} failed ({pass_rate_qa:.1f}% pass rate) (iter {iteration})")

                _check_cost_cap(costs, cost_cap)

                # --- Step 5b: Regeneration loop for QA failures ---
                if iter_failed:
                    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
                        task = progress.add_task(f"Step 5b (iter {iteration}): Regenerating {len(iter_failed)} failed leads...", total=None)
                        recovered, still_failed, regen_cost = regenerate_failed_leads(
                            iter_failed,
                            campaign_strategy=campaign_strategy,
                            max_attempts=3,
                        )
                        progress.update(task, completed=True)

                    costs["claude_cost"] += regen_cost
                    if recovered:
                        console.print(f"  Regen: recovered {len(recovered)}/{len(iter_failed)} leads (${regen_cost:.2f})")
                        iter_passed.extend(recovered)
                        iter_failed = still_failed
                    else:
                        console.print(f"  Regen: no leads recovered ({len(iter_failed)} still failed)")

                _check_cost_cap(costs, cost_cap)

                _save_intermediate(run_id, iter_passed_cache, iter_passed)
                _save_intermediate(run_id, iter_failed_cache, iter_failed)
                _save_run_state(run_id, costs)

            all_passed.extend(iter_passed)
            all_failed.extend(iter_failed)

            console.print(f"  [bold]Progress: {len(all_passed)}/{collection_target} leads ready[/bold]")

            if len(all_passed) >= collection_target:
                break

        # Save consolidated results
        _save_intermediate(run_id, "05_qa_passed", all_passed)
        _save_intermediate(run_id, "05_qa_failed", all_failed)
        _save_run_state(run_id, costs)

    passed_leads = all_passed
    failed_leads = all_failed

    if not passed_leads:
        console.print("[red]No leads passed QA — aborting[/red]")
        _notify_abort(run_id, campaign_name, "Zero leads passed QA")
        sys.exit(1)

    leads = []  # For summary (not used directly anymore)
    verified_leads = []
    personalized_leads = passed_leads + failed_leads

    from execution.preflight_launch import build_launch_preflight_report
    launch_preflight = build_launch_preflight_report(
        campaign_brief=campaign_brief,
        campaign_strategy=campaign_strategy,
        deploy_target=deploy_target,
        cost_cap=cost_cap,
        passed_leads=passed_leads,
        pretest_info=pretest_info,
    )
    _save_intermediate(run_id, "00_launch_preflight", launch_preflight)
    if launch_preflight.get("blockers"):
        console.print("[yellow]Launch preflight blockers detected:[/yellow]")
        for blocker in launch_preflight["blockers"]:
            console.print(f"  - {blocker}")
    if launch_preflight.get("warnings"):
        console.print("[yellow]Launch preflight warnings:[/yellow]")
        for warning in launch_preflight["warnings"][:5]:
            console.print(f"  - {warning}")
    if launch_preflight.get("blockers") and not dry_run:
        console.print("[red]Live deployment blocked by launch preflight. Fix the blockers above or rerun with --dry-run.[/red]")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Step 6: Deploy
    # -----------------------------------------------------------------------
    deploy_cache = _load_intermediate(run_id, "06_deploy")
    if deploy_cache is not None and deploy_cache.get("upload_stats") is not None:
        campaign_id = deploy_cache.get("campaign_id", "")
        upload_stats = deploy_cache.get("upload_stats", {})
        console.print(f"  Step 6: [dim]loaded deploy result from cache (campaign {campaign_id})[/dim]")
    elif dry_run:
        console.print("  [yellow]DRY RUN — skipping Instantly deployment[/yellow]")
        campaign_id = "dry-run"
        upload_stats = {"total": len(passed_leads), "uploaded": 0, "failed": 0, "success_rate": 0}
        _save_intermediate(run_id, "06_deploy", {"campaign_id": campaign_id, "upload_stats": upload_stats})
    else:
        campaign_id = (deploy_cache or {}).get("campaign_id", "")
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
            task = progress.add_task("Step 6: Deploying to Instantly...", total=None)
            from execution.deploy_instantly import (
                _verify_lead_count,
                create_campaign,
                load_sending_accounts,
                upload_leads,
            )

            if not campaign_id:
                sending_accounts = load_sending_accounts()
                campaign_id = create_campaign(
                    campaign_name,
                    sending_accounts=sending_accounts,
                    campaign_strategy=campaign_strategy,
                    date_suffix=campaign_date_suffix,
                )
                # Save immediately so a crash during upload resumes into the same draft.
                _save_intermediate(run_id, "06_deploy", {"campaign_id": campaign_id, "upload_stats": None})

            upload_stats = upload_leads(campaign_id, passed_leads, campaign_strategy=campaign_strategy)
            actual_count = _verify_lead_count(campaign_id)
            if actual_count >= 0:
                skipped = upload_stats["uploaded"] - actual_count
                upload_stats["actually_in_campaign"] = actual_count
                if skipped > 0:
                    log.warning(
                        "instantly.leads_skipped",
                        api_accepted=upload_stats["uploaded"],
                        actually_in_campaign=actual_count,
                        skipped=skipped,
                        reason="Leads likely already exist in other campaigns in this workspace",
                    )
                    upload_stats["skipped_workspace_duplicate"] = skipped
            progress.update(task, completed=True)

        console.print(f"  Deployed: campaign {campaign_id} ({upload_stats['uploaded']} leads uploaded)")
        _save_intermediate(run_id, "06_deploy", {"campaign_id": campaign_id, "upload_stats": upload_stats})

    actual_in_campaign = upload_stats.get("actually_in_campaign", upload_stats.get("uploaded", 0))
    under_target = not dry_run and actual_in_campaign < deploy_target
    if under_target:
        shortfall = deploy_target - actual_in_campaign
        console.print(
            f"[red]Campaign under target after workspace dedupe: {actual_in_campaign}/{deploy_target} in campaign "
            f"(short by {shortfall})[/red]"
        )

    # -----------------------------------------------------------------------
    # Notify
    # -----------------------------------------------------------------------
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("Logging + notifications...", total=None)

        from execution.notify import notify_completion
        notify_completion(
            run_id=run_id,
            campaign_name=campaign_name,
            campaign_id=campaign_id,
            passed_leads=passed_leads,
            failed_leads=failed_leads,
            costs=costs,
            upload_stats=upload_stats,
            campaign_strategy=campaign_strategy,
            launch_preflight=launch_preflight,
        )
        progress.update(task, completed=True)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    total_cost = sum(costs.values())
    _print_summary_table({
        "Run ID": run_id,
        "Campaign": campaign_name,
        "Campaign ID": campaign_id,
        "Deploy target": deploy_target,
        "Collection target": collection_target,
        "Total sourced": total_sourced or len(all_processed_emails),
        "QA passed": len(passed_leads),
        "QA failed": len(failed_leads),
        "Leads uploaded": upload_stats.get("uploaded", 0),
        "Actually in campaign": actual_in_campaign,
        "Total cost (EUR)": f"{total_cost:.2f}",
        "Status": "UNDER TARGET" if under_target else ("DRAFT" if not dry_run else "DRY RUN"),
    })

    console.print(f"\n[dim]Intermediates saved to .tmp/{run_id}/[/dim]")
    console.print(f"[dim]To resume if interrupted: python run_campaign.py --resume {run_id} --campaign-name \"{campaign_name}\"[/dim]")
    if under_target:
        sys.exit(1)


def _check_cost_cap(costs: dict, cap: float):
    """Abort if accumulated costs exceed the cap."""
    total = sum(costs.values())
    if total > cap:
        console.print(f"[red]Cost cap exceeded: EUR {total:.2f} > EUR {cap:.2f}. Aborting.[/red]")
        sys.exit(1)


def _notify_abort(run_id: str, campaign_name: str, reason: str):
    """Send Telegram notification on abort."""
    try:
        from execution.notify import send_telegram_notification
        send_telegram_notification(
            run_id=run_id,
            campaign_name=campaign_name,
            campaign_id="ABORTED",
            leads_uploaded=0,
            leads_excluded_qa=0,
            total_cost=0,
            abort_reason=reason,
        )
    except Exception as exc:
        log.error("notify.abort_failed", run_id=run_id, error=str(exc))


if __name__ == "__main__":
    main()
