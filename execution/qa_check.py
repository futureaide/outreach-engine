"""
Step 6: QA (Quality Assurance)
Every lead's cold-email variables are checked before deployment.
Hard fails trigger regeneration in personalize.py.
"""

from __future__ import annotations

import json
import os
import re

import structlog
from dotenv import load_dotenv

from execution.campaign_strategy import get_approved_ctas, get_custom_variable_keys, render_sequence_preview
from execution.copy_constraints import CONSTRAINT_CONFIGS, DEFAULT_CONSTRAINT_ID
from execution.source_leads import _simplify_company_name

load_dotenv()
log = structlog.get_logger()

ANTHROPIC_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "60"))

BANNED_AI_WORDS = {
    "delve", "leverage", "streamline", "landscape", "robust", "seamless",
    "cutting edge", "game changer", "unlock", "empower", "foster", "holistic",
    "innovative", "groundbreaking", "pivotal", "ever evolving", "harness",
    "optimize", "spearhead", "elevate", "navigate", "amplify", "reimagine",
    "impressed", "unprecedented", "transformative", "synergy", "disruptive",
    "paradigm", "revolutionize", "comprehensive", "facilitate", "utilize",
    "endeavor", "multifaceted", "intricate", "nuanced",
}

BANNED_AI_PHRASES = [
    "in today's fast paced world",
    "seamless integration",
    "a testament to",
    "in the realm of",
    "at the forefront of",
    "poised to",
    "stands out as",
    "it is worth noting",
    "game changer",
    "revolutionary",
]

BANNED_TRANSITIONS = [
    "furthermore", "moreover", "nevertheless", "consequently",
    "accordingly", "in conclusion", "it is worth noting",
    "additionally", "subsequently", "henceforth",
]

BANNED_OUTREACH_PHRASES = [
    "i hope this email finds you well",
    "just wanted to reach out",
    "i came across your profile on linkedin",
    "just bumping this",
    "i noticed on linkedin",
    "saw your headline",
]

FORMAL_EXPANSIONS = {
    "do not": "don't",
    "cannot": "can't",
    "it is": "it's",
    "i am": "i'm",
    "i have": "i've",
    "i will": "i'll",
    "you are": "you're",
    "would not": "wouldn't",
    "should not": "shouldn't",
    "could not": "couldn't",
    "will not": "won't",
    "is not": "isn't",
    "has not": "hasn't",
    "have not": "haven't",
    "did not": "didn't",
    "does not": "doesn't",
}

LOWERCASE_START_VARS = [
    "signal_topic",
    "personalization_line",
    "who_i_help",
    "problem_i_fix",
    "bottleneck_trigger",
    "offer_observation",
    "offer_scope",
    "offer_payoff",
    "followup_ping",
    "proof_problem",
    "proof_fix",
    "proof_payoff",
]

DEFAULT_REQUIRED_VARIABLES = list(LOWERCASE_START_VARS)

DEFAULT_CTA_LADDER = [
    "want me to map the 3 biggest leaks i see?",
    "want the 6 bullet version of what i would fix first?",
    "worth a yes/no?",
]

COLLECTIVE_VOICE_WORDS = ["we", "our", "us"]
BOOKING_WORDS = ["book", "calendar", "call", "meeting", "demo"]
BANNED_PRAISE_STARTS = ["love ", "impressed ", "admire "]
BAD_ENDING_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "if", "in",
    "into", "is", "it", "of", "on", "or", "so", "that", "the", "this",
    "to", "with", "your", "you're", "youre", "once", "gets", "hit", "piece",
    "really", "never", "be", "first", "new", "coverage", "quietly", "whole", "feeling", "something",
}
FOLLOWUP_META_PATTERNS = [
    r"\bcheck(?:ing)?\b",
    r"\bburied\b",
    r"\bcircl(?:e|ing)\s+back\b",
    r"\bbump(?:ing)?\b",
    r"\bwanted to see if\b",
    r"\bleft this here in case\b",
]
AMBIGUOUS_CONTRACTION_PATTERNS = [
    r"\bit['’]d\b",
    r"\bthat['’]d\b",
]
POINT_WORDS = {"usually", "tends", "often", "means", "leaves", "puts", "pulls", "starts", "pushes"}
PAYOFF_WORDS = {
    "time", "times", "pipeline", "replies", "reply", "qualified", "follow", "momentum",
    "speed", "response", "owners", "demos", "lag", "leaks", "chasing", "steadier", "faster",
    "stalling", "moving", "live", "saved", "save", "revenue", "headcount", "handled", "tighter",
}
GENERIC_PROBLEM_WORDS = {"automation", "operations", "growth", "marketing", "systems", "workflow", "workflows"}
TRIGGER_WORDS = {"once", "when", "as", "starts", "gets", "goes", "shows", "after"}
STOPWORDS = {
    "a", "about", "after", "again", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "between", "both", "but", "by", "can", "company",
    "do", "does", "for", "from", "get", "gets", "got", "had", "has", "have", "how", "if",
    "in", "into", "is", "it", "its", "just", "like", "more", "most", "new", "not", "of",
    "on", "once", "one", "or", "our", "out", "over", "same", "so", "some", "still", "team",
    "that", "the", "their", "them", "then", "there", "they", "this", "to", "up", "us",
    "usually", "very", "was", "we", "what", "when", "where", "which", "who", "with", "your",
}


