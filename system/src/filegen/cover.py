"""
Draft the covering email. A person sends it.

One deliberate checkpoint at the external boundary: everything up to here is
automatic, and the thing that leaves the company is read by a human first. The
cost of a wrong file reaching a bank is not a retry, it is a conversation with
the relationship manager and a batch that misses its slot.

What the draft is for is as much internal as external. It states the batch, the
count, and the conditions attached to particular merchants, so the reply can be
matched back to what was sent and so a conditional approval is not read later
as an unconditional one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..catalogue.eligibility import EligibilityResult, Verdict
from .fill import FilledFile
from .preflight import Preflight


@dataclass
class Draft:
    to: str
    subject: str
    body: str
    attachment: str


def draft(
    f: FilledFile,
    check: Preflight,
    attachment: str,
    lot_no: str,
    on: date,
    eligibility: dict[str, EligibilityResult],
    acquirer_name: str,
    acquirer_email: str,
) -> Draft:
    n = len(f.request_ids)
    subject = (
        f"MID Request | Lot {lot_no} | {n} application{'' if n == 1 else 's'} | "
        f"{on.strftime('%d-%b-%Y')}"
    )

    lines = [
        "Dear Team,",
        "",
        f"Please find attached {n} MID application{'' if n == 1 else 's'} for "
        f"processing, submitted as lot {lot_no}.",
        "",
    ]

    conditional = [
        (rid, e)
        for rid, e in eligibility.items()
        if rid in set(f.request_ids) and e.verdict is Verdict.CONDITIONAL
    ]
    if conditional:
        # Stated in the covering note rather than left in the file, because a
        # condition buried in a column is read by nobody and a merchant goes
        # live on a rail the policy does not allow.
        lines += [
            "The following are submitted against a conditional category and the "
            "condition is noted so the approval is not read as unconditional:",
            "",
        ]
        by_name = {r.request_id: r.merchant_name for r in f.rows}
        for rid, e in sorted(conditional, key=lambda kv: by_name.get(kv[0], kv[0])):
            detail = e.condition or e.reason
            lines.append(f"  - {by_name.get(rid, rid)}: {detail}")
        lines.append("")

    lines += [
        "Supporting documents for each entity are available on request.",
        "",
        "Request: confirmation of receipt today, and the decision or query by "
        "your standard turnaround so we can keep the merchants informed.",
        "",
        "Regards,",
        "MID Operations",
    ]

    if check.warnings:
        # Not part of the email. It sits with the draft so the reviewer sees it
        # at the moment they are deciding whether to send.
        lines += [
            "",
            "-- internal note, delete before sending --",
            *(f"  {w.message}" for w in check.warnings),
        ]

    return Draft(
        to=acquirer_email,
        subject=subject,
        body="\n".join(lines),
        attachment=attachment,
    )
