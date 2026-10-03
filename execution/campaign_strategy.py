"""
Campaign brief + strategy helpers.

This module is the new tactical source of truth for outreach behavior.
It loads a campaign brief, loads the Nick Saraev doctrine asset, compiles a
runtime strategy object, and exposes helpers used by personalization, QA,
deployment, notifications, and reply handling.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAMPAIGNS_DIR = PROJECT_ROOT / "campaigns"
DEFAULT_CAMPAIGN_BRIEF_PATH = CAMPAIGNS_DIR / "futureaide_default.json"
DEFAULT_DOCTRINE_PATH = CAMPAIGNS_DIR / "nick_saraev_doctrine.json"
RUN_SCHEMA_VERSION = "2026-03-26-v5"


DEFAULT_QA_THRESHOLDS = {
    "email1_max_words": 110,
    "email2_max_words": 55,
    "email3_max_words": 70,
    "personalization_line_max_words": 18,
    "who_i_help_max_words": 6,
    "problem_i_fix_max_words": 7,
    "bottleneck_trigger_max_words": 13,
    "offer_observation_max_words": 16,
    "offer_scope_max_words": 8,
    "offer_payoff_max_words": 14,
    "followup_ping_max_words": 15,
    "proof_problem_max_words": 14,
    "proof_fix_max_words": 7,
    "proof_payoff_max_words": 12,
    "signal_topic_max_words": 4,
    "max_question_marks_email1": 1,
    "company_name_max_words": 2,
}

DEFAULT_CTA_LADDER = [
    "want me to map the 3 biggest leaks i see?",
    "want the 6 bullet version of what i would fix first?",
    "worth a yes/no?",
]

DEFAULT_LAUNCH_SETTINGS = {
    "recommended_min_cost_eur": 90,
    "daily_limit": 50,
    "upload_buffer_ratio": 0.2,
    "upload_buffer_min": 150,
    "pilot_send_count": 250,
    "max_active_variants": 2,
    "min_leads_per_variant": 500,
    "iteration_day": "sunday",
}


class CampaignConfigError(ValueError):
    """Raised when a campaign brief or doctrine asset is malformed."""


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_doctrine(path: str | None = None) -> dict:
    doctrine_path = Path(path) if path else DEFAULT_DOCTRINE_PATH
    if not doctrine_path.exists():
        raise CampaignConfigError(f"Doctrine asset not found: {doctrine_path}")
    doctrine = _load_json(doctrine_path)
    required = [
        "trust_principles",
        "outbound_components",
        "copy_framework",
        "offer_formula",
        "subject_lines",
        "follow_ups",
        "iteration",
        "platform_notes",
        "ai_usage",
        "prompt_blocks",
    ]
    missing = [key for key in required if key not in doctrine]
    if missing:
        raise CampaignConfigError(f"Doctrine asset missing keys: {', '.join(missing)}")
    doctrine["_path"] = str(doctrine_path)
    return doctrine


def load_campaign_brief(path: str | None = None) -> dict:
    brief_path = Path(path) if path else DEFAULT_CAMPAIGN_BRIEF_PATH
    if not brief_path.exists():
        raise CampaignConfigError(f"Campaign brief not found: {brief_path}")

    brief = _load_json(brief_path)
    required = [
        "campaign_name",
        "channel",
        "primary_goal",
        "icp",
        "offer",
        "trust_assets",
        "proof_library",
        "sequence_blueprint",
        "selectors",
        "qa_thresholds",
        "reply_playbook",
        "model_settings",
    ]
    missing = [key for key in required if key not in brief]
    if missing:
        raise CampaignConfigError(f"Campaign brief missing keys: {', '.join(missing)}")

    brief.setdefault("brief_key", brief_path.stem)
    brief["_path"] = str(brief_path)
    return brief


def compile_campaign_strategy(brief: dict, doctrine: dict | None = None) -> dict:
    """Compile the user-facing brief into a runtime strategy."""
    doctrine = doctrine or load_doctrine()
    strategy = deepcopy(brief)
    strategy["doctrine"] = doctrine
    strategy["schema_version"] = RUN_SCHEMA_VERSION
    strategy["brief_key"] = brief.get("brief_key", Path(brief.get("_path", "campaign")).stem)

    qa_thresholds = dict(DEFAULT_QA_THRESHOLDS)
    qa_thresholds.update(brief.get("qa_thresholds", {}))
    strategy["qa_thresholds"] = qa_thresholds

    cta_ladder = brief.get("offer", {}).get("cta_ladder") or list(DEFAULT_CTA_LADDER)
    strategy["cta_ladder"] = cta_ladder

    launch_settings = dict(DEFAULT_LAUNCH_SETTINGS)
    launch_settings.update(brief.get("launch_settings", {}))
    strategy["launch_settings"] = launch_settings

    sequence_steps = []
    blueprint = brief.get("sequence_blueprint", {})
    step_keys = sorted(
        [key for key in blueprint if re.fullmatch(r"email_\d+", key)],
        key=lambda key: int(key.split("_")[1]),
    )
    if not step_keys:
        raise CampaignConfigError("Campaign brief must define at least one email_N sequence step")
    for step_key in step_keys:
        step = deepcopy(blueprint.get(step_key, {}))
        if not step:
            raise CampaignConfigError(f"Campaign brief missing sequence step: {step_key}")
        step["key"] = step_key
        sequence_steps.append(step)
    strategy["sequence_steps"] = sequence_steps

    strategy["trust_mix"] = brief.get("selectors", {}).get(
        "trust_mix",
        ["give_first", "micro_commitments", "social_proof", "authority", "rapport", "scarcity", "shared_identity"],
    )
    strategy["identity_mode"] = brief.get("trust_assets", {}).get("identity_mode", "operator")
    strategy["offer_frame"] = brief.get("offer", {}).get("frame", "give_first")
    strategy["proof_mode"] = brief.get("selectors", {}).get("proof_mode", "matched_operational_story")
    strategy["subject_style_set"] = brief.get("selectors", {}).get("subject_style_set", ["lowercase_internal", "plausible_deniability"])
    strategy["reply_rules"] = brief.get("reply_playbook", {})
    strategy["regen_rules"] = brief.get("selectors", {}).get("regen_rules", ["fix_flow", "fix_specificity", "fix_trust_signal"])
    return strategy


def load_campaign_strategy(path: str | None = None) -> tuple[dict, dict]:
    brief = load_campaign_brief(path)
    doctrine = load_doctrine()
    return brief, compile_campaign_strategy(brief, doctrine)


@lru_cache(maxsize=1)
def list_campaign_briefs() -> list[dict]:
    briefs: list[dict] = []
    for path in sorted(CAMPAIGNS_DIR.glob("*.json")):
        if path == DEFAULT_DOCTRINE_PATH:
            continue
        try:
            briefs.append(load_campaign_brief(str(path)))
        except CampaignConfigError:
            continue
    return briefs


def resolve_campaign_brief_path(campaign_name: str | None, default_path: str | None = None) -> Path:
    fallback = Path(default_path) if default_path else DEFAULT_CAMPAIGN_BRIEF_PATH
    if not campaign_name:
        return fallback

    normalized_name = campaign_name.strip()
    lower_name = normalized_name.lower()
    bracket_match = re.match(r"^\[([A-Za-z0-9_\-]+)\]", normalized_name)
    brief_key = bracket_match.group(1) if bracket_match else ""

    for brief in list_campaign_briefs():
        current_path = Path(brief["_path"])
        current_key = str(brief.get("brief_key", "")).lower()
        current_name = str(brief.get("campaign_name", "")).lower()
        if brief_key and current_key == brief_key.lower():
            return current_path
        if current_name and current_name in lower_name:
            return current_path
        if current_key and current_key in lower_name:
            return current_path

    return fallback


def load_campaign_strategy_for_name(
    campaign_name: str | None,
    default_path: str | None = None,
) -> tuple[dict, dict]:
    resolved_path = resolve_campaign_brief_path(campaign_name, default_path=default_path)
    return load_campaign_strategy(str(resolved_path))


def summarize_doctrine_for_prompt(doctrine: dict) -> str:
    """Return a concise prompt-ready summary of the Nick doctrine."""
    trust_lines = []
    for key, value in doctrine.get("trust_principles", {}).items():
        trust_lines.append(f"- {key}: {value['prompt_rule']}")

    framework_lines = []
    for step, value in doctrine.get("copy_framework", {}).items():
        framework_lines.append(f"- {step}: {value['prompt_rule']}")

    lines = [
        "Nick Saraev doctrine:",
        "Trust principles:",
        *trust_lines,
        "Outbound components:",
        *[f"- {key}: {value['prompt_rule']}" for key, value in doctrine.get("outbound_components", {}).items()],
        "Copy framework:",
        *framework_lines,
        "Offer formula:",
        f"- {doctrine['offer_formula']['prompt_rule']}",
        "Subject lines:",
        f"- {doctrine['subject_lines']['prompt_rule']}",
        "Follow-ups:",
        f"- {doctrine['follow_ups']['prompt_rule']}",
        "Iteration:",
        f"- {doctrine['iteration']['prompt_rule']}",
    ]

    if doctrine.get("launch_rules"):
        lines.extend(
            [
                "Launch rules:",
                *[f"- {key}: {value['prompt_rule']}" for key, value in doctrine.get("launch_rules", {}).items()],
            ]
        )
    if doctrine.get("personalization_rules"):
        lines.extend(
            [
                "Personalization rules:",
                *[f"- {key}: {value['prompt_rule']}" for key, value in doctrine.get("personalization_rules", {}).items()],
            ]
        )

    lines.extend(
        [
            "AI usage:",
            f"- {doctrine['ai_usage']['prompt_rule']}",
        ]
    )
    return "\n".join(lines)


def get_sequence_step(strategy: dict, step_key: str) -> dict:
    for step in strategy.get("sequence_steps", []):
        if step.get("key") == step_key:
            return step
    raise CampaignConfigError(f"Unknown sequence step: {step_key}")


def _placeholder_context(lead: dict, variables: dict) -> dict:
    context = {
        "firstName": lead.get("first_name", ""),
        "lastName": lead.get("last_name", ""),
        "companyName": lead.get("company_name", ""),
        "email": lead.get("email", ""),
    }
    context.update({k: str(v) for k, v in variables.items()})
    return context


def render_template_string(template: str, lead: dict, variables: dict) -> str:
    """Render an Instantly-style {{placeholder}} string with lead + variable context."""
    context = _placeholder_context(lead, variables)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, "")

    return re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", replace, template)


def render_body_lines(body_lines: list[str], lead: dict, variables: dict) -> list[str]:
    return [render_template_string(line, lead, variables) for line in body_lines]


def body_lines_to_html(body_lines: list[str]) -> str:
    html_parts = ["<div>"]
    for line in body_lines:
        if line:
            html_parts.append(line)
        html_parts.append("<br><br>" if line else "<br>")
    if html_parts[-1] in {"<br>", "<br><br>"}:
        html_parts.pop()
    html_parts.append("</div>")
    return "".join(html_parts)


def render_sequence_preview(step: dict, lead: dict, variables: dict, variant_index: int = 0) -> tuple[str, str]:
    subjects = step.get("subjects", [])
    subject_template = subjects[min(variant_index, len(subjects) - 1)] if subjects else ""
    subject = render_template_string(subject_template, lead, variables)
    lines = render_body_lines(step.get("body_lines", []), lead, variables)
    body = "\n".join(line for line in lines if line != "")
    return subject, body


def render_sequence_open_bundle(step: dict, lead: dict, variables: dict, variant_index: int = 0) -> dict:
    subject, body = render_sequence_preview(step, lead, variables, variant_index=variant_index)
    teaser = re.sub(r"\s+", " ", body.replace("\n", " ")).strip()[:140]
    return {
        "subject": subject,
        "body": body,
        "teaser": teaser,
    }


def get_identity_signal(strategy: dict, index: int = 0) -> str:
    identity_signals = strategy.get("trust_assets", {}).get("identity_signals", [])
    if not identity_signals:
        return ""
    return identity_signals[index % len(identity_signals)]


def get_primary_reply_cta(strategy: dict) -> str:
    ladder = strategy.get("cta_ladder", [])
    return ladder[0] if ladder else DEFAULT_CTA_LADDER[0]


def get_approved_ctas(strategy: dict) -> list[str]:
    return list(strategy.get("cta_ladder", []) or DEFAULT_CTA_LADDER)


def get_proof_library(strategy: dict) -> dict:
    return strategy.get("proof_library", {})


def get_reply_assets(strategy: dict) -> dict:
    return strategy.get("reply_playbook", {})


def get_launch_settings(strategy: dict | None) -> dict:
    current = strategy or {}
    launch_settings = dict(DEFAULT_LAUNCH_SETTINGS)
    launch_settings.update(current.get("launch_settings", {}))
    return launch_settings


def get_custom_variable_keys(strategy: dict | None) -> list[str]:
    current = strategy or {}
    keys: list[str] = []
    native_keys = {"firstName", "lastName", "companyName", "email"}

    for step in current.get("sequence_steps", []):
        for template in [*(step.get("subjects", []) or []), *(step.get("body_lines", []) or [])]:
            for match in re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", template):
                if match in native_keys or match in keys:
                    continue
                keys.append(match)

    return keys
