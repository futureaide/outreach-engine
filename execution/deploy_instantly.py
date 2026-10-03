"""
Step 7: Deploy to Instantly
Creates a campaign with the active brief's fixed email sequence, then uploads leads.
Campaign is always created in DRAFT status (status=0).
"""

import csv
import os
import time
from datetime import datetime
from pathlib import Path

import requests
import structlog
from dotenv import load_dotenv

from execution.campaign_strategy import body_lines_to_html, get_custom_variable_keys, get_launch_settings

load_dotenv()
log = structlog.get_logger()

INSTANTLY_BASE_URL = "https://api.instantly.ai/api/v2"
BATCH_SIZE = 100
BATCH_DELAY = 3  # seconds between lead upload batches
DEFAULT_ACCOUNTS_CSV = Path(__file__).parent.parent / "accounts.csv"

# Instantly API accepts a restricted subset of IANA timezones.
# Common names like America/New_York are rejected.
TIMEZONE_FALLBACKS = {
    "America/New_York": "America/Detroit",
    "America/Los_Angeles": "America/Creston",
    "America/Denver": "America/Boise",
    "US/Eastern": "America/Detroit",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Boise",
    "US/Pacific": "America/Creston",
}
DEFAULT_INSTANTLY_TIMEZONE = "America/Detroit"

def _get_headers() -> dict:
    api_key = os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        raise RuntimeError("INSTANTLY_API_KEY not set in .env")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _humanize(text: str) -> str:
    """Post-process text to sound human: lowercase start, no hyphens, minimal punctuation."""
    if not text:
        return text
    # Lowercase the first character
    text = text[0].lower() + text[1:] if text else text
    # Remove all hyphens/dashes between words (em dash, en dash, regular hyphen)
    text = text.replace("—", " ").replace("–", " ").replace("-", " ")
    # Collapse double spaces from replacements
    while "  " in text:
        text = text.replace("  ", " ")
    # Remove semicolons and colons
    text = text.replace(";", ",").replace(":", "")
    return text.strip()


def standardize_variables(variables: dict, campaign_strategy: dict | None = None) -> dict:
    """Ensure all leads have the same set of keys with flat string values."""
    standard_keys = get_custom_variable_keys(campaign_strategy) or [
        "signal_topic",
        "personalization_line",
        "identity_line",
        "who_i_help",
        "problem_i_fix",
        "bottleneck_trigger",
        "offer_observation",
        "offer_scope",
        "offer_payoff",
        "micro_insight",
        "followup_ping",
        "proof_problem",
        "proof_fix",
        "proof_payoff",
    ]
    result = {}
    for key in standard_keys:
        val = variables.get(key, "")
        if val is None:
            val = ""
        if not isinstance(val, str):
            val = str(val)
        result[key] = val

    for key in standard_keys:
        if result.get(key):
            result[key] = _humanize(result[key])

    return result


def load_sending_accounts(csv_path: str | None = None) -> list[str]:
    """
    Load sending account emails from a CSV file.
    Falls back to DEFAULT_ACCOUNTS_CSV if no path is provided.
    Returns a list of email addresses.
    """
    path = Path(csv_path) if csv_path else DEFAULT_ACCOUNTS_CSV
    if not path.exists():
        log.warning("instantly.no_accounts_csv", path=str(path))
        return []

    emails = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("Email", "").strip()
            if email:
                emails.append(email)

    log.info("instantly.loaded_sending_accounts", count=len(emails))
    return emails


def _build_sequence(campaign_strategy: dict | None = None) -> list[dict]:
    """Build the Instantly sequence from the compiled campaign strategy."""
    strategy = campaign_strategy or {}
    sequence_steps = strategy.get("sequence_steps", [])
    if not sequence_steps:
        raise ValueError("campaign_strategy missing sequence_steps")

    steps = []
    for step in sequence_steps:
        body = body_lines_to_html(step.get("body_lines", []))
        variants = [
            {
                "subject": subject,
                "body": body,
            }
            for subject in step.get("subjects", [])
        ]
        # Each step's `delay` is "days to wait before firing THIS step after the
        # previous step." So step 1 gets 0 (fire on activation), step 2 gets
        # email_2.delay_days, etc. The older off-by-one wiring (set to the NEXT
        # step's delay) is what produced the live draft's weird Step1=3d/Step3=0d.
        steps.append(
            {
                "type": "email",
                "delay": int(step.get("delay_days", 0)),
                "variants": variants,
                "delay_unit": "days",
                "pre_delay_unit": "days",
            }
        )

    return [{"steps": steps}]