def _get_strategy(strategy: dict | None) -> dict:
    return strategy or {}


def _get_qa_thresholds(strategy: dict | None) -> dict:
    current = _get_strategy(strategy)
    return current.get(
        "qa_thresholds",
        {
            # Total-email sanity ceilings — raised from Nick's targets (130/55/70)
            # to 1.2-1.4x so natural-length variation isn't rejected. The prompt
            # still asks Claude to aim for the tighter Nick targets.
            "email1_max_words": 160,
            "email2_max_words": 80,
            "email3_max_words": 90,
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
        },
    )


def _get_cta_ladder(strategy: dict | None) -> list[str]:
    current = _get_strategy(strategy)
    approved = get_approved_ctas(current)
    return approved or list(DEFAULT_CTA_LADDER)


def _get_required_variables(strategy: dict | None) -> list[str]:
    current = _get_strategy(strategy)
    keys = get_custom_variable_keys(current)
    return keys or list(DEFAULT_REQUIRED_VARIABLES)


def _get_email1_cta(strategy: dict | None) -> str:
    ladder = _get_cta_ladder(strategy)
    return ladder[0] if ladder else DEFAULT_CTA_LADDER[0]


def _render_email_step(lead: dict, variables: dict, strategy: dict | None, index: int) -> str:
    current = _get_strategy(strategy)
    sequence_steps = current.get("sequence_steps", [])
    if index >= len(sequence_steps):
        return ""
    _subject, body = render_sequence_preview(sequence_steps[index], lead, variables)
    return body


def _render_email1(lead: dict, variables: dict, strategy: dict | None = None) -> str:
    rendered = _render_email_step(lead, variables, strategy, 0)
    if rendered:
        return rendered
    cta = _get_email1_cta(strategy)
    return (
        f"Hi {lead.get('first_name', 'there')},\n\n"
        f"{variables.get('personalization_line', '')}.\n\n"
        f"i work with {variables.get('who_i_help', '')} on {variables.get('problem_i_fix', '')}. "
        f"i'm usually pulled in when {variables.get('bottleneck_trigger', '')}.\n\n"
        f"my guess is {variables.get('offer_observation', '')}. if useful, i can map the 3 leaks in your "
        f"{variables.get('offer_scope', '')} so {variables.get('offer_payoff', '')}.\n\n"
        f"{cta}\n\n"
        "Ziad @ futureaide"
    )


def _render_email3(lead: dict, variables: dict, strategy: dict | None = None) -> str:
    rendered = _render_email_step(lead, variables, strategy, 2)
    if rendered:
        return rendered
    return (
        f"{lead.get('first_name', 'there')}, one example before i close the loop.\n\n"
        f"{variables.get('proof_problem', '')}. i fixed it with {variables.get('proof_fix', '')}, "
        f"which {variables.get('proof_payoff', '')}.\n\n"
        "worth a yes/no?\n\n"
        "Ziad @ futureaide"
    )


def _set_qa_result(lead: dict, status: str, failures: list[str] | None = None) -> None:
    lead["qa_status"] = status
    lead["_qa_status"] = status
    if failures is not None:
        lead["qa_failures"] = failures
        lead["_qa_failures"] = failures
    else:
        lead.pop("qa_failures", None)
        lead.pop("_qa_failures", None)


def _join_variable_text(variables: dict) -> str:
    return " ".join(str(v) for v in variables.values()).lower()


def _extract_meaningful_terms(text: str, *, min_len: int = 4) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", (text or "").lower())
    terms: list[str] = []
    for word in words:
        if len(word) < min_len or word in STOPWORDS:
            continue
        if word not in terms:
            terms.append(word)
    return terms


def _extract_source_anchor_terms(lead: dict) -> list[str]:
    text = " ".join(
        [
            str(lead.get("title") or ""),
            str(lead.get("linkedin_headline") or ""),
            str(lead.get("linkedin_job_description") or ""),
            str(lead.get("perplexity_company_context") or ""),
            str(lead.get("company_description") or ""),
            str(lead.get("company_name") or ""),
        ]
    )
    return _extract_meaningful_terms(text, min_len=3)


def _get_lead_dossier_for_qa(lead: dict) -> dict:
    dossier = lead.get("lead_dossier")
    if dossier:
        return dossier
    try:
        from execution.personalize import _build_lead_dossier, extract_primitives

        constraint_id = lead.get("constraint_id", lead.get("_constraint_id", DEFAULT_CONSTRAINT_ID))
        primitives = extract_primitives(lead)
        return _build_lead_dossier(lead, constraint_id, None, primitives=primitives)
    except Exception as exc:
        # Was silently returning {} on any error, which made source-grounding
        # checks toothless — they'd compare generated copy against an empty
        # anchor list and trivially pass. Now we log loudly and return a
        # sentinel that qa_check_lead detects and fails closed.
        log.warning(
            "qa.dossier_rebuild_failed",
            email=lead.get("email", ""),
            error=str(exc),
        )
        return {"__rebuild_failed__": True, "__rebuild_error__": str(exc)}


def _get_constraint_config(lead: dict) -> dict:
    constraint_id = lead.get("constraint_id", lead.get("_constraint_id", DEFAULT_CONSTRAINT_ID))
    return CONSTRAINT_CONFIGS.get(constraint_id, CONSTRAINT_CONFIGS[DEFAULT_CONSTRAINT_ID])


