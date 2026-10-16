"""
Bank reply emails, rendered the way acquirers actually send them.

Three things make these hard to parse, and all three are deliberate here:

1. **Every acquirer uses its own shape.** Some paste a table into the email
   body, some attach their own sheet. Column names differ. Status wording is
   free text, not a fixed list.
2. **Replies are partial.** A batch of 40 comes back in dribs and drabs, so a
   reply covers some rows and not others.
3. **Names drift.** The bank types the legal name its own way, so matching is
   on legal name plus MCC with a tolerance, not an exact string.

Status replies and MID delivery arrive on different subjects. That separation
is why approval-to-MID time is invisible in the current tracker.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from .model import EventType, ProcurementRequest, RejectionReason
from .names import SUFFIX_VARIANTS

# How each acquirer formats its reply.
REPLY_STYLE: dict[str, str] = {
    "ALDRIDGE": "body_table",
    "BRINDLE": "attached_csv",
    "CASTERLY": "body_table",
    "NORTHWAY_CYBS": "attached_csv",
    "NORTHWAY_FSS": "attached_csv",
    "VERRAN": "body_list",
}

# Each acquirer's own column headings. No two agree.
COLUMNS: dict[str, dict[str, str]] = {
    "ALDRIDGE": {"name": "Merchant Name", "mcc": "MCC", "status": "Status", "remarks": "Remarks"},
    "BRINDLE": {"name": "LEGAL NAME", "mcc": "MCC CODE", "status": "APPLICATION STATUS", "remarks": "COMMENTS"},
    "CASTERLY": {"name": "Entity", "mcc": "Mcc", "status": "Decision", "remarks": "Reason"},
    "NORTHWAY_CYBS": {"name": "Merchant", "mcc": "MCC", "status": "Status", "remarks": "Observation"},
    "NORTHWAY_FSS": {"name": "Merchant", "mcc": "MCC", "status": "Status", "remarks": "Observation"},
    "VERRAN": {"name": "Merchant Legal Name", "mcc": "MCC", "status": "Current Status", "remarks": "Note"},
}

# The same outcome, worded differently by each bank and sometimes within one.
APPROVED_WORDS = ["Approved", "APPROVED", "MID Approved", "Approved - proceed", "Cleared"]
REJECTED_WORDS = ["Rejected", "REJECTED", "Declined", "Not Approved", "Rejected by risk"]
DOCS_WORDS = [
    "Additional documents required",
    "ADDL DOCS REQUIRED",
    "Pending - docs awaited",
    "Additional docs reqd",
]
CLARIFICATION_WORDS = [
    "Clarification required",
    "Query raised",
    "CLARIFICATION REQD",
    "Need clarification",
]

# Free-text rejection remarks, which a classifier has to bucket. Several are
# ambiguous on purpose: that is what the confidence score is for.
REJECTION_TEXT: dict[RejectionReason, list[str]] = {
    RejectionReason.RESTRICTED_CATEGORY: [
        "Merchant falls under restricted business category as per our internal policy",
        "Prohibited category - cannot be onboarded",
        "Business model not permitted under current risk appetite",
    ],
    RejectionReason.RESTRICTED_MCC: [
        "MCC not enabled for this acquirer",
        "Requested MCC is in the negative list",
        "MCC blocked - please do not re-raise",
    ],
    RejectionReason.WEBSITE_NOT_LEGITIMATE: [
        "Website does not appear genuine, no company details visible",
        "Site looks like a template, no real business presence found",
        "Unable to verify authenticity of the website",
    ],
    RejectionReason.UNCLEAR_CUSTOMER_JOURNEY: [
        "Customer journey not clear from the website",
        "Could not establish how the end customer transacts",
        "Checkout journey unclear, please share a demo",
    ],
    RejectionReason.MULTIPLE_USE_CASES: [
        "Multiple business lines on one site, single MCC cannot be assigned",
        "Site covers several unrelated use cases",
        "Please split the use cases and re-apply separately",
    ],
    RejectionReason.PAYMENT_FLOW_BROKEN: [
        "Payment page not loading at our end",
        "Checkout flow throws an error, could not complete test transaction",
        "Payment flow incomplete on the website",
    ],
    RejectionReason.INCORRECT_MCC: [
        "MCC does not match the business as seen on the website",
        "Incorrect MCC assigned, suggest reviewing",
        "MCC mismatch with actual line of business",
    ],
    RejectionReason.DOCUMENT_DISCREPANCY: [
        "Name on PAN does not match the entity name provided",
        "Discrepancy observed between GST and address proof",
        "Document mismatch, please revalidate",
    ],
}

DOCS_TEXT = [
    "Please share authorised signatory PAN and address proof",
    "Board resolution awaited",
    "Need latest GST certificate",
    "Signatory KYC pending",
    "Please share cancelled cheque and GST copy",
]

CLARIFICATION_TEXT = [
    "Please confirm the delivery timeline mentioned on the site",
    "Confirm whether the merchant sells to end consumers or businesses",
    "Need clarity on the refund window stated in the policy",
    "Please confirm the settlement account belongs to the same entity",
]


@dataclass
class ReplyEmail:
    """One bank reply, as it would land in the inbox."""

    acquirer_code: str
    subject: str
    sent_at: datetime
    body: str
    attachment_name: str | None = None
    attachment_csv: str | None = None
    # Kept only so tests and the parser's accuracy can be scored later. A real
    # inbox would not carry this.
    covers_request_ids: list[str] = None  # type: ignore[assignment]


def _drift_name(rng: random.Random, name: str) -> str:
    """Type the legal name the way a bank clerk might: a different suffix
    form, dropped punctuation, upper case, or a trailing space."""
    out = name
    for canonical, variants in SUFFIX_VARIANTS.items():
        if out.endswith(canonical) and rng.random() < 0.45:
            out = out[: -len(canonical)] + rng.choice(variants)
            break
    roll = rng.random()
    if roll < 0.12:
        out = out.upper()
    elif roll < 0.18:
        out = out.replace(".", "")
    if rng.random() < 0.07:
        out = out + " "
    return out


def _status_cell(rng: random.Random, event_type: EventType) -> str:
    if event_type is EventType.ACQUIRER_APPROVED:
        return rng.choice(APPROVED_WORDS)
    if event_type is EventType.ACQUIRER_REJECTED:
        return rng.choice(REJECTED_WORDS)
    if event_type is EventType.ACQUIRER_REQUESTED_DOCS:
        return rng.choice(DOCS_WORDS)
    return rng.choice(CLARIFICATION_WORDS)


def _remark_cell(
    rng: random.Random, event_type: EventType, reason: RejectionReason | None
) -> str:
    if event_type is EventType.ACQUIRER_REJECTED:
        pool = REJECTION_TEXT.get(reason or RejectionReason.OTHER, ["Rejected"])
        return rng.choice(pool)
    if event_type is EventType.ACQUIRER_REQUESTED_DOCS:
        return rng.choice(DOCS_TEXT)
    if event_type is EventType.ACQUIRER_REQUESTED_CLARIFICATION:
        return rng.choice(CLARIFICATION_TEXT)
    return ""


@dataclass
class _Row:
    request_id: str
    name: str
    mcc: str
    status: str
    remarks: str


# Wordings outside the usual vocabulary. Real inboxes contain these, and a
# parser that has never met one has an untested review path.
UNKNOWN_STATUS = [
    "Under process at our end",
    "With committee",
    "Referred to risk",
    "Held",
    "In progress",
]


def _mangle_name(rng: random.Random, name: str) -> str:
    """Damage a name past the point where a fuzzy match should be trusted:
    truncated mid-word, or with the distinctive first word missing."""
    if rng.random() < 0.5:
        cut = max(8, int(len(name) * rng.uniform(0.35, 0.55)))
        return name[:cut].strip()
    parts = name.split()
    return " ".join(parts[1:]) if len(parts) > 2 else name[:10]


def _rows_for(
    rng: random.Random, batch: list[tuple[ProcurementRequest, EventType, RejectionReason | None]]
) -> list[_Row]:
    rows = []
    for request, event_type, reason in batch:
        name = _drift_name(rng, request.merchant.legal_name)
        status = _status_cell(rng, event_type)

        # A small share of rows are genuinely beyond a rule-based parser. They
        # exist so the review queue is exercised rather than assumed to work.
        roll = rng.random()
        if roll < 0.012:
            status = rng.choice(UNKNOWN_STATUS)
        elif roll < 0.022:
            name = _mangle_name(rng, name)

        rows.append(
            _Row(
                request_id=request.request_id,
                name=name,
                mcc=request.mcc,
                status=status,
                remarks=_remark_cell(rng, event_type, reason),
            )
        )
    return rows


def _body_table(code: str, rows: list[_Row], when: datetime) -> str:
    c = COLUMNS[code]
    widths = {
        "name": max(len(c["name"]), max((len(r.name) for r in rows), default=10)),
        "status": max(len(c["status"]), max((len(r.status) for r in rows), default=8)),
    }
    header = (
        f"{c['name']:<{widths['name']}} | {c['mcc']:<5} | "
        f"{c['status']:<{widths['status']}} | {c['remarks']}"
    )
    lines = [
        "Dear Team,",
        "",
        f"Please find below the status of the MID applications received up to "
        f"{when:%d-%b-%Y}.",
        "",
        header,
        "-" * len(header),
    ]
    for r in rows:
        lines.append(
            f"{r.name:<{widths['name']}} | {r.mcc:<5} | "
            f"{r.status:<{widths['status']}} | {r.remarks}"
        )
    lines += ["", "Remaining cases are under review and will follow.", "", "Regards,"]
    return "\n".join(lines)


def _body_list(code: str, rows: list[_Row], when: datetime) -> str:
    lines = [
        "Hi,",
        "",
        f"Status update as on {when:%d/%m/%Y}:",
        "",
    ]
    for i, r in enumerate(rows, start=1):
        line = f"{i}. {r.name} (MCC {r.mcc}) - {r.status}"
        if r.remarks:
            line += f". {r.remarks}"
        lines.append(line)
    lines += ["", "Balance cases pending at our end.", "", "Thanks"]
    return "\n".join(lines)


def _attached_csv(code: str, rows: list[_Row]) -> str:
    c = COLUMNS[code]
    out = [f"{c['name']},{c['mcc']},{c['status']},{c['remarks']}"]
    for r in rows:
        remarks = r.remarks.replace(",", ";")
        name = r.name.replace(",", "")
        out.append(f"{name},{r.mcc},{r.status},{remarks}")
    return "\n".join(out)


def build_status_reply(
    rng: random.Random,
    acquirer_code: str,
    batch: list[tuple[ProcurementRequest, EventType, RejectionReason | None]],
    when: datetime,
) -> ReplyEmail:
    """One status email covering part of a batch."""
    rows = _rows_for(rng, batch)
    style = REPLY_STYLE[acquirer_code]
    subject = f"RE: MID Request | {acquirer_code} | Status Tracker"

    if style == "attached_csv":
        body = (
            "Dear Team,\n\nPlease refer to the attached tracker for the latest "
            "status.\n\nRegards,"
        )
        return ReplyEmail(
            acquirer_code=acquirer_code,
            subject=subject,
            sent_at=when,
            body=body,
            attachment_name=f"{acquirer_code}_status_{when:%d%m%Y}.csv",
            attachment_csv=_attached_csv(acquirer_code, rows),
            covers_request_ids=[r.request_id for r in rows],
        )

    renderer = _body_table if style == "body_table" else _body_list
    return ReplyEmail(
        acquirer_code=acquirer_code,
        subject=subject,
        sent_at=when,
        body=renderer(acquirer_code, rows, when),
        covers_request_ids=[r.request_id for r in rows],
    )


def build_mid_delivery(
    rng: random.Random,
    acquirer_code: str,
    requests: list[ProcurementRequest],
    when: datetime,
) -> ReplyEmail:
    """MIDs arrive on a separate subject, with credentials that differ by
    acquirer. This is the second inbound stream, and the reason approval and
    MID receipt are two distinct events."""
    lines = ["Dear Team,", "", "Please find the credentials for the approved cases.", ""]

    for r in requests:
        mid = f"{acquirer_code[:3]}{rng.randint(10**9, 10**10 - 1)}"
        lines.append(f"Merchant : {_drift_name(rng, r.merchant.legal_name)}")
        lines.append(f"MCC      : {r.mcc}")
        lines.append(f"MID      : {mid}")
        if acquirer_code.startswith("NORTHWAY"):
            lines.append(f"TID      : {rng.randint(10**7, 10**8 - 1)}")
            lines.append(f"Access Key  : {rng.getrandbits(64):016x}")
            lines.append(f"Secret Key  : {rng.getrandbits(96):024x}")
        elif acquirer_code == "BRINDLE":
            lines.append(f"API Key     : brk_{rng.getrandbits(72):018x}")
            lines.append(f"Salt        : {rng.getrandbits(48):012x}")
        else:
            lines.append(f"Encryption Key : {rng.getrandbits(80):020x}")
            lines.append(f"Working Key    : {rng.getrandbits(64):016x}")
        lines.append("")

    lines += ["Please confirm once configured.", "", "Regards,"]

    return ReplyEmail(
        acquirer_code=acquirer_code,
        subject=f"MID Credentials | {acquirer_code} | {when:%d-%b-%Y}",
        sent_at=when,
        body="\n".join(lines),
        covers_request_ids=[r.request_id for r in requests],
    )