def _verify_campaign_draft(campaign_id: str) -> int | None:
    try:
        headers = _get_headers()
        resp = requests.get(
            f"{INSTANTLY_BASE_URL}/campaigns/{campaign_id}",
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            log.warning("instantly.campaign_status_fetch_failed", campaign_id=campaign_id, status=resp.status_code)
            return None
        data = resp.json()
        return data.get("status")
    except Exception as e:
        log.warning("instantly.campaign_status_error", campaign_id=campaign_id, error=str(e))
        return None


def get_campaign(campaign_id: str) -> dict:
    headers = _get_headers()
    resp = requests.get(
        f"{INSTANTLY_BASE_URL}/campaigns/{campaign_id}",
        headers=headers,
        timeout=30,
    )
    if not resp.ok:
        log.error("instantly.get_campaign_failed", campaign_id=campaign_id, status=resp.status_code, body=resp.text[:300])
        resp.raise_for_status()
    return resp.json()


def update_campaign(campaign_id: str, campaign_strategy: dict | None = None) -> dict:
    existing = get_campaign(campaign_id)
    headers = _get_headers()
    payload = {
        "name": existing.get("name"),
        "campaign_schedule": existing.get("campaign_schedule"),
        "sequences": _build_sequence(campaign_strategy=campaign_strategy),
        "daily_limit": existing.get("daily_limit", get_launch_settings(campaign_strategy).get("daily_limit", 50)),
        "stop_on_reply": existing.get("stop_on_reply", True),
        "link_tracking": existing.get("link_tracking", False),
        "open_tracking": existing.get("open_tracking", False),
        "email_list": existing.get("email_list", []),
    }

    resp = requests.patch(
        f"{INSTANTLY_BASE_URL}/campaigns/{campaign_id}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        log.error("instantly.update_campaign_failed", campaign_id=campaign_id, status=resp.status_code, body=resp.text[:500])
        resp.raise_for_status()
    return resp.json()


def create_campaign(
    campaign_name: str,
    sending_accounts: list[str] | None = None,
    campaign_strategy: dict | None = None,
    date_suffix: str | None = None,
) -> str:
    """
    Create an Instantly campaign with the embedded sequence from the active brief.
    sending_accounts: list of email addresses to attach as senders.
    Returns the campaign_id.
    """
    headers = _get_headers()
    raw_tz = os.getenv("DEFAULT_TIMEZONE", "America/New_York")
    timezone = TIMEZONE_FALLBACKS.get(raw_tz, raw_tz)
    brief_key = (campaign_strategy or {}).get("brief_key", "")
    campaign_label = f"[{brief_key}] {campaign_name}" if brief_key else campaign_name
    launch_settings = get_launch_settings(campaign_strategy)
    daily_limit = int(launch_settings.get("daily_limit", 50))
    campaign_date = date_suffix or datetime.now().strftime('%Y-%m-%d')

    payload = {
        "name": f"{campaign_label} - {campaign_date}",
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "Business Hours",
                    "timing": {"from": "09:00", "to": "17:00"},
                    "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
                    "timezone": timezone,
                }
            ]
        },
        "sequences": _build_sequence(campaign_strategy=campaign_strategy),
        "daily_limit": daily_limit,
        "stop_on_reply": True,
        "link_tracking": False,
        "open_tracking": False,
    }

    if sending_accounts:
        payload["email_list"] = sending_accounts
        log.info("instantly.attaching_sending_accounts", count=len(sending_accounts))

    log.info("instantly.creating_campaign", name=payload["name"])

    resp = requests.post(
        f"{INSTANTLY_BASE_URL}/campaigns",
        headers=headers,
        json=payload,
        timeout=30,
    )

    if resp.status_code in (401, 402, 403):
        raise RuntimeError(f"Instantly auth error ({resp.status_code}): {resp.text}")

    if not resp.ok:
        log.error("instantly.create_failed", status=resp.status_code, body=resp.text)
        resp.raise_for_status()

    data = resp.json()
    campaign_id = data.get("id", "")

    campaign_status = _verify_campaign_draft(campaign_id)
    if campaign_status == 0:
        log.info("instantly.campaign_created", campaign_id=campaign_id, status="DRAFT", verified=True)
    else:
        log.warning(
            "instantly.campaign_created_unverified_status",
            campaign_id=campaign_id,
            expected_status=0,
            actual_status=campaign_status,
        )
    return campaign_id


def _upload_single_lead(headers: dict, campaign_id: str, lead_payload: dict) -> tuple[bool, str]:
    """
    Upload a single lead via POST /api/v2/leads (v2 requires one lead per request).
    Returns (success, email).
    """
    email = lead_payload.get("email", "")
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{INSTANTLY_BASE_URL}/leads",
                headers=headers,
                json=lead_payload,
                timeout=30,
            )

            if resp.status_code in (401, 402, 403):
                raise RuntimeError(f"Instantly auth error ({resp.status_code}): {resp.text}")

            if resp.status_code == 429:
                wait = (2 ** attempt) * 3
                log.warning("instantly.lead_rate_limit", email=email, wait=wait)
                time.sleep(wait)
                continue

            if not resp.ok:
                log.error("instantly.lead_upload_failed", email=email, status=resp.status_code, body=resp.text[:300])
                return False, email

            return True, email

        except RuntimeError:
            raise
        except Exception as e:
            log.error("instantly.lead_upload_error", email=email, error=str(e), attempt=attempt + 1)
            if attempt < 2:
                time.sleep(2)
                continue
            return False, email

    return False, email