def _check_banned_words(variables: dict) -> list[str]:
    failures = []
    all_text = _join_variable_text(variables)
    for word in BANNED_AI_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", all_text):
            failures.append(f"Banned AI word: '{word}'")
    return failures


def _check_collective_voice(variables: dict) -> list[str]:
    failures = []
    all_text = _join_variable_text(variables)
    for word in COLLECTIVE_VOICE_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", all_text):
            failures.append(f"Collective voice not allowed: '{word}'")
    return failures


def _check_banned_phrases(variables: dict) -> list[str]:
    failures = []
    all_text = _join_variable_text(variables)
    for phrase in BANNED_AI_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", all_text):
            failures.append(f"Banned AI phrase: '{phrase}'")
    for phrase in BANNED_OUTREACH_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", all_text):
            failures.append(f"Banned outreach phrase: '{phrase}'")
    return failures


def _check_banned_transitions(variables: dict) -> list[str]:
    failures = []
    all_text = _join_variable_text(variables)
    for transition in BANNED_TRANSITIONS:
        if re.search(r"\b" + re.escape(transition) + r"\b", all_text):
            failures.append(f"Banned transition: '{transition}'")
    return failures


def _check_formal_expansions(variables: dict) -> list[str]:
    failures = []
    all_text = _join_variable_text(variables)
    for formal, contraction in FORMAL_EXPANSIONS.items():
        if re.search(r"\b" + re.escape(formal) + r"\b", all_text):
            failures.append(f"Use '{contraction}' instead of '{formal}'")
    return failures


def _check_required_variables(variables: dict, strategy: dict | None = None) -> list[str]:
    failures = []
    for key in _get_required_variables(strategy):
        if not (variables.get(key) or "").strip():
            failures.append(f"Missing required variable: '{key}'")
    return failures


def _check_subject_line(variables: dict, strategy: dict | None = None) -> list[str]:
    failures = []
    signal_topic = (variables.get("signal_topic") or "").strip()
    if not signal_topic:
        return failures

    max_words = _get_qa_thresholds(strategy).get("signal_topic_max_words", 4)
    word_count = len(signal_topic.split())
    if word_count < 1 or word_count > max_words:
        failures.append(f"Subject line must be 1-{max_words} words, got {word_count}: '{signal_topic}'")
    if signal_topic != signal_topic.lower():
        failures.append(f"Subject line must be lowercase: '{signal_topic}'")
    if "?" in signal_topic:
        failures.append(f"Subject line must not contain '?': '{signal_topic}'")
    return failures


def _check_email_step_word_count(lead: dict, variables: dict, strategy: dict | None, index: int, threshold_key: str, label: str) -> list[str]:
    rendered = _render_email_step(lead, variables, strategy, index)
    if not rendered:
        return []
    max_words = _get_qa_thresholds(strategy).get(threshold_key)
    if not max_words:
        return []
    word_count = len(rendered.split())
    if word_count > max_words:
        return [f"{label} word count {word_count} > {max_words}"]
    return []


def _check_email1_word_count(lead: dict, variables: dict, strategy: dict | None = None) -> list[str]:
    rendered = _render_email1(lead, variables, strategy)
    max_words = _get_qa_thresholds(strategy).get("email1_max_words", 110)
    word_count = len(rendered.split())
    if word_count > max_words:
        return [f"Email 1 word count {word_count} > {max_words}"]
    return []


def _check_email1_question_marks(strategy: dict | None = None) -> list[str]:
    cta = _get_email1_cta(strategy)
    max_question_marks = _get_qa_thresholds(strategy).get("max_question_marks_email1", 1)
    qmark_count = cta.count("?")
    if qmark_count > max_question_marks:
        return [f"Email 1 CTA has {qmark_count} question marks, expected at most {max_question_marks}"]
    return []


def _check_hyphens_dashes(variables: dict) -> list[str]:
    failures = []
    for key in LOWERCASE_START_VARS:
        value = variables.get(key, "")
        if value and ("-" in value or "—" in value or "–" in value):
            failures.append(f"'{key}' contains hyphen/dash (banned): '{value[:40]}...'")
    return failures


def _check_semicolons_colons(variables: dict) -> list[str]:
    failures = []
    for key in LOWERCASE_START_VARS:
        value = variables.get(key, "")
        if not value:
            continue
        if ";" in value:
            failures.append(f"'{key}' contains semicolon (banned)")
        if ":" in value:
            failures.append(f"'{key}' contains colon (banned)")
    return failures


def _check_lowercase_starts(variables: dict) -> list[str]:
    failures = []
    for key in LOWERCASE_START_VARS:
        value = variables.get(key, "")
        if value and value[0].isupper():
            failures.append(f"'{key}' starts uppercase: '{value[:30]}...'")
    return failures


def _check_max_words(variables: dict, key: str, threshold_key: str, strategy: dict | None = None) -> list[str]:
    """
    Sanity ceiling — not a compliance gate. The threshold is a target
    Claude aims for; natural copy will vary 20-40% around it. Only fail
    at 1.5x the target, which catches genuinely broken output (e.g.
    Claude returning 50 words when the target was 18) without rejecting
    natural-length variation that reads well.

    The old behavior enforced the target as a hard max and the pipeline
    post-hoc chopped overages, producing broken sentences. We stopped
    chopping; this check now catches only the outliers.
    """
    value = (variables.get(key) or "").strip()
    if not value:
        return []
    target = _get_qa_thresholds(strategy).get(threshold_key)
    if not target:
        return []
    ceiling = int(target * 1.5)
    word_count = len(value.split())
    if word_count > ceiling:
        return [f"{key} is {word_count} words (ceiling {ceiling}, target {target})"]
    return []


