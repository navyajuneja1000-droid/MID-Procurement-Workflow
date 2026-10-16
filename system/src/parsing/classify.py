"""
Turn a bank's wording into an outcome and a reason bucket.

Status cells are free text. The same decision arrives as "Approved",
"APPROVED", "Cleared" or "Approved - proceed", and a rejection as "Rejected",
"Declined" or "Not Approved". Reasons are whole sentences, never codes.

Two rules shape this module:

1. **"Not Approved" must not read as "Approved".** Negations are checked
   before anything else, because a substring match would silently stop the
   wrong clock and mark a declined merchant as won.
2. **Every classification carries a confidence.** A phrase match is strong, a
   single keyword is weaker, and nothing matching at all is an honest zero that
   sends the row to a human rather than guessing.

Keyword scoring is used here rather than a language model so the demo runs
offline and every decision can be explained. The tail it cannot place is
reported rather than hidden, and that tail is where a model would earn its
keep in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..model import EventType, RejectionReason

NEGATION = re.compile(r"\b(not|non|un|dis)[-\s]?(approved|cleared|accepted)\b", re.IGNORECASE)

OUTCOME_WORDS: dict[EventType, list[str]] = {
    EventType.ACQUIRER_REJECTED: ["reject", "declin", "not approved", "denied"],
    EventType.ACQUIRER_REQUESTED_DOCS: [
        "additional document",
        "addl docs",
        "additional docs",
        "docs awaited",
        "documents required",
        "docs reqd",
        "docs required",
    ],
    EventType.ACQUIRER_REQUESTED_CLARIFICATION: [
        "clarification",
        "clarif",
        "query raised",
        "need clarification",
    ],
    EventType.ACQUIRER_APPROVED: ["approved", "cleared", "proceed", "accepted"],
}

# Phrases that place a rejection in a bucket. Longer, more specific phrases
# score higher than bare keywords.
REASON_PHRASES: dict[RejectionReason, list[tuple[str, float]]] = {
    RejectionReason.RESTRICTED_CATEGORY: [
        ("restricted business category", 1.0),
        ("prohibited category", 1.0),
        ("risk appetite", 0.9),
        ("not permitted", 0.7),
        ("restricted", 0.5),
    ],
    RejectionReason.RESTRICTED_MCC: [
        ("negative list", 1.0),
        ("mcc not enabled", 1.0),
        ("mcc blocked", 1.0),
        ("do not re-raise", 0.6),
    ],
    RejectionReason.WEBSITE_NOT_LEGITIMATE: [
        ("does not appear genuine", 1.0),
        ("verify authenticity", 1.0),
        ("looks like a template", 1.0),
        ("no real business presence", 0.9),
        ("no company details", 0.8),
    ],
    RejectionReason.UNCLEAR_CUSTOMER_JOURNEY: [
        ("customer journey not clear", 1.0),
        ("checkout journey unclear", 1.0),
        ("how the end customer transacts", 0.9),
        ("journey", 0.4),
    ],
    RejectionReason.MULTIPLE_USE_CASES: [
        ("single mcc cannot be assigned", 1.0),
        ("multiple business lines", 1.0),
        ("several unrelated use cases", 1.0),
        ("split the use cases", 0.9),
    ],
    RejectionReason.PAYMENT_FLOW_BROKEN: [
        ("payment page not loading", 1.0),
        ("checkout flow throws an error", 1.0),
        ("test transaction", 0.8),
        ("payment flow incomplete", 1.0),
    ],
    RejectionReason.INCORRECT_MCC: [
        ("mcc does not match", 1.0),
        ("incorrect mcc", 1.0),
        ("mcc mismatch", 1.0),
        ("suggest reviewing", 0.5),
    ],
    RejectionReason.DOCUMENT_DISCREPANCY: [
        ("name on pan does not match", 1.0),
        ("discrepancy observed", 1.0),
        ("document mismatch", 1.0),
        ("revalidate", 0.6),
    ],
}


@dataclass
class Classification:
    outcome: EventType | None
    outcome_confidence: float
    reason: RejectionReason | None = None
    reason_confidence: float = 0.0
    why: str = ""


def classify_outcome(status_text: str) -> tuple[EventType | None, float, str]:
    """Read the bank's status wording. Negations are handled first, because a
    plain substring test would read 'Not Approved' as approved and stop the
    wrong clock."""
    text = (status_text or "").strip().lower()
    if not text:
        return None, 0.0, "status cell was empty"

    if NEGATION.search(text):
        return EventType.ACQUIRER_REJECTED, 0.95, "negated approval"

    # Most specific first: docs and clarification before the generic words,
    # since "Pending - docs awaited" also contains no approval word but is not
    # a rejection either.
    order = [
        EventType.ACQUIRER_REQUESTED_DOCS,
        EventType.ACQUIRER_REQUESTED_CLARIFICATION,
        EventType.ACQUIRER_REJECTED,
        EventType.ACQUIRER_APPROVED,
    ]
    for outcome in order:
        for word in OUTCOME_WORDS[outcome]:
            if word in text:
                # An exact single-word cell is a stronger signal than a word
                # buried in a longer sentence.
                confidence = 0.98 if text == word or text.rstrip(".") == word else 0.88
                return outcome, confidence, f"matched '{word}'"

    return None, 0.0, f"no known status wording in '{status_text[:40]}'"


def classify_reason(remark_text: str) -> tuple[RejectionReason | None, float, str]:
    """Place a free-text rejection remark in a bucket."""
    text = (remark_text or "").strip().lower()
    if not text:
        return None, 0.0, "no remark given"

    best: tuple[RejectionReason | None, float, str] = (None, 0.0, "")
    for reason, phrases in REASON_PHRASES.items():
        for phrase, weight in phrases:
            if phrase in text and weight > best[1]:
                best = (reason, weight, f"matched '{phrase}'")

    if best[0] is None:
        return RejectionReason.OTHER, 0.0, f"unrecognised remark: {remark_text[:50]}"
    return best


def classify(status_text: str, remark_text: str) -> Classification:
    outcome, oc, why = classify_outcome(status_text)
    c = Classification(outcome=outcome, outcome_confidence=oc, why=why)
    if outcome is EventType.ACQUIRER_REJECTED:
        c.reason, c.reason_confidence, reason_why = classify_reason(remark_text)
        c.why = f"{why}; {reason_why}"
    return c
