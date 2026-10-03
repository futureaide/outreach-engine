"""
Launch preflight checks for large cold-email campaigns.

This module does not replace human launch review. It turns the most important
Nick-style readiness checks into a structured report so the run surfaces
operational blockers before deployment.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from execution.campaign_strategy import (
    get_launch_settings,
    render_sequence_open_bundle,
)
from execution.deploy_instantly import DEFAULT_ACCOUNTS_CSV, load_sending_accounts
from execution.personalize import extract_primitives


GENERIC_SENDER_LOCAL_PARTS = {
    "admin",
    "billing",
    "contact",
    "hello",
    "help",
    "info",
    "marketing",
    "sales",
    "support",
    "team",
}


def _looks_like_real_url(value: str) -> bool:
    if not value or value.startswith("[") or "pending" in value.lower():
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def inspect_sending_accounts(csv_path: str | None = None) -> dict:
    accounts = load_sending_accounts(csv_path)
    generic = []
    domains = set()

    for email in accounts:
        if "@" not in email:
            continue
        local_part, domain = email.split("@", 1)
        domains.add(domain.lower())
        if local_part.lower() in GENERIC_SENDER_LOCAL_PARTS:
            generic.append(email)

    return {
        "count": len(accounts),
        "accounts": accounts,
        "unique_domains": sorted(domains),
        "generic_sender_emails": sorted(generic),
        "display_name_check_required": True,
        "csv_path": str(Path(csv_path) if csv_path else DEFAULT_ACCOUNTS_CSV),
    }


def inspect_env_readiness() -> dict:
    booking_link = os.environ.get("BOOKING_LINK", "")
    return {
        "apify_key_present": bool(os.environ.get("APIFY_API_TOKEN") or os.environ.get("APIFY_API_KEY")),
        "reoon_key_present": bool(os.environ.get("REOON_API_KEY")),
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "instantly_key_present": bool(os.environ.get("INSTANTLY_API_KEY")),
        "perplexity_key_present": bool(os.environ.get("PERPLEXITY_API_KEY")),
        "telegram_ready": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "booking_link_ready": _looks_like_real_url(booking_link),
        "booking_link_value": booking_link or "",
        "manual_credit_check_required": True,
    }


def _sample_preview_rows(passed_leads: list[dict], campaign_strategy: dict) -> list[dict]:
    sequence_steps = campaign_strategy.get("sequence_steps", [])
    if not sequence_steps:
        return []

    step = sequence_steps[0]
    previews: list[dict] = []
    sample = passed_leads[: min(5, len(passed_leads))]
    for lead in sample:
        variables = lead.get("custom_variables", {})
        bundles = []
        for variant_index, _subject in enumerate(step.get("subjects", [])[:2]):
            bundles.append(render_sequence_open_bundle(step, lead, variables, variant_index=variant_index))
        previews.append(
            {
                "email": lead.get("email", ""),
                "first_name": lead.get("first_name", ""),
                "company_name": lead.get("company_name", ""),
                "open_package": bundles,
            }
        )
    return previews


def _lane_fit_summary(passed_leads: list[dict]) -> dict:
    if not passed_leads:
        return {"agency_share": 0.0, "saas_share": 0.0, "generic_share": 0.0}

    counts = {
        "performance_marketing_agency": 0,
        "b2b_saas": 0,
        "generic": 0,
    }
    for lead in passed_leads:
        lane = extract_primitives(lead).get("lane", "generic")
        counts[lane] = counts.get(lane, 0) + 1

    total = len(passed_leads)
    return {
        "agency_share": round(counts.get("performance_marketing_agency", 0) / total, 3),
        "saas_share": round(counts.get("b2b_saas", 0) / total, 3),
        "generic_share": round(counts.get("generic", 0) / total, 3),
    }


def build_launch_preflight_report(
    *,
    campaign_brief: dict,
    campaign_strategy: dict,
    deploy_target: int,
    cost_cap: float,
    passed_leads: list[dict] | None = None,
    pretest_info: dict | None = None,
    sending_accounts_csv: str | None = None,
) -> dict:
    passed_leads = passed_leads or []
    pretest_info = pretest_info or {}
    launch_settings = get_launch_settings(campaign_strategy)
    env_ready = inspect_env_readiness()
    account_ready = inspect_sending_accounts(sending_accounts_csv)
    lane_summary = _lane_fit_summary(passed_leads) if passed_leads else {}

    blockers: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    recommended_cost = float(launch_settings.get("recommended_min_cost_eur", 90))
    if cost_cap < recommended_cost and deploy_target >= 1000:
        blockers.append(
            f"cost cap is {cost_cap:.0f} EUR but the launch recommendation is at least {recommended_cost:.0f} EUR"
        )

    if not env_ready["booking_link_ready"]:
        blockers.append("BOOKING_LINK is missing or looks like a placeholder")

    if account_ready["count"] < 1:
        blockers.append("no sending accounts found in accounts.csv")

    if account_ready["generic_sender_emails"]:
        warnings.append(
            "generic sender emails found: " + ", ".join(account_ready["generic_sender_emails"][:5])
        )

    if campaign_brief.get("channel") == "email":
        notes.append("manual sender display-name check still required inside Instantly")

    # Surface suppression-list health. A silent load failure (Google Sheets
    # auth expired, network error) is indistinguishable from an empty list to
    # the caller of apply_suppression, so every previously-suppressed lead
    # would be emailed again. Treat load errors as blockers, and a token-
    # missing status as a warning the operator must acknowledge.
    from execution.source_leads import get_suppression_readiness
    suppression = get_suppression_readiness()
    if suppression.get("reason") == "load_error":
        blockers.append(
            f"suppression list failed to load ({suppression.get('error')}) — "
            f"previously suppressed leads would be emailed again"
        )
    elif suppression.get("reason") == "no_token":
        warnings.append(
            "Google OAuth token missing; suppression list not loaded "
            "(set GOOGLE_TOKEN_PATH or run the OAuth flow)"
        )
    elif suppression.get("reason") == "ok" and suppression.get("count", 0) == 0:
        notes.append("suppression list is empty — fine for a first campaign but verify it's intentional")

    if len(campaign_strategy.get("sequence_steps", [])) > 2:
        warnings.append("sequence is 3 emails; Nick would normally start with 2 and earn the 3rd")

    max_variants = int(launch_settings.get("max_active_variants", 2))
    min_variant_volume = int(launch_settings.get("min_leads_per_variant", 500))
    notes.append(
        f"launch cap for meaningful variant testing: {max_variants} active variants with at least {min_variant_volume} leads per variant"
    )

    if passed_leads and lane_summary.get("agency_share", 0) < 0.7 and campaign_brief.get("brief_key") == "fa_agencies":
        warnings.append(
            f"agency lane share is only {lane_summary['agency_share']:.0%}; sample may be drifting away from full-service performance agencies"
        )

    return {
        "ready": not blockers,
        "brief_key": campaign_brief.get("brief_key", ""),
        "campaign_name": campaign_brief.get("campaign_name", ""),
        "deploy_target": deploy_target,
        "cost_cap_eur": round(cost_cap, 2),
        "recommended_min_cost_eur": recommended_cost,
        "launch_settings": launch_settings,
        "pretest_info": pretest_info,
        "env_readiness": env_ready,
        "sending_accounts": account_ready,
        "lane_fit": lane_summary,
        "preview_samples": _sample_preview_rows(passed_leads, campaign_strategy),
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
    }