def _check_personalization_no_praise(variables: dict) -> list[str]:
    personalization = (variables.get("personalization_line") or "").strip().lower()
    for banned in BANNED_PRAISE_STARTS:
        if personalization.startswith(banned):
            return [f"personalization_line starts with praise ('{banned.strip()}'), which breaks Nick's opener rule"]
    return []


def _check_personalization_source_anchor(lead: dict, variables: dict) -> list[str]:
    personalization = (variables.get("personalization_line") or "").strip().lower()
    if not personalization:
        return []
    dossier = _get_lead_dossier_for_qa(lead)
    specialization = (dossier.get("agency_specialization") or "").lower()
    agency_anchor = (dossier.get("agency_anchor") or "").lower()
    normalized_personalization = re.sub(r"[-–—]", " ", personalization)
    source_observations = [re.sub(r"[-–—]", " ", item.lower()) for item in dossier.get("source_observations", [])]
    source_snippets = [re.sub(r"[-–—]", " ", item.lower()) for item in dossier.get("source_snippets", [])]
    source_anchors = [re.sub(r"[-–—]", " ", item.lower()) for item in dossier.get("source_anchors", [])]
    if any(observation in normalized_personalization for observation in source_observations[:4]):
        return []
    if any(snippet in normalized_personalization for snippet in source_snippets[:8]):
        return []
    if any(anchor in normalized_personalization for anchor in source_anchors[:6]):
        return []
    if specialization == "print_promo" and re.search(r"\b(print|printer|printing|production|promo)\b", normalized_personalization):
        return []
    if specialization in {"full_service_creative", "digital_marketing", "social_media"} and re.search(
        r"\b(full service|creative|design|digital marketing|social media)\b",
        normalized_personalization,
    ):
        return []
    if specialization == "paid_media" and re.search(r"\b(paid|ppc|search|social|media)\b", normalized_personalization):
        return []
    if specialization == "activation" and re.search(r"\b(event|activation|experiential|promo)\b", normalized_personalization):
        return []
    if specialization == "pr" and re.search(r"\b(pr|publicity|stories|contracts)\b", normalized_personalization):
        return []
    if agency_anchor == "design and build delivery" and re.search(r"\b(full service|creative|design|digital marketing)\b", normalized_personalization):
        return []
    if agency_anchor == "campaign delivery" and re.search(r"\b(campaign|paid|ppc|search|social|media)\b", normalized_personalization):
        return []
    if agency_anchor == "production heavy delivery" and re.search(r"\b(print|printer|printing|production|promo|fulfillment)\b", normalized_personalization):
        return []
    if agency_anchor == "live activation work" and re.search(r"\b(event|activation|experiential|promo)\b", normalized_personalization):
        return []
    source_terms = _extract_source_anchor_terms(lead)
    if not source_terms:
        return []
    if not any(term in normalized_personalization for term in source_terms[:20]):
        return ["personalization_line does not appear grounded in the lead's source context"]
    return []


def _check_personalization_has_point(variables: dict) -> list[str]:
    personalization = (variables.get("personalization_line") or "").strip().lower()
    if not personalization:
        return []
    if not any(word in personalization.split() for word in POINT_WORDS):
        return ["personalization_line is personal but does not imply a consequence or point"]
    return []


def _check_personalization_sentence_shape(variables: dict) -> list[str]:
    # Import locally to avoid a circular import at module load time; qa_check
    # is imported from personalize during its own initialization.
    from execution.personalize import BANNED_PERSONALIZATION_PREFIXES

    personalization = (variables.get("personalization_line") or "").strip().lower()
    if not personalization:
        return []
    if personalization.startswith(BANNED_PERSONALIZATION_PREFIXES):
        return ["personalization_line reads like a fragment instead of a complete sentence"]
    if not re.search(r"\b(means|mean|puts|put|pulls|pull|leaves|leave|starts|start|gets|get|keeps|keep|slows|slow|pushes|push)\b", personalization):
        return ["personalization_line does not read like a complete observation with a clear verb"]
    return []


