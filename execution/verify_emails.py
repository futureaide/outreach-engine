"""
Step 3: Verify Emails
Splits leads by email_status: validated leads pass through, others go to Reoon.
Only Reoon status "safe" = verified. Everything else is dropped.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

REOON_ENDPOINT = "https://emailverifier.reoon.com/api/v1/verify"
REOON_TIMEOUT = 90
MAX_WORKERS = 5
MAX_RETRIES = 3

# Errors that should abort the entire run immediately
ABORT_STATUS_CODES = {401, 402, 403}


class ReoonAbortError(Exception):
    """Raised when Reoon returns a fatal status code (401/402/403)."""
    pass


def _verify_single_email(email: str, api_key: str) -> tuple[str, str]:
    """
    Verify a single email via Reoon Power mode.
    Returns (email, status) where status is 'safe', 'risky', 'invalid', etc.
    """
    params = {
        "email": email,
        "key": api_key,
        "mode": "power",
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        resp = None
        try:
            resp = requests.get(
                REOON_ENDPOINT,
                params=params,
                timeout=REOON_TIMEOUT,
            )

            if resp.status_code in ABORT_STATUS_CODES:
                raise ReoonAbortError(
                    f"Reoon returned {resp.status_code} for {email}. "
                    f"Aborting — check API key / account balance."
                )

            if resp.status_code == 429:
                wait = (2 ** attempt) * 2
                log.warning("reoon.rate_limit", email=email, retry_in=wait, attempt=attempt + 1)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "unknown").lower()
            return email, status

        except ReoonAbortError:
            raise
        except requests.RequestException as e:
            last_error = e
            if resp is not None and resp.status_code >= 500:
                wait = (2 ** attempt) * 2
                log.warning("reoon.server_error", email=email, status=resp.status_code, retry_in=wait)
                time.sleep(wait)
                continue
            log.error("reoon.request_error", email=email, error=str(e))
            return email, "error"

    log.error("reoon.max_retries", email=email, error=str(last_error))
    return email, "error"


def verify_emails(leads: list[dict]) -> list[dict]:
    """
    Verify email addresses for all leads.
    - Leads with email_status='validated' pass through directly.
    - All others are sent to Reoon; only 'safe' status passes.

    Returns the verified leads pool.
    """
    api_key = os.getenv("REOON_API_KEY")
    if not api_key:
        raise RuntimeError("REOON_API_KEY not set in .env")

    pre_validated = []
    needs_verification = []

    for lead in leads:
        if lead.get("email_status") == "validated":
            pre_validated.append(lead)
        else:
            needs_verification.append(lead)

    log.info(
        "verify.split",
        pre_validated=len(pre_validated),
        needs_verification=len(needs_verification),
    )

    if not needs_verification:
        log.info("verify.all_pre_validated", total=len(pre_validated))
        return pre_validated

    verified_emails = set()
    reoon_cost = len(needs_verification) * 0.0012  # $12/10K credits

    log.info("reoon.starting", count=len(needs_verification), estimated_cost_usd=round(reoon_cost, 2))

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(_verify_single_email, lead["email"], api_key): lead
                for lead in needs_verification
            }

            safe_count = 0
            dropped_count = 0

            for future in as_completed(futures):
                try:
                    email, status = future.result()
                    if status == "safe":
                        verified_emails.add(email)
                        safe_count += 1
                    else:
                        dropped_count += 1
                        log.debug("reoon.dropped", email=email, status=status)
                except ReoonAbortError as e:
                    log.error("reoon.abort", error=str(e))
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise

    except ReoonAbortError:
        raise

    reoon_verified = [l for l in needs_verification if l["email"] in verified_emails]
    for lead in reoon_verified:
        lead["email_status"] = "verified"

    all_verified = pre_validated + reoon_verified

    log.info(
        "verify.complete",
        pre_validated=len(pre_validated),
        reoon_safe=safe_count,
        reoon_dropped=dropped_count,
        total_verified=len(all_verified),
        reoon_cost_usd=round(reoon_cost, 2),
    )

    if not all_verified:
        raise RuntimeError("Zero verified leads — aborting run")

    return all_verified
