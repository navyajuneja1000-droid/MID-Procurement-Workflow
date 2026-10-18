"""
One chase email per acquirer, per day. A person sends it.

Consolidated for the same reason bounces are consolidated per party: a bank
that gets eleven separate emails about eleven merchants on one morning has
received one piece of information and eleven interruptions, and will answer
them in whatever order they happen to open them. One email with eleven lines
can be worked down the list.

What goes in it is chosen to be checkable. Every line carries the date the
application went in, the round the bank is on, how long it has held this round
and what was agreed, because the only chase a bank cannot argue with is one
that quotes its own commitment back with a date attached. Vague urgency gets a
vague answer.

Nothing here sends. The draft lands in a folder and a person decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..model import EventType, ProcurementRequest
from .due import Overdue
from .ladder import Stage

OPENING = {
    Stage.FIRST: (
        "Following up on the applications below, which are past the response "
        "time we work to. Could you confirm where each one stands."
    ),
    Stage.SECOND: (
        "Following up again on the applications below. These were raised with "
        "you earlier and are still open past the agreed response time. The "
        "merchants have go-live dates against them, so a date for each would "
        "help even if the decision is not ready."
    ),
}


@dataclass
class ChaseDraft:
    acquirer_code: str
    acquirer_name: str
    to: str
    subject: str
    body: str
    request_ids: list[str]
    stage: Stage


def _raised_on(request: ProcurementRequest) -> date | None:
    for e in request.events:
        if e.event_type is EventType.RAISED_TO_ACQUIRER:
            return e.at.date()
    return None


def _line(item: Overdue) -> str:
    raised = _raised_on(item.request)
    when = raised.strftime("%d-%b-%Y") if raised else "date on file"
    round_note = (
        "" if item.round_index <= 1 else f", round {item.round_index} with you"
    )
    return (
        f"  - {item.request.merchant.legal_name} · MCC {item.request.mcc} · "
        f"raised {when}{round_note}\n"
        f"    Waiting on {item.what_is_owed}. {item.days_held} working days on this "
        f"round against {item.sla_days} agreed."
    )


def draft_for_acquirer(
    items: list[Overdue],
    stage: Stage,
    on: date,
    acquirer_email: str,
) -> ChaseDraft:
    """Build one email covering everything overdue at one acquirer."""
    acquirer = items[0].acquirer
    n = len(items)

    worst = max(items, key=lambda i: i.days_over)
    subject = (
        f"Open MID applications | {acquirer.code} | "
        f"{n} pending, oldest {worst.days_held} working days"
    )
    if stage is Stage.SECOND:
        subject = "Second follow-up | " + subject

    lines = ["Dear Team,", "", OPENING[stage], ""]
    lines += [_line(i) for i in items]
    lines += [
        "",
        "Happy to re-send any supporting document that would help. If anything "
        "below is waiting on us rather than on you, tell us and we will turn it "
        "around today.",
        "",
        "Regards,",
        "MID Operations",
    ]

    return ChaseDraft(
        acquirer_code=acquirer.code,
        acquirer_name=acquirer.name,
        to=acquirer_email,
        subject=subject,
        body="\n".join(lines),
        request_ids=[i.request.request_id for i in items],
        stage=stage,
    )


def escalation_note(items: list[Overdue]) -> str:
    """What goes to a person instead of to the bank.

    These have been chased twice and have not moved. Sending a third email is
    the thing this ladder exists to prevent, so the output here is a short
    internal note naming what to raise on the next relationship call.
    """
    lines = [
        "ESCALATE: chased twice, still open",
        "",
        "These are past the point where another email helps. Raise them on the "
        "next call with the relationship manager, with the dates below.",
        "",
    ]
    for item in sorted(items, key=lambda i: -i.days_over):
        raised = _raised_on(item.request)
        when = raised.strftime("%d-%b-%Y") if raised else "date on file"
        lines.append(
            f"  {item.acquirer.name:<34}{item.request.merchant.legal_name}\n"
            f"  {'':<34}raised {when}, {item.days_held} working days on round "
            f"{item.round_index} against {item.sla_days} agreed"
        )
    return "\n".join(lines)