def _check_identity_block(lead: dict, variables: dict) -> list[str]:
    failures = []
    config = _get_constraint_config(lead)
    anchor_terms = config.get("anchor_terms", [])
    dossier = _get_lead_dossier_for_qa(lead)
    primary_observation = (dossier.get("primary_observation") or "").lower()
    agency_anchor = (dossier.get("agency_anchor") or "").lower()
    specialization = (dossier.get("agency_specialization") or "").lower()
    source_observations = [item.lower() for item in (dossier.get("source_observations") or [])]

    def has_source(*terms: str) -> bool:
        return any(any(term == obs or term in obs for term in terms) for obs in source_observations)

    if specialization == "pr":
        pr_context, production_context, event_context, creative_context, paid_media_context = True, False, False, False, False
    elif specialization == "paid_media":
        pr_context, production_context, event_context, creative_context, paid_media_context = False, False, False, False, True
    elif specialization in {"execution_fulfillment", "merch_gifting", "print_promo"}:
        pr_context, production_context, event_context, creative_context, paid_media_context = False, True, False, False, False
    elif specialization == "activation":
        pr_context, production_context, event_context, creative_context, paid_media_context = False, False, True, False, False
    elif specialization in {"social_media", "digital_marketing", "full_service_creative", "creative_studio", "b2b_agency", "generic_agency"}:
        pr_context, production_context, event_context, creative_context, paid_media_context = False, False, False, True, False
    else:
        pr_context = agency_anchor == "monthly pr delivery" or has_source(
            "performance based pr",
            "pr delivery",
            "guaranteed stories",
            "month to month contracts",
        )
        production_context = agency_anchor == "production heavy delivery"
        event_context = agency_anchor == "live activation work"
        creative_context = agency_anchor == "design and build delivery" or has_source(
            "full service delivery",
            "creative delivery",
            "full-service digital marketing",
            "digital marketing agency",
        )
        paid_media_context = agency_anchor == "campaign delivery" or has_source(
            "paid media delivery",
            "ppc",
            "paid search",
            "paid social",
        )
    ag_context = "ag marketing" in primary_observation

    who_i_help = (variables.get("who_i_help") or "").strip().lower()
    problem_i_fix = (variables.get("problem_i_fix") or "").strip().lower()
    trigger = (variables.get("bottleneck_trigger") or "").strip().lower()

    if re.search(r"\b(i|we|our|us|my)\b", who_i_help):
        failures.append("who_i_help should be a reference group, not a pitch sentence")
    if len(who_i_help.split()) < 2:
        failures.append("who_i_help is too thin")
    if not re.search(r"\b(agency|agencies|team|teams|saas|founder|founders|owner|owners|ops|firm|firms|shop|shops|studio|studios|operators|consultancies|platforms)\b", who_i_help):
        failures.append("who_i_help should name a relevant reference group")

    if len(problem_i_fix.split()) < 3:
        failures.append("problem_i_fix is too thin")
    if not any(term in problem_i_fix for term in anchor_terms):
        failures.append("problem_i_fix does not describe a concrete bottleneck class")
    if problem_i_fix in GENERIC_PROBLEM_WORDS:
        failures.append("problem_i_fix is too generic")

    if len(trigger.split()) < 4:
        failures.append("bottleneck_trigger is too thin")
    if not any(word in trigger.split() for word in TRIGGER_WORDS):
        failures.append("bottleneck_trigger should explain when the problem shows up")
    if re.search(r"\b(i|we|our|us|my)\b", trigger):
        failures.append("bottleneck_trigger should describe the trigger moment, not the sender")
    if re.search(r"\b(fix|wire|build|help|work with|pulled in)\b", trigger):
        failures.append("bottleneck_trigger should describe when the problem appears, not the pitch")

    if pr_context and not re.search(r"\bpr\b|\bpublic relations\b|\bpublicity\b", who_i_help):
        failures.append("who_i_help should match the PR reference group")
    if production_context and not re.search(r"\b(print|shop|shops|production|promotional|fulfillment|merchandise|gifting|promo)\b", who_i_help):
        failures.append("who_i_help should match the production or print reference group")
    if event_context and not re.search(r"\b(event|activation|experiential)\b", who_i_help):
        failures.append("who_i_help should match the event or activation reference group")
    if paid_media_context and not re.search(r"\b(paid|ppc|media|search|social|digital marketing)\b", who_i_help):
        failures.append("who_i_help should match the paid media reference group")
    # For creative/generic context, accept any who_i_help that names a valid group noun.
    # Claude generates specific, high-quality values like "digital pr agencies", "paid social agencies"
    # that are better than forcing generic "full service creative agencies".
    has_group_noun = re.search(r"\b(agency|agencies|team|teams|firm|firms|shop|shops|studio|studios|founder|founders|owner|owners|operators|consultancies|platforms)\b", who_i_help)
    if creative_context and not has_group_noun:
        failures.append("who_i_help should match the full service or creative reference group")
    if ag_context and "ag" not in who_i_help:
        failures.append("who_i_help should match the ag marketing reference group")

    return failures