def upload_leads(campaign_id: str, leads: list[dict], campaign_strategy: dict | None = None) -> dict:
    """
    Upload leads to an Instantly campaign one at a time (API v2 requirement).
    Uses the `campaign` field (not `campaign_id`) and `custom_variables` for personalization.
    Parallel workers with rate limiting. Returns upload stats.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    headers = _get_headers()
    max_workers = 5

    formatted_leads = []
    for lead in leads:
        variables = standardize_variables(lead.get("custom_variables", {}), campaign_strategy=campaign_strategy)
        payload = {
            "email": lead["email"],
            "first_name": lead.get("first_name", ""),
            "last_name": lead.get("last_name", ""),
            "company_name": lead.get("company_name", ""),
            "campaign": campaign_id,
            "skip_if_in_campaign": True,
            "skip_if_in_workspace": False,
            "custom_variables": variables,
        }
        formatted_leads.append(payload)

    total = len(formatted_leads)
    uploaded = 0
    failed = 0

    log.info("instantly.uploading_leads", total=total, workers=max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_upload_single_lead, headers, campaign_id, lp): lp
            for lp in formatted_leads
        }

        for future in as_completed(futures):
            try:
                success, email = future.result()
                if success:
                    uploaded += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                log.error("instantly.lead_future_error", error=str(e))

            if (uploaded + failed) % 50 == 0:
                log.info("instantly.upload_progress", uploaded=uploaded, failed=failed, remaining=total - uploaded - failed)

    success_rate = uploaded / total * 100 if total else 0
    stats = {
        "total": total,
        "uploaded": uploaded,
        "failed": failed,
        "success_rate": round(success_rate, 1),
    }

    if success_rate < 90:
        log.warning("instantly.low_success_rate", **stats)
    else:
        log.info("instantly.upload_complete", **stats)

    return stats


def _verify_lead_count(campaign_id: str) -> int:
    """Query Instantly to get the actual lead count in the campaign."""
    try:
        headers = _get_headers()
        resp = requests.post(
            f"{INSTANTLY_BASE_URL}/leads/list",
            headers=headers,
            json={"campaign": campaign_id, "limit": 1},
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return 0
            # Paginate to count (Instantly doesn't return total count directly)
            all_ids = set()
            next_cursor = None
            while True:
                payload = {"campaign": campaign_id, "limit": 100}
                if next_cursor:
                    payload["starting_after"] = next_cursor
                r = requests.post(f"{INSTANTLY_BASE_URL}/leads/list", headers=headers, json=payload, timeout=15)
                if not r.ok:
                    break
                page = r.json()
                page_items = page.get("items", [])
                if not page_items:
                    break
                for item in page_items:
                    all_ids.add(item["id"])
                next_cursor = page.get("next_starting_after")
                if not next_cursor or len(page_items) < 100:
                    break
            return len(all_ids)
    except Exception as e:
        log.warning("instantly.verify_count_failed", error=str(e))
    return -1


def list_campaign_leads(campaign_id: str) -> list[dict]:
    headers = _get_headers()
    items: list[dict] = []
    next_cursor = None
    while True:
        payload = {"campaign": campaign_id, "limit": 100}
        if next_cursor:
            payload["starting_after"] = next_cursor
        resp = requests.post(
            f"{INSTANTLY_BASE_URL}/leads/list",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if not resp.ok:
            log.error("instantly.list_leads_failed", campaign_id=campaign_id, status=resp.status_code, body=resp.text[:300])
            resp.raise_for_status()
        data = resp.json()
        page_items = data.get("items", [])
        if not page_items:
            break
        items.extend(page_items)
        next_cursor = data.get("next_starting_after")
        if not next_cursor or len(page_items) < 100:
            break
    return items


def update_lead_custom_variables(lead_id: str, custom_variables: dict) -> None:
    headers = _get_headers()
    resp = requests.patch(
        f"{INSTANTLY_BASE_URL}/leads/{lead_id}",
        headers=headers,
        json={"custom_variables": custom_variables},
        timeout=30,
    )
    if not resp.ok:
        log.error("instantly.update_lead_failed", lead_id=lead_id, status=resp.status_code, body=resp.text[:300])
        resp.raise_for_status()


def deploy_campaign(
    campaign_name: str,
    leads: list[dict],
    sending_accounts: list[str] | None = None,
    campaign_strategy: dict | None = None,
) -> dict:
    """
    Full deployment: create campaign + upload leads.
    If sending_accounts is None, loads them from accounts.csv.
    Returns deployment result with campaign_id and upload stats.
    """
    if sending_accounts is None:
        sending_accounts = load_sending_accounts()

    campaign_id = create_campaign(
        campaign_name,
        sending_accounts=sending_accounts,
        campaign_strategy=campaign_strategy,
    )
    upload_stats = upload_leads(campaign_id, leads, campaign_strategy=campaign_strategy)

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

    return {
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "upload_stats": upload_stats,
    }
