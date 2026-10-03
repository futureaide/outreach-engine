# Step 8: Notify & Log (Post-Deploy)

> **Goal:** Log the campaign to Google Sheets and send a Telegram notification with summary stats.
>
> **Script:** `execution/notify.py`

---

## Input

- Campaign ID from Instantly
- Lead upload results (counts, QA exclusions, costs)
- Run metadata (run_id, timestamps)

## Output

- State spreadsheet updated (Campaign_Index + Cost_Tracking tabs)
- Per-campaign Google Sheet created (Leads + Emails + Launch_Preflight tabs)
- Telegram notification sent

---

## Google Sheets Integration

**Auth:** Service account (`GOOGLE_SHEETS_CREDENTIALS_PATH`) or OAuth (`GOOGLE_OAUTH_CREDENTIALS_PATH` + `GOOGLE_TOKEN_PATH`)

### State Spreadsheet (`Outbound_Engine_State`)

**Campaign_Index tab:**

| Column | Description |
|--------|-------------|
| run_id | Unique run identifier |
| campaign_id | Instantly campaign ID |
| created_at | Timestamp |
| leads_uploaded | Actual in-campaign count when available, otherwise API accepted count |
| leads_excluded_qa | Count |
| total_cost | EUR |
| status | draft/active/completed |

**Cost_Tracking tab:**

| Column | Description |
|--------|-------------|
| run_id | Matches Campaign_Index |
| apify_cost | Lead sourcing cost |
| reoon_cost | Verification cost |
| linkedin_cost | LinkedIn enrichment cost |
| perplexity_cost | Company enrichment cost |
| claude_cost | Synthesis cost |
| total_cost | Sum |

**Suppression tab:**

| Column | Description |
|--------|-------------|
| email | Suppressed email address |
| reason | bounce / unsubscribe / complaint |
| added_at | Timestamp |
| source | campaign_id or manual |

### Per-Campaign Sheets

Created per run. Two tabs:

**Leads tab:** lead_id, email, first_name, company_name, title, constraint_id, copy_selector, qa_status, variables (JSON string)

**Emails tab:** lead_id, email_step, subject, body_preview, qa_status

**Launch_Preflight tab:** blockers, warnings, launch notes, and sample open-package previews (subject + teaser)

---

The Emails tab is rendered from the active campaign strategy and the lead's real `custom_variables`. Do not hardcode preview strings or assume `first_name` exists inside `custom_variables`; Instantly resolves `{{firstName}}` and `{{companyName}}` from native lead fields.

## Telegram Notification

- **Endpoint:** `https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage`
- **Chat ID:** from `TELEGRAM_CHAT_ID` env var
- **Content:** Run completion summary (API uploaded count, actual in-campaign count when known, QA exclusions, total cost, campaign name)

---

## Error Handling

| Error | Action |
|-------|--------|
| Google Sheets API failure | Log warning, continue (non-blocking) |
| Telegram API failure | Log warning, continue (non-blocking) |

---

## References

- `api_reference.md` — Google Sheets + Telegram API details
- `07_deploy_campaign.md` — campaign data that feeds into logging