def _check_offer_block(lead: dict, variables: dict) -> list[str]:
    failures = []
    config = _get_constraint_config(lead)
    anchor_terms = config.get("anchor_terms", [])
    dossier = _get_lead_dossier_for_qa(lead)
    primary_observation = (dossier.get("primary_observation") or "").lower()
    agency_anchor = (dossier.get("agency_anchor") or "").lower()
    specialization = (dossier.get("agency_specialization") or "").lower()
    source_observations = [item.lower() for item in (dossier.get("source_observations") or [])]

    def has_source(*terms: str) -> bool:
        return any(any(term == obs or term in obs for term in terms) for obs in source_observations)

    if specialization == "pr":
        pr_context, production_context, event_context, paid_media_context = True, False, False, False
    elif specialization == "paid_media":
        pr_context, production_context, event_context, paid_media_context = False, False, False, True
    elif specialization in {"execution_fulfillment", "merch_gifting", "print_promo"}:
        pr_context, production_context, event_context, paid_media_context = False, True, False, False
    elif specialization == "activation":
        pr_context, production_context, event_context, paid_media_context = False, False, True, False
    else:
        pr_context = agency_anchor == "monthly pr delivery" or has_source(
            "performance based pr",
            "pr delivery",
            "guaranteed stories",
            "month to month contracts",
        )
        production_context = agency_anchor == "production heavy delivery"
        event_context = agency_anchor == "live activation work"
        paid_media_context = agency_anchor == "campaign delivery" or has_source(
            "paid media delivery",
            "ppc",
            "paid search",
            "paid social",
        )

    observation = (variables.get("offer_observation") or "").strip().lower()
    offer_scope = (variables.get("offer_scope") or "").strip().lower()
    payoff = (variables.get("offer_payoff") or "").strip().lower()

    if len(observation.split()) < 5:
        failures.append("offer_observation is too thin")
    if re.search(r"\b(i|we|our|us|my|can|will|map|book|call|meeting|demo)\b", observation):
        failures.append("offer_observation should feel like an observation, not a pitch or ask")
    if not any(term in observation for term in anchor_terms):
        failures.append("offer_observation does not feel tied to the specific bottleneck")

    if len(offer_scope.split()) < 3:
        failures.append("offer_scope is too thin")
    if re.search(r"\b(i|we|our|us|my)\b", offer_scope):
        failures.append("offer_scope should be the leak area, not a pitch sentence")
    if re.search(r"\b(can|will|map|book|call|show|fix|help)\b", offer_scope):
        failures.append("offer_scope should stay a concise noun phrase")

    if len(payoff.split()) < 3:
        failures.append("offer_payoff is too thin")
    if not any(word in payoff for word in PAYOFF_WORDS):
        failures.append("offer_payoff does not clearly express ROI or relief")

    if pr_context and not re.search(r"\bpr\b|\bpublic relations\b|\bpublicity\b", observation):
        failures.append("offer_observation should match the PR delivery context")
    if production_context and not re.search(r"\b(production|floor|print|promotional|fulfillment|merchandise|gifting|promo)\b", observation):
        failures.append("offer_observation should match the production context")
    if event_context and not re.search(r"\b(event|activation|experiential|trade show)\b", observation):
        failures.append("offer_observation should match the event context")
    if paid_media_context and not re.search(r"\b(campaign|paid|ppc|search|social|media)\b", observation):
        failures.append("offer_observation should match the paid media context")

    return failures


def _check_offer_reply_first(variables: dict) -> list[str]:
    failures = []
    for key in ["offer_observation", "offer_scope", "offer_payoff", "followup_ping"]:
        line = (variables.get(key) or "").strip().lower()
        if any(re.search(r"\b" + re.escape(word) + r"\b", line) for word in BOOKING_WORDS):
            failures.append(f"{key} should stay reply first and not ask for a meeting")
    return failures


def _check_followup_ping(variables: dict) -> list[str]:
    followup_ping = (variables.get("followup_ping") or "").strip().lower()
    if not followup_ping:
        return []
    for pattern in FOLLOWUP_META_PATTERNS:
        if re.search(pattern, followup_ping):
            return ["followup_ping should advance the issue, not repeat inbox meta language"]
    return []


def _check_followup_context_match(lead: dict, variables: dict) -> list[str]:
    followup_ping = (variables.get("followup_ping") or "").strip().lower()
    if not followup_ping:
        return []
    dossier = _get_lead_dossier_for_qa(lead)
    primary_observation = (dossier.get("primary_observation") or "").lower()
    agency_anchor = (dossier.get("agency_anchor") or "").lower()
    specialization = (dossier.get("agency_specialization") or "").lower()
    source_observations = [item.lower() for item in (dossier.get("source_observations") or [])]

    def has_source(*terms: str) -> bool:
        return any(any(term == obs or term in obs for term in terms) for obs in source_observations)

    if specialization == "pr":
        pr_context, production_context, event_context, paid_media_context = True, False, False, False
    elif specialization == "paid_media":
        pr_context, production_context, event_context, paid_media_context = False, False, False, True
    elif specialization in {"execution_fulfillment", "merch_gifting", "print_promo"}:
        pr_context, production_context, event_context, paid_media_context = False, True, False, False
    elif specialization == "activation":
        pr_context, production_context, event_context, paid_media_context = False, False, True, False
    else:
        pr_context = agency_anchor == "monthly pr delivery" or has_source(
            "performance based pr",
            "pr delivery",
            "guaranteed stories",
            "month to month contracts",
        )
        production_context = agency_anchor == "production heavy delivery"
        event_context = agency_anchor == "live activation work"
        paid_media_context = agency_anchor == "campaign delivery" or has_source(
            "paid media delivery",
            "ppc",
            "paid search",
            "paid social",
        )

    if pr_context and "pr" not in followup_ping:
        return ["followup_ping should match the PR delivery context"]
    if production_context and not re.search(r"\b(production|floor|print|promotional|fulfillment|merchandise|gifting|promo)\b", followup_ping):
        return ["followup_ping should match the production context"]
    if event_context and not re.search(r"\b(event|activation|experiential|trade show)\b", followup_ping):
        return ["followup_ping should match the event context"]
    if paid_media_context and not re.search(r"\b(campaign|paid|ppc|search|social|media)\b", followup_ping):
        return ["followup_ping should match the paid media context"]
    return []


