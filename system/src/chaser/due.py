"""
What is overdue, and whose court is it in.

Chasing is the cheapest intervention in this whole process and the easiest one
to do badly. Three ways it goes wrong, and this module exists to prevent all
three:

**Chasing the wrong party.** A request that is late because a bank asked us for
a document and we have not sent it is not the bank's fault. Chasing it anyway
is worse than silence: it spends credibility that is needed for the requests
that genuinely are late, and the reply is a screenshot of their own email. So
nothing is chaseable unless the ball is demonstrably in the acquirer's court,
derived from the event log rather than from a status field someone updates.

**Chasing on the wrong clock.** Lateness is judged on the round the bank is
holding right now, not on total elapsed time. A bank that answered twice
quickly and is now two days into a third round is two days late, not twelve.
Charging it for our own turnaround between its rounds is the same error the
SLA measurement avoids everywhere else, and a bank that receives a chase it can
disprove stops reading the next one.

**Chasing everything equally.** Ops has a finite amount of attention and a
finite amount of goodwill with each bank. A P0 merchant one day over matters
more than a P4 six days over, and a queue sorted by days overdue alone puts the
P4 first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..model import (
    Acquirer,
    EventType,
    PriorityTier,
    ProcurementRequest,
    RequestStatus,
)
from ..tat import STATUS, MID_DELIVERY, Episode, RequestTat
from ..model import Party


class Owes(str, Enum):
    """Who the request is currently waiting on."""

    ACQUIRER_STATUS = "acquirer_status"  # a decision on the application
    ACQUIRER_MID = "acquirer_mid"  # approved, but the MID has not arrived
    US = "us"  # they asked us something, or it never went out
    NOBODY = "nobody"  # procured, rejected or deprioritised


# The last event that moved the request tells us who holds it. Derived rather
# than stored, for the same reason every other figure here is: a status field
# is only as current as the last person who remembered to change it.
_OWES_BY_EVENT: dict[EventType, Owes] = {
    EventType.RAISED_TO_ACQUIRER: Owes.ACQUIRER_STATUS,
    EventType.RESPONDED_TO_ACQUIRER: Owes.ACQUIRER_STATUS,
    EventType.ACQUIRER_APPROVED: Owes.ACQUIRER_MID,
    EventType.ACQUIRER_REQUESTED_DOCS: Owes.US,
    EventType.ACQUIRER_REQUESTED_CLARIFICATION: Owes.US,
    EventType.BOUNCED_TO_BD: Owes.US,
    EventType.BOUNCED_TO_ONBOARDING: Owes.US,
    EventType.RETURNED_TO_OPS: Owes.US,
    EventType.ASSIGNED_TO_OPS: Owes.US,
    EventType.MID_RECEIVED: Owes.NOBODY,
    EventType.ACQUIRER_REJECTED: Owes.NOBODY,
    EventType.DEPRIORITISED: Owes.NOBODY,
}

# How much each tier multiplies a day of lateness. P0 merchants are the ones
# whose go-live date someone outside ops is already asking about.
# Steep enough that a P0 one day late outranks a P4 six days late, which is
# the call ops makes by hand anyway. Not so steep that a low tier can rot
# forever: a P4 twelve days over still comes above a P0 one day over, so the
# queue drains instead of starving its own tail.
TIER_WEIGHT: dict[PriorityTier, float] = {
    PriorityTier.P0: 5.0,
    PriorityTier.P1: 3.0,
    PriorityTier.P2: 1.8,
    PriorityTier.P3: 1.0,
    PriorityTier.P4: 0.5,
}


def owed_by(request: ProcurementRequest) -> Owes:
    """Who owes the next move on this request."""
    from ..model import ANNOTATION_EVENTS

    moving = [e for e in request.events if e.event_type not in ANNOTATION_EVENTS]
    if not moving:
        return Owes.US
    return _OWES_BY_EVENT[moving[-1].event_type]


@dataclass
class Overdue:
    """One request a bank is sitting on past the agreed response time."""

    request: ProcurementRequest
    acquirer: Acquirer
    owes: Owes
    days_held: int  # working days in the round they are holding *now*
    sla_days: int
    round_index: int  # which response round this is, 1-based

    @property
    def days_over(self) -> int:
        return self.days_held - self.sla_days

    @property
    def tier(self) -> PriorityTier:
        return self.request.merchant.priority_tier

    @property
    def score(self) -> float:
        """What decides the order of the queue.

        Days over, weighted by tier. Deliberately simple and stated in the
        report, because a ranking nobody can explain is one ops overrides by
        hand, and then the ranking is whatever they felt like that morning.
        """
        return self.days_over * TIER_WEIGHT[self.tier]

    @property
    def what_is_owed(self) -> str:
        return "a decision" if self.owes is Owes.ACQUIRER_STATUS else "the MID"


def _open_acquirer_episode(tat: RequestTat, phase: str) -> Episode | None:
    for ep in tat.episodes:
        if ep.party is Party.ACQUIRER and ep.phase == phase and ep.open_at_cutoff:
            return ep
    return None


def _round_index(tat: RequestTat, phase: str) -> int:
    return sum(
        1 for ep in tat.episodes if ep.party is Party.ACQUIRER and ep.phase == phase
    )


def find_overdue(
    request: ProcurementRequest,
    tat: RequestTat,
    acquirer: Acquirer,
    as_of: datetime,
) -> Overdue | None:
    """Is this request late, on the round the acquirer is holding right now?

    Returns None when the bank owes nothing, when we are the ones holding it,
    or when the current round is still inside the agreed time. Being quiet
    about the requests that are not late is most of the value: a chase list
    that includes everything is a list nobody reads.
    """
    if request.is_deprioritised or request.status in (
        RequestStatus.PROCURED,
        RequestStatus.REJECTED,
    ):
        return None

    owes = owed_by(request)
    if owes in (Owes.US, Owes.NOBODY):
        return None

    phase = STATUS if owes is Owes.ACQUIRER_STATUS else MID_DELIVERY
    sla = acquirer.status_sla_days if phase == STATUS else acquirer.mid_sla_days

    episode = _open_acquirer_episode(tat, phase)
    if episode is None:
        return None

    held = episode.working_days
    if held <= sla:
        return None

    return Overdue(
        request=request,
        acquirer=acquirer,
        owes=owes,
        days_held=held,
        sla_days=sla,
        round_index=_round_index(tat, phase),
    )


def rank(items: list[Overdue]) -> list[Overdue]:
    """Worst first, with ties broken by the oldest, so the order is stable
    between runs and a request cannot drift down the list by chance."""
    return sorted(
        items,
        key=lambda o: (-o.score, -o.days_over, o.request.request_id),
    )
