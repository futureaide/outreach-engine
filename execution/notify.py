"""
Logging + Notifications
- Google Sheets: Campaign_Index, Cost_Tracking, per-campaign sheets
- Telegram: run completion notifications
"""

import json
import os
from datetime import datetime

import requests
import structlog
from dotenv import load_dotenv

from execution.campaign_strategy import render_sequence_preview

load_dotenv()
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def _get_gspread_client():
    """Authenticate and return a gspread client."""
    import gspread
    from google.oauth2.credentials import Credentials

    token_path = os.getenv("GOOGLE_TOKEN_PATH", "./token.json")

    if not os.path.exists(token_path):
        raise FileNotFoundError(
            f"Google OAuth token not found at {token_path}. "
            "Run OAuth flow first to generate token.json."
        )

    creds = Credentials.from_authorized_user_file(token_path)
    return gspread.authorize(creds)


def _get_or_create_worksheet(spreadsheet, title: str, headers: list[str]):
    """Get existing worksheet or create it with headers."""
    import gspread

    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.update(f"A1:{chr(64 + len(headers))}1", [headers])
        log.info("sheets.created_tab", tab=title)

    return ws


# ---------------------------------------------------------------------------
# Google Sheets: State Spreadsheet
# ---------------------------------------------------------------------------