def _check_proof_block(variables: dict) -> list[str]:
    failures = []
    problem = (variables.get("proof_problem") or "").strip().lower()
    fix = (variables.get("proof_fix") or "").strip().lower()
    payoff = (variables.get("proof_payoff") or "").strip().lower()

    if len(problem.split()) < 5:
        failures.append("proof_problem is too thin")
    if len(fix.split()) < 2:
        failures.append("proof_fix is too thin")
    if not re.search(r"\b(flow|system|routing|logic|split|handoff|rule|owner|reminder|escalation)\b", fix):
        failures.append("proof_fix should name the system or mechanism that solved it")
    if len(payoff.split()) < 4:
        failures.append("proof_payoff is too thin")
    if not any(word in payoff for word in PAYOFF_WORDS):
        failures.append("proof_payoff should include the business payoff after the fix")

    proof_text = " ".join([problem, fix, payoff])
    if re.search(r"\d+%", proof_text):
        failures.append("proof block contains percentage claim (banned)")
    if re.search(r"\d+x\b", proof_text):
        failures.append("proof block contains multiplier claim (banned)")

    return failures


def _check_ambiguous_contractions(variables: dict) -> list[str]:
    failures = []
    all_text = _join_variable_text(variables)
    for pattern in AMBIGUOUS_CONTRACTION_PATTERNS:
        match = re.search(pattern, all_text)
        if match:
            failures.append(f"Unclear contraction not allowed: '{match.group(0)}'")
    return failures


def _check_incomplete_line_endings(variables: dict) -> list[str]:
    failures = []
    for key in LOWERCASE_START_VARS:
        value = (variables.get(key) or "").strip()
        if not value:
            continue
        last_word = value.split()[-1].strip(".,!?;:").lower()
        if last_word in BAD_ENDING_WORDS:
            failures.append(f"{key} ends awkwardly on '{last_word}'")
        if re.search(r"\b(?:never|not)\s+really$", value.lower()):
            failures.append(f"{key} ends on a clipped phrase")
    return failures


def _check_company_name_length(lead: dict, strategy: dict | None = None) -> list[str]:
    company = (lead.get("company_name") or "").strip()
    if not company:
        return []
    company = _simplify_company_name(company)
    max_words = _get_qa_thresholds(strategy).get("company_name_max_words", 2)
    if len(company.split()) > max_words:
        return [f"company_name too long ({len(company.split())} words): '{company}' (max {max_words})"]
    return []


def qa_check_lead(lead: dict, campaign_strategy: dict | None = None) -> tuple[bool, list[str]]:
    variables = lead.get("custom_variables", {})
    if not variables:
        return False, ["No custom_variables found"]

    # Fail-closed on dossier rebuild errors. Source-grounding checks rely on
    # the dossier; with an empty one they'd trivially pass and let flawed
    # personalization ship.
    probe_dossier = _get_lead_dossier_for_qa(lead)
    if probe_dossier.get("__rebuild_failed__"):
        return False, [f"Lead dossier unavailable ({probe_dossier.get('__rebuild_error__', 'unknown')}); source-grounding checks cannot run"]

    failures: list[str] = []
    failures.extend(_check_required_variables(variables, campaign_strategy))
    failures.extend(_check_banned_words(variables))
    failures.extend(_check_collective_voice(variables))
    failures.extend(_check_banned_phrases(variables))
    failures.extend(_check_banned_transitions(variables))
    failures.extend(_check_formal_expansions(variables))
    failures.extend(_check_subject_line(variables, campaign_strategy))
    failures.extend(_check_email1_word_count(lead, variables, campaign_strategy))
    failures.extend(_check_email_step_word_count(lead, variables, campaign_strategy, 1, "email2_max_words", "Email 2"))
    failures.extend(_check_email_step_word_count(lead, variables, campaign_strategy, 2, "email3_max_words", "Email 3"))
    failures.extend(_check_email1_question_marks(campaign_strategy))
    failures.extend(_check_hyphens_dashes(variables))
    failures.extend(_check_semicolons_colons(variables))
    failures.extend(_check_lowercase_starts(variables))

    thresholds = _get_qa_thresholds(campaign_strategy)
    for key, threshold_key in [
        ("personalization_line", "personalization_line_max_words"),
        ("who_i_help", "who_i_help_max_words"),
        ("problem_i_fix", "problem_i_fix_max_words"),
        ("bottleneck_trigger", "bottleneck_trigger_max_words"),
        ("offer_observation", "offer_observation_max_words"),
        ("offer_scope", "offer_scope_max_words"),
        ("offer_payoff", "offer_payoff_max_words"),
        ("followup_ping", "followup_ping_max_words"),
        ("proof_problem", "proof_problem_max_words"),
        ("proof_fix", "proof_fix_max_words"),
        ("proof_payoff", "proof_payoff_max_words"),
    ]:
        if threshold_key in thresholds:
            failures.extend(_check_max_words(variables, key, threshold_key, campaign_strategy))

    failures.extend(_check_personalization_no_praise(variables))
    failures.extend(_check_personalization_source_anchor(lead, variables))
    failures.extend(_check_personalization_has_point(variables))
    failures.extend(_check_personalization_sentence_shape(variables))
    failures.extend(_check_identity_block(lead, variables))
    failures.extend(_check_offer_block(lead, variables))
    failures.extend(_check_offer_reply_first(variables))
    failures.extend(_check_followup_ping(variables))
    failures.extend(_check_followup_context_match(lead, variables))
    failures.extend(_check_proof_block(variables))
    failures.extend(_check_ambiguous_contractions(variables))
    failures.extend(_check_incomplete_line_endings(variables))
    failures.extend(_check_company_name_length(lead, campaign_strategy))

    deduped_failures = []
    seen = set()
    for failure in failures:
        if failure not in seen:
            seen.add(failure)
            deduped_failures.append(failure)

    return len(deduped_failures) == 0, deduped_failures


