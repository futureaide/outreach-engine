"""
Shared cold-email constraint configuration.

This module centralizes the bottleneck themes that drive selection, fallbacks,
deterministic fragments, and proof structures for Nick-aligned cold email.
"""

from __future__ import annotations


DEFAULT_CONSTRAINT_ID = "agency_delivery_drag"


CONSTRAINT_CONFIGS = {
    "agency_delivery_drag": {
        "implication": "when client delivery gets heavy, new business follow up usually slips first",
        "anchor_terms": [
            "delivery",
            "follow up",
            "new business",
            "pipeline",
            "replies",
            "ownership",
            "handoff",
            "routing",
        ],
        "triggers": {
            "role_bucket": ["founder", "growth", "other"],
            "lane": ["performance_marketing_agency", "generic"],
        },
        "deterministic": {
            "offer_scope": "follow up flow",
            "followup_ping": "new business follow up usually starts slipping once delivery weeks stack up",
        },
        "fallback_dynamic": {
            "signal_topic": "follow up ownership",
            "personalization_line": "client delivery usually means new business follow up gets whatever time is left",
            "who_i_help": "performance agencies",
            "problem_i_fix": "reply handling and handoff leaks",
            "bottleneck_trigger": "client delivery starts crowding out new business follow up",
            "offer_observation": "a few reply leaks are probably already showing up once delivery gets heavy",
            "offer_payoff": "warm replies stop stalling",
        },
        "proof_variants": [
            {
                "problem": "client delivery kept eating new business time and reply handling was running 3 to 4 days behind",
                "fix": "one owner routing flow",
                "payoff": "cut reply lag from days to same day and freed up about 5 hours a week",
            },
            {
                "problem": "warm replies were sitting in a shared inbox with nobody clearly owning the next step",
                "fix": "one routing and reminder flow",
                "payoff": "recovered 11 stalled replies in the first two weeks and saved about 4 hours of manual chasing",
            },
            {
                "problem": "follow up kept slipping every time a new client onboarded",
                "fix": "one handoff and ownership system",
                "payoff": "cut missed follow ups by about 80 percent in the first month",
            },
            {
                "problem": "reply ownership kept falling between delivery and new business",
                "fix": "one routing logic and escalation flow",
                "payoff": "cut average reply time from 36 hours to 8 hours and recovered 6 warm leads in the first two weeks",
            },
            {
                "problem": "month to month PR delivery kept pushing follow up to the side",
                "fix": "one owner and routing flow",
                "payoff": "recovered 8 warm leads in the first sprint and kept new business moving during delivery weeks",
            },
            {
                "problem": "paid media delivery kept pulling attention away from new business replies",
                "fix": "one reply routing flow",
                "payoff": "cut reply lag to same day and saved about 6 hours a week on admin",
            },
            {
                "problem": "full service projects kept pushing website leads into a slow handoff",
                "fix": "one handoff and reminder flow",
                "payoff": "recovered 12 stalled replies in the first two weeks and kept pipeline moving",
            },
            {
                "problem": "social media delivery kept pulling the team away from new business follow up",
                "fix": "one reply owner and routing flow",
                "payoff": "cut reply lag from 2 days to under 6 hours and saved about 4 hours a week",
            },
            {
                "problem": "branding projects kept pushing follow up into the background",
                "fix": "one handoff and escalation flow",
                "payoff": "recovered 9 warm leads stalling between project milestones",
            },
            {
                "problem": "client onboarding kept eating the week and new business replies piled up",
                "fix": "one owner and reminder system",
                "payoff": "cut reply backlog from 15 to 3 in the first two weeks",
            },
            {
                "problem": "campaign reporting was stealing time from new business follow up",
                "fix": "one routing and ownership flow",
                "payoff": "got warm replies handled same day and freed up about 5 hours a week",
            },
            {
                "problem": "creative reviews and client revisions kept pushing pipeline work out",
                "fix": "one handoff and reminder flow",
                "payoff": "recovered 10 stalled replies and kept new business moving without adding overhead",
            },
            {
                "problem": "fulfillment timelines kept crowding out new business reply handling",
                "fix": "one routing and escalation system",
                "payoff": "got replies handled same day and saved about 7 hours a week on manual chasing",
            },
            {
                "problem": "media planning cycles kept pushing reply ownership to whoever had time",
                "fix": "one owner routing flow",
                "payoff": "cut reply lag from 2 days to 14 hours and recovered about 9 stalled conversations a month",
            },
            {
                "problem": "month to month pr retainers meant new business replies sat between media pitches",
                "fix": "one pr owner assignment and reminder flow",
                "payoff": "cut reply time from 4 days to same day and kept 8 warm leads alive in a month",
            },
            {
                "problem": "publicity campaigns kept pulling the team before warm replies got answered",
                "fix": "one reply ownership rule tied to the pr calendar",
                "payoff": "got reply time under 12 hours and recovered 7 warm pitches in the first two weeks",
            },
            {
                "problem": "pr reporting cycles kept eating time that should have gone to inbound replies",
                "fix": "one simple routing rule with a pr-owner fallback",
                "payoff": "saved about 5 hours a week and recovered 10 stalled conversations in a month",
            },
            {
                "problem": "monthly paid media reporting kept new business replies on hold for the first week",
                "fix": "one reply owner and reminder system",
                "payoff": "got reply time under 24 hours and recovered about 8 warm leads a month",
            },
            {
                "problem": "paid search and paid social delivery kept pulling ae time away from inbound",
                "fix": "one reply triage and reassignment rule",
                "payoff": "cut reply time from 3 days to same day and saved about 4 hours a week",
            },
            {
                "problem": "performance reviews and campaign reporting delayed new business replies by 2 to 3 days",
                "fix": "one routing rule split by owner and media channel",
                "payoff": "cut reply lag to under 12 hours and saved about 6 hours a week",
            },
            {
                "problem": "multi-phase branding projects kept stretching team bandwidth and new business replies slipped",
                "fix": "one ownership rotation and follow up reminder",
                "payoff": "kept reply lag under 18 hours and saved about 5 hours a week on manual follow up",
            },
            {
                "problem": "design sprints and client revisions pushed warm inbound conversations to whenever there was time",
                "fix": "one triage and handoff flow for new business replies",
                "payoff": "cut reply lag from 5 days to under 24 hours and recovered 11 warm leads in a month",
            },
            {
                "problem": "fulfillment ramps kept pulling account managers away from new business replies",
                "fix": "one reply triage and escalation flow",
                "payoff": "cut lag from 3 days to 8 hours and recovered about 9 warm leads in the first month",
            },
            {
                "problem": "production scheduling took most of the team's attention and reply queues grew",
                "fix": "one owner rotation for warm replies during production weeks",
                "payoff": "saved about 6 hours a week and kept reply times under 12 hours",
            },
            {
                "problem": "shipping and fulfillment cycles kept blocking warm quote replies",
                "fix": "one routing and reminder rule for inbound quote inquiries",
                "payoff": "cut reply time from days to same day and kept about 10 more quote conversations alive monthly",
            },
            {
                "problem": "event prep weeks kept pulling the team away from warm inbound replies",
                "fix": "one reply owner and reminder flow tied to the event calendar",
                "payoff": "cut reply lag from 4 days to same day and saved about 5 hours a week",
            },
            {
                "problem": "on-site activation and staffing ramps kept pushing warm conversations off the schedule",
                "fix": "one routing and escalation rule for warm replies during activations",
                "payoff": "got reply time under 12 hours and recovered 8 warm leads in the first 3 weeks",
            },
            {
                "problem": "tight event timelines kept crowding out new business follow up",
                "fix": "one reply triage and owner assignment during event weeks",
                "payoff": "cut reply lag from days to same day and kept about 9 more conversations alive a month",
            },
        ],
    },
    "handoff_leakage_ops": {
        "implication": "as the team scales, routing and ownership usually start breaking first",
        "anchor_terms": [
            "routing",
            "ownership",
            "handoff",
            "response",
            "sla",
            "lag",
            "follow up",
            "queue",
        ],
        "triggers": {
            "role_bucket": ["ops"],
            "lane": ["performance_marketing_agency", "b2b_saas", "generic"],
        },
        "deterministic": {
            "offer_scope": "handoff ownership flow",
            "followup_ping": "the lag usually shows up once ownership gets fuzzy between people",
        },
        "fallback_dynamic": {
            "signal_topic": "handoff lag",
            "personalization_line": "once more people touch the lead, clean ownership usually gets fuzzy fast",
            "who_i_help": "ops teams in growing agencies and SaaS companies",
            "problem_i_fix": "routing, ownership, and follow up leaks",
            "bottleneck_trigger": "more people start touching the lead and nobody owns the next step cleanly",
            "offer_observation": "there is probably some response lag hiding in the handoff logic",
            "offer_payoff": "handoffs stop leaking and follow up gets faster",
        },
        "proof_variants": [
            {
                "problem": "qualified leads were getting assigned late and nobody followed up inside the window they thought they had",
                "fix": "one handoff and escalation flow",
                "payoff": "cut response lag from 3 days to same day and kept more qualified conversations alive",
            },
            {
                "problem": "they had an sla on paper but not in the actual workflow",
                "fix": "one alert and owner routing system",
                "payoff": "saved about 5 hours a week on manual checking and got follow up under 24 hours",
            },
            {
                "problem": "round robin was landing replies with the wrong person once capacity shifted",
                "fix": "one owner load rule",
                "payoff": "cut reassignment drag by about 70 percent and kept responses tighter",
            },
            {
                "problem": "routing looked fine until replies started piling up",
                "fix": "one ownership and escalation flow",
                "payoff": "cleared a backlog of 14 stalled replies and saved about 4 hours a week in ops time",
            },
            {
                "problem": "sdr to ae handoff was breaking for any lead that replied outside business hours",
                "fix": "one timezone aware routing rule and owner queue",
                "payoff": "cut after hours response lag from 18 hours to 3 hours and kept about 12 more qualified conversations alive each month",
            },
            {
                "problem": "multi-touch replies were pinging 3 different owners and the ball got dropped",
                "fix": "one first touch owner logic",
                "payoff": "cut duplicate outreach by about 90 percent and saved about 6 hours a week on coordination",
            },
            {
                "problem": "weekend and holiday replies were sitting unrouted for 2 to 4 days",
                "fix": "one queue with auto acknowledgement and morning assignment",
                "payoff": "kept about 8 warm leads a month from going cold and got first response under 12 hours",
            },
        ],
    },
    "saas_followup_drag": {
        "implication": "when demo flow depends on manual follow up, speed to lead usually leaks first",
        "anchor_terms": [
            "demo",
            "speed to lead",
            "replies",
            "routing",
            "ownership",
            "follow up",
            "handoff",
            "queue",
        ],
        "triggers": {
            "role_bucket": ["founder", "growth", "other"],
            "lane": ["b2b_saas"],
        },
        "deterministic": {
            "offer_scope": "speed to lead flow",
            "followup_ping": "warm replies usually cool off once follow up still depends on memory",
        },
        "fallback_dynamic": {
            "signal_topic": "speed to lead",
            "personalization_line": "once reply volume picks up, speed to lead usually starts depending on memory",
            "who_i_help": "small and midsize SaaS teams",
            "problem_i_fix": "speed to lead and reply handling leaks",
            "bottleneck_trigger": "reply volume goes up and follow up stays manual",
            "offer_observation": "there are probably a few avoidable leaks between interest and rep follow up",
            "offer_payoff": "hot replies get handled faster",
        },
        "proof_variants": [
            {
                "problem": "warm demo replies were sitting in shared queues for 2 to 3 days",
                "fix": "one routing system",
                "payoff": "got them to the right owner same day and kept about 30 percent more demos alive",
            },
            {
                "problem": "the handoff from reply to rep was still manual and taking about 48 hours",
                "fix": "one reminder and routing flow",
                "payoff": "saved about 4 hours a week of rep time and cut follow up to under 6 hours",
            },
            {
                "problem": "speed to lead kept slipping when the team got busy",
                "fix": "one owner logic flow",
                "payoff": "got hot replies handled in under 4 hours and kept about 25 percent more demos live",
            },
            {
                "problem": "high intent replies were getting the same path as weak ones",
                "fix": "one route split",
                "payoff": "saved about 3 hours a week on manual triage and improved response speed by about 60 percent",
            },
            {
                "problem": "self serve signups with buying intent were getting generic onboarding emails instead of rep outreach",
                "fix": "one intent scoring rule and owner route",
                "payoff": "lifted demo booking from signups by about 35 percent and cut time to first touch to under 2 hours",
            },
            {
                "problem": "inbound replies were hitting a shared inbox and getting claimed on a first come basis, so leads went to whoever was online, not whoever fit",
                "fix": "one account mapping and round robin rule",
                "payoff": "cut lead misrouting by about 75 percent and recovered about 4 hours a week of rep time",
            },
            {
                "problem": "expansion replies from existing customers were being treated like new inbound and routed to sdrs",
                "fix": "one segmentation rule and owner override",
                "payoff": "got expansion conversations to csm owners same day and kept about 20 percent more upgrade paths live",
            },
        ],
    },
}