def log_to_campaign_index(
    run_id: str,
    campaign_id: str,
    leads_uploaded: int,
    leads_excluded_qa: int,
    total_cost: float,
    status: str = "draft",
) -> None:
    """Log a row to the Campaign_Index tab."""
    try:
        gc = _get_gspread_client()
        spreadsheet_name = os.getenv("STATE_SPREADSHEET_NAME", "Outbound_Engine_State")
        sh = gc.open(spreadsheet_name)

        headers = ["run_id", "campaign_id", "created_at", "leads_uploaded",
                    "leads_excluded_qa", "total_cost", "status"]
        ws = _get_or_create_worksheet(sh, "Campaign_Index", headers)

        row = [
            run_id,
            campaign_id,
            datetime.now().isoformat(),
            leads_uploaded,
            leads_excluded_qa,
            round(total_cost, 2),
            status,
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("sheets.campaign_index_logged", run_id=run_id)

    except Exception as e:
        log.error("sheets.campaign_index_error", error=str(e))


def log_to_cost_tracking(run_id: str, costs: dict) -> None:
    """Log cost breakdown to the Cost_Tracking tab."""
    try:
        gc = _get_gspread_client()
        spreadsheet_name = os.getenv("STATE_SPREADSHEET_NAME", "Outbound_Engine_State")
        sh = gc.open(spreadsheet_name)

        headers = ["run_id", "apify_cost", "reoon_cost", "linkedin_cost",
                    "perplexity_cost", "claude_cost", "total_cost"]
        ws = _get_or_create_worksheet(sh, "Cost_Tracking", headers)

        total = sum(costs.values())
        row = [
            run_id,
            round(costs.get("apify_cost", 0), 4),
            round(costs.get("reoon_cost", 0), 4),
            round(costs.get("linkedin_cost", 0), 4),
            round(costs.get("perplexity_cost", 0), 4),
            round(costs.get("claude_cost", 0), 4),
            round(total, 2),
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("sheets.cost_tracking_logged", run_id=run_id, total=round(total, 2))

    except Exception as e:
        log.error("sheets.cost_tracking_error", error=str(e))


# ---------------------------------------------------------------------------
# Google Sheets: Per-Campaign Sheet
# ---------------------------------------------------------------------------

def create_campaign_sheet(
    run_id: str,
    campaign_name: str,
    passed_leads: list[dict],
    failed_leads: list[dict],
    campaign_strategy: dict | None = None,
    launch_preflight: dict | None = None,
) -> None:
    """Create a per-campaign Google Sheet with Leads and Emails tabs."""
    try:
        gc = _get_gspread_client()
        sheet_title = f"{campaign_name} - {run_id[:8]}"

        sh = gc.create(sheet_title)

        # Leads tab
        leads_headers = ["lead_id", "email", "first_name", "company_name",
                         "title", "constraint_id", "copy_selector", "qa_status", "variables"]
        leads_ws = sh.sheet1
        leads_ws.update_title("Leads")
        leads_ws.update("A1:I1", [leads_headers])

        rows = []
        all_leads = passed_leads + failed_leads
        for i, lead in enumerate(all_leads):
            variables = lead.get("custom_variables", {})
            rows.append([
                f"L{i+1:04d}",
                lead.get("email", ""),
                lead.get("first_name", ""),
                lead.get("company_name", ""),
                lead.get("title", ""),
                lead.get("constraint_id", lead.get("_constraint_id", "")),
                lead.get("copy_selector", ""),
                lead.get("qa_status", lead.get("_qa_status", "")),
                json.dumps(variables) if variables else "",
            ])

        if rows:
            leads_ws.update(f"A2:I{len(rows) + 1}", rows, value_input_option="USER_ENTERED")

        # Emails tab
        emails_headers = ["lead_id", "email_step", "subject", "body_preview", "qa_status"]
        sequence_steps = (campaign_strategy or {}).get("sequence_steps", [])
        emails_ws = sh.add_worksheet(
            title="Emails",
            rows=len(all_leads) * max(1, len(sequence_steps)) + 1,
            cols=5,
        )
        emails_ws.update("A1:E1", [emails_headers])

        email_rows = []
        for i, lead in enumerate(passed_leads):
            lead_id = f"L{i+1:04d}"
            v = lead.get("custom_variables", {})
            for step_index, step in enumerate(sequence_steps, start=1):
                subject, preview = render_sequence_preview(step, lead, v)
                email_rows.append([
                    lead_id,
                    step_index,
                    subject,
                    preview[:500],
                    lead.get("qa_status", lead.get("_qa_status", "")),
                ])

        if email_rows:
            emails_ws.update(f"A2:E{len(email_rows) + 1}", email_rows, value_input_option="USER_ENTERED")

        if launch_preflight:
            preflight_ws = sh.add_worksheet(title="Launch_Preflight", rows=200, cols=4)
            preflight_ws.update("A1:D1", [["section", "item", "value", "notes"]])
            rows = []
            for blocker in launch_preflight.get("blockers", []):
                rows.append(["blocker", "launch", blocker, ""])
            for warning in launch_preflight.get("warnings", []):
                rows.append(["warning", "launch", warning, ""])
            for note in launch_preflight.get("notes", []):
                rows.append(["note", "launch", note, ""])
            for preview in launch_preflight.get("preview_samples", []):
                for idx, bundle in enumerate(preview.get("open_package", []), start=1):
                    rows.append([
                        "preview",
                        preview.get("email", ""),
                        bundle.get("subject", ""),
                        bundle.get("teaser", ""),
                    ])
                    if idx >= 2:
                        break
            if rows:
                preflight_ws.update(f"A2:D{len(rows) + 1}", rows, value_input_option="USER_ENTERED")

        log.info("sheets.campaign_sheet_created", title=sheet_title, leads=len(all_leads))

    except Exception as e:
        log.error("sheets.campaign_sheet_error", error=str(e))


# ---------------------------------------------------------------------------
# Telegram Notification
# ---------------------------------------------------------------------------

def send_telegram_notification(
    run_id: str,
    campaign_name: str,
    campaign_id: str,
    leads_uploaded: int,
    leads_excluded_qa: int,
    total_cost: float,
    upload_stats: dict | None = None,
    abort_reason: str | None = None,
) -> None:
    """
    Send a run completion summary via Telegram.

    The message header depends on the run's end state, detected from
    `campaign_id`:
      - "ABORTED" → "Campaign ABORTED" (surfaces the abort_reason)
      - "dry-run" → "Dry Run Complete" (explicit test-run label)
      - anything else → "Campaign Deployed (DRAFT)"
    Previously every run — including aborts and dry-runs — was labelled as
    "Campaign Deployed (DRAFT)", which produced misleading notifications for
    test runs and aborted pipelines.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        log.warning("telegram.not_configured", msg="TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return

    success_rate = upload_stats.get("success_rate", "N/A") if upload_stats else "N/A"
    actually_in_campaign = upload_stats.get("actually_in_campaign") if upload_stats else None
    actual_line = f"Actually in campaign: {actually_in_campaign}\n" if actually_in_campaign is not None else ""

    if campaign_id == "ABORTED":
        header = "Campaign ABORTED"
        status_line = f"Status: ABORTED — {abort_reason or 'see logs'}"
    elif campaign_id == "dry-run":
        header = "Dry Run Complete"
        status_line = "Status: DRY RUN (nothing deployed to Instantly)"
    else:
        header = "Campaign Deployed (DRAFT)"
        status_line = "Status: DRAFT (manual activation required)"

    message = (
        f"{header}\n"
        f"{'=' * 30}\n"
        f"Run: {run_id}\n"
        f"Campaign: {campaign_name}\n"
        f"Campaign ID: {campaign_id}\n"
        f"Leads uploaded: {leads_uploaded}\n"
        f"{actual_line}"
        f"Leads excluded (QA): {leads_excluded_qa}\n"
        f"Upload success rate: {success_rate}%\n"
        f"Total cost: EUR {total_cost:.2f}\n"
        f"{status_line}"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("telegram.sent", run_id=run_id)
    except requests.RequestException as e:
        log.error("telegram.send_error", error=str(e))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def notify_completion(
    run_id: str,
    campaign_name: str,
    campaign_id: str,
    passed_leads: list[dict],
    failed_leads: list[dict],
    costs: dict,
    upload_stats: dict | None = None,
    campaign_strategy: dict | None = None,
    launch_preflight: dict | None = None,
) -> None:
    """Run all notification steps: Sheets logging + Telegram."""
    total_cost = sum(costs.values())
    leads_uploaded = (upload_stats or {}).get("actually_in_campaign")
    if leads_uploaded is None:
        leads_uploaded = (upload_stats or {}).get("uploaded", len(passed_leads))
    leads_excluded_qa = len(failed_leads)

    log_to_campaign_index(
        run_id=run_id,
        campaign_id=campaign_id,
        leads_uploaded=leads_uploaded,
        leads_excluded_qa=leads_excluded_qa,
        total_cost=total_cost,
        status="draft",
    )

    log_to_cost_tracking(run_id=run_id, costs=costs)

    create_campaign_sheet(
        run_id=run_id,
        campaign_name=campaign_name,
        passed_leads=passed_leads,
        failed_leads=failed_leads,
        campaign_strategy=campaign_strategy,
        launch_preflight=launch_preflight,
    )

    send_telegram_notification(
        run_id=run_id,
        campaign_name=campaign_name,
        campaign_id=campaign_id,
        leads_uploaded=leads_uploaded,
        leads_excluded_qa=leads_excluded_qa,
        total_cost=total_cost,
        upload_stats=upload_stats,
    )

    log.info("notify.complete", run_id=run_id)