def qa_check_all(leads: list[dict], campaign_strategy: dict | None = None) -> tuple[list[dict], list[dict]]:
    passed = []
    failed = []
    for lead in leads:
        is_pass, failures = qa_check_lead(lead, campaign_strategy=campaign_strategy)
        if is_pass:
            _set_qa_result(lead, "passed")
            passed.append(lead)
        else:
            _set_qa_result(lead, "failed", failures)
            failed.append(lead)
            log.debug("qa.failed", email=lead.get("email"), reasons=failures)

    pass_rate = len(passed) / len(leads) * 100 if leads else 0
    log.info(
        "qa.complete",
        total=len(leads),
        passed=len(passed),
        failed=len(failed),
        pass_rate=f"{pass_rate:.1f}%",
    )
    return passed, failed


COHERENCE_SYSTEM = """You check whether a cold email follows Nick Saraev's framework.
Email 1 should read like one human thought:
1. the opener is specific and feels personally observed
2. the identity paragraph naturally explains why the sender matters
3. the offer paragraph feels like the obvious next step

Email 3 should read like:
1. clear problem
2. clear fix
3. clear payoff

Return ONLY a JSON object:
{"email1_score": <int>, "email3_score": <int>, "issue": "<one sentence or empty>"}
Scores use 1-5, where 5 is natural and 1 is broken."""


def check_flow_coherence(lead: dict, strategy: dict | None = None) -> tuple[bool, str, bool]:
    """
    Returns (passed, issue, skipped). `skipped=True` means we couldn't actually
    run the check (missing lib, missing key, or API error). Callers that care
    about how many leads bypassed the gate should track skipped separately —
    see `qa_check_all_with_coherence`.
    """
    try:
        import anthropic
    except ImportError:
        return True, "", True

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return True, "", True

    variables = lead.get("custom_variables", {})
    if not variables:
        return True, "", False

    email1 = _render_email1(lead, variables, strategy)
    email3 = _render_email3(lead, variables, strategy)

    prompt = f"""Rate these rendered cold emails for flow.

EMAIL 1
{email1}

EMAIL 3
{email3}

Flag generic openers, logic jumps, awkward English, or weak problem/fix/payoff movement.
Return JSON only."""

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=ANTHROPIC_TIMEOUT_SECONDS)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=120,
            temperature=0,
            system=COHERENCE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            timeout=ANTHROPIC_TIMEOUT_SECONDS,
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        result = json.loads(text)
        email1_score = int(result.get("email1_score", 5))
        email3_score = int(result.get("email3_score", 5))
        issue = result.get("issue", "")
        if email1_score <= 3 or email3_score <= 3:
            return False, f"Flow coherence scores e1={email1_score}/5 e3={email3_score}/5: {issue}", False
        return True, "", False
    except Exception as exc:
        # Was `debug` + fail-open silently. Surfaced to warning so operators see
        # when the gate is bypassed, and the skipped flag lets the caller track
        # the rate of bypasses (see COHERENCE_SKIP_ABORT_RATIO below).
        log.warning("coherence.check_error", error=str(exc), email=lead.get("email", ""))
        return True, "", True


def qa_check_all_with_coherence(leads: list[dict], campaign_strategy: dict | None = None) -> tuple[list[dict], list[dict], float]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    passed, failed = qa_check_all(leads, campaign_strategy=campaign_strategy)
    coherence_cost = 0.0
    coherence_failures = []
    coherence_skipped = 0

    def _check_one(lead):
        is_coherent, issue, skipped = check_flow_coherence(lead, strategy=campaign_strategy)
        return lead, is_coherent, issue, skipped

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_check_one, lead): lead for lead in passed}
        for future in as_completed(futures):
            try:
                lead, is_coherent, issue, skipped = future.result()
                coherence_cost += 0.001
                if skipped:
                    coherence_skipped += 1
                if not is_coherent:
                    _set_qa_result(lead, "failed", [issue])
                    coherence_failures.append(lead)
            except Exception as exc:
                coherence_skipped += 1
                log.warning("coherence.future_error", error=str(exc))

    if coherence_failures:
        passed = [lead for lead in passed if lead.get("qa_status", lead.get("_qa_status")) != "failed"]
        failed.extend(coherence_failures)
        log.info("qa.coherence_failures", count=len(coherence_failures))

    # Abort if too many leads bypassed the coherence gate — we'd be charging
    # the operator for a gate that isn't actually running.
    if passed or coherence_failures:
        total_checked = len(passed) + len(coherence_failures) + coherence_skipped
        skip_ratio = coherence_skipped / total_checked if total_checked else 0
        abort_ratio = float(os.getenv("COHERENCE_SKIP_ABORT_RATIO", "0.25"))
        if skip_ratio > abort_ratio:
            raise RuntimeError(
                f"Flow coherence gate bypassed on {coherence_skipped}/{total_checked} leads "
                f"({skip_ratio:.0%} > {abort_ratio:.0%} threshold). "
                f"The coherence check is effectively off — check Anthropic API status "
                f"before continuing."
            )
        if coherence_skipped:
            log.warning(
                "qa.coherence_skipped_tolerated",
                skipped=coherence_skipped,
                total=total_checked,
                skip_ratio=round(skip_ratio, 3),
            )

    return passed, failed, coherence_cost
