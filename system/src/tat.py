"""
The TAT engine: turns an event history into held-time figures per party.

Rules encoded here, all from how the process is actually measured:

- TAT is per request (merchant + MCC + acquirer), never one figure per merchant.
- Working days only, on a per-acquirer holiday calendar.
- The acquirer's clock starts next working day if raised after the 2pm cut-off.
- Clocks overlap in two places:
    ops runs alongside the acquirer, because getting the MID is ops' job;
    onboarding runs alongside BD, because onboarding owns the ticket until it
    comes back clean.
- Ops time is reported per episode, not merged. A request ops touches twice is
  two entries, and the episode count is itself a measure of rework.
- Ops time is split into solo time and time overlapping the acquirer, so ops
  efficiency can be read without the bank's queue inside it.
- Deprioritisation stops every clock.

Because clocks overlap, the per-party days sum to more than elapsed time. Any
"share of delay" pie chart built on these figures would be wrong. The honest
visual is a timeline per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .calendar_rules import WorkingCalendar
from .model import EventType, Party, ProcurementRequest

# Ops episode phases
SOLO = "solo"
WITH_ACQUIRER = "with_acquirer"

# Acquirer episode phases
STATUS = "status"  # raised -> status response, measured against status SLA
MID_DELIVERY = "mid_delivery"  # approved -> MID received, against MID SLA


@dataclass
class Episode:
    """One continuous stretch of a party holding a request.

    `touch` groups episodes that belong to the same pickup. An ops touch is
    split into a solo stretch and an overlap stretch when the request is
    raised, but that is one touch, not two: ops never put it down. Counting
    phase splits as episodes would overstate rework.
    """

    party: Party
    phase: str
    start: datetime
    end: datetime | None = None
    working_days: int = 0
    open_at_cutoff: bool = False
    touch: int = 1

    @property
    def is_open(self) -> bool:
        return self.end is None


@dataclass
class RequestTat:
    """Everything derived for one request. Nothing here is stored anywhere;
    it is all recomputed from events."""

    request_id: str
    merchant_name: str
    mcc: str
    acquirer_code: str
    episodes: list[Episode] = field(default_factory=list)
    deprioritised: bool = False

    def _of(self, party: Party, phase: str | None = None) -> list[Episode]:
        return [
            e
            for e in self.episodes
            if e.party is party and (phase is None or e.phase == phase)
        ]

    def _days(self, party: Party, phase: str | None = None) -> int:
        return sum(e.working_days for e in self._of(party, phase))

    # Ops -------------------------------------------------------------
    @property
    def ops_episodes(self) -> list[Episode]:
        return self._of(Party.OPS)

    @property
    def ops_episode_count(self) -> int:
        """How many separate times ops picked this request up. Anything above
        one means the request came back to ops after sitting elsewhere."""
        return len({e.touch for e in self.ops_episodes})

    @property
    def ops_days_by_episode(self) -> list[int]:
        """Days per pickup, in order. Reported as separate entries rather than
        one merged figure, so a bounced request shows its rework."""
        by_touch: dict[int, int] = {}
        for e in self.ops_episodes:
            by_touch[e.touch] = by_touch.get(e.touch, 0) + e.working_days
        return [by_touch[t] for t in sorted(by_touch)]

    @property
    def ops_solo_days(self) -> int:
        """Time ops held the request with no acquirer clock running. This is
        the figure ops efficiency should be judged on."""
        return self._days(Party.OPS, SOLO)

    @property
    def ops_overlap_days(self) -> int:
        """Time ops held the request while waiting on the acquirer. Ops is
        accountable for it but does not control it."""
        return self._days(Party.OPS, WITH_ACQUIRER)

    @property
    def ops_total_days(self) -> int:
        return self.ops_solo_days + self.ops_overlap_days

    # Other internal parties -----------------------------------------
    @property
    def bd_days(self) -> int:
        return self._days(Party.BD)

    @property
    def onboarding_days(self) -> int:
        return self._days(Party.ONBOARDING)

    @property
    def bd_episode_count(self) -> int:
        return len(self._of(Party.BD))

    @property
    def onboarding_episode_count(self) -> int:
        return len(self._of(Party.ONBOARDING))

    # Acquirer --------------------------------------------------------
    @property
    def acquirer_status_days(self) -> int:
        return self._days(Party.ACQUIRER, STATUS)

    @property
    def acquirer_mid_days(self) -> int:
        """Approval to MID in hand. Invisible in the current tracker, because
        approval and MID delivery arrive on different email threads."""
        return self._days(Party.ACQUIRER, MID_DELIVERY)

    @property
    def acquirer_total_days(self) -> int:
        return self.acquirer_status_days + self.acquirer_mid_days

    @property
    def acquirer_worst_status_round(self) -> int:
        """The longest single stretch the acquirer held it awaiting a status
        response.

        An acquirer that answers in 4 days, asks for a document, then answers
        again in 4 days has met a 5-day SLA twice over. Judging it on the
        8-day sum would charge it for our own turnaround between the rounds
        and report a breach where there was none.
        """
        return max((e.working_days for e in self._of(Party.ACQUIRER, STATUS)), default=0)

    @property
    def acquirer_status_rounds(self) -> int:
        """How many times the acquirer came back before deciding. More than
        one means the application was not complete enough to decide in a
        single pass, which is a cost of our own data quality, not theirs."""
        return len(self._of(Party.ACQUIRER, STATUS))

    # Consolidated ----------------------------------------------------
    @property
    def consolidated_days(self) -> int:
        """Elapsed working days from first to last activity. Not the sum of
        party days, which double-counts the overlaps by design."""
        if not self.episodes:
            return 0
        starts = [e.start for e in self.episodes]
        ends = [e.end for e in self.episodes if e.end is not None]
        if not ends:
            return 0
        return _span_days(min(starts), max(ends), self._calendar, self._calendar_name)

    def sla_breach(self, status_sla: int, mid_sla: int) -> dict[str, bool]:
        """Judged per response round, not on total held time. The agreed SLA
        is a promise about how fast they answer, and our own turnaround
        between their rounds is not theirs to carry."""
        return {
            "status": self.acquirer_worst_status_round > status_sla,
            "mid_delivery": self.acquirer_mid_days > mid_sla,
        }

    # Set by the engine so consolidated_days can count correctly
    _calendar: WorkingCalendar | None = None
    _calendar_name: str = "default"


def _span_days(
    start: datetime, end: datetime, cal: WorkingCalendar | None, name: str
) -> int:
    if cal is None:
        return 0
    return cal.working_days_between(start.date(), end.date(), name)


class TatEngine:
    """Replays an event history into episodes.

    The cut-off applies to the acquirer's clock only. That is the convention
    agreed with the banks: a file sent after 2pm is taken up the next working
    day. Internal clocks start the moment the handoff happens.
    """

    def __init__(self, calendar: WorkingCalendar) -> None:
        self.calendar = calendar

    def compute(
        self, request: ProcurementRequest, as_of: datetime | None = None
    ) -> RequestTat:
        result = RequestTat(
            request_id=request.request_id,
            merchant_name=request.merchant.legal_name,
            mcc=request.mcc,
            acquirer_code=request.acquirer.code,
            deprioritised=request.is_deprioritised,
        )
        result._calendar = self.calendar
        result._calendar_name = request.acquirer.holiday_calendar

        open_episodes: dict[tuple[Party, str], Episode] = {}
        touch: dict[Party, int] = {p: 0 for p in Party}

        def start(party: Party, phase: str, at: datetime) -> None:
            key = (party, phase)
            if key in open_episodes:
                return  # already running; a duplicate event must not restart it
            open_episodes[key] = Episode(
                party=party, phase=phase, start=at, touch=touch[party]
            )

        def pickup(party: Party) -> None:
            """A genuine new hold, as opposed to a phase split within one."""
            touch[party] += 1

        def stop(party: Party, at: datetime, phase: str | None = None) -> None:
            for key in [
                k
                for k in open_episodes
                if k[0] is party and (phase is None or k[1] == phase)
            ]:
                ep = open_episodes.pop(key)
                ep.end = at
                ep.working_days = self._episode_days(ep, request)
                result.episodes.append(ep)

        def stop_all(at: datetime) -> None:
            for party in list(Party):
                stop(party, at)

        acquirer_running = False

        for event in request.events:
            at = event.at
            et = event.event_type

            if et is EventType.ASSIGNED_TO_OPS:
                stop(Party.ONBOARDING, at)
                pickup(Party.OPS)
                start(Party.OPS, SOLO, at)

            elif et is EventType.BOUNCED_TO_BD:
                stop(Party.OPS, at)
                pickup(Party.BD)
                pickup(Party.ONBOARDING)
                start(Party.BD, "", at)
                start(Party.ONBOARDING, "", at)  # onboarding owns it until clean

            elif et is EventType.BOUNCED_TO_ONBOARDING:
                stop(Party.OPS, at)
                pickup(Party.ONBOARDING)
                start(Party.ONBOARDING, "", at)

            elif et is EventType.RETURNED_TO_OPS:
                stop(Party.BD, at)
                stop(Party.ONBOARDING, at)
                pickup(Party.OPS)
                start(Party.OPS, WITH_ACQUIRER if acquirer_running else SOLO, at)

            elif et is EventType.RAISED_TO_ACQUIRER:
                # Ops keeps running, but from here it is overlap time, not solo.
                # Same touch: ops never put the request down.
                stop(Party.OPS, at)
                start(Party.OPS, WITH_ACQUIRER, at)
                pickup(Party.ACQUIRER)
                start(Party.ACQUIRER, STATUS, at)
                acquirer_running = True

            elif et in (
                EventType.ACQUIRER_REQUESTED_DOCS,
                EventType.ACQUIRER_REQUESTED_CLARIFICATION,
            ):
                stop(Party.ACQUIRER, at)
                stop(Party.OPS, at)
                acquirer_running = False
                pickup(Party.BD)
                pickup(Party.ONBOARDING)
                start(Party.BD, "", at)
                start(Party.ONBOARDING, "", at)

            elif et is EventType.RESPONDED_TO_ACQUIRER:
                stop(Party.BD, at)
                stop(Party.ONBOARDING, at)
                pickup(Party.ACQUIRER)
                start(Party.ACQUIRER, STATUS, at)
                pickup(Party.OPS)
                start(Party.OPS, WITH_ACQUIRER, at)
                acquirer_running = True

            elif et is EventType.ACQUIRER_APPROVED:
                # Status phase ends; MID delivery phase begins. Ops keeps
                # running, because procurement is not done until the MID lands.
                stop(Party.ACQUIRER, at, phase=STATUS)
                start(Party.ACQUIRER, MID_DELIVERY, at)

            elif et is EventType.MID_RECEIVED:
                stop_all(at)
                acquirer_running = False

            elif et in (EventType.ACQUIRER_REJECTED, EventType.DEPRIORITISED):
                stop_all(at)
                acquirer_running = False

        # Anything still open is held right now. Close it at as_of so live
        # requests show their current age rather than nothing.
        if open_episodes:
            cutoff = as_of or datetime.now()
            for key in list(open_episodes):
                ep = open_episodes.pop(key)
                ep.end = cutoff
                ep.open_at_cutoff = True
                ep.working_days = self._episode_days(ep, request)
                result.episodes.append(ep)

        result.episodes.sort(key=lambda e: (e.start, e.party.value, e.phase))
        return result

    def _episode_days(self, ep: Episode, request: ProcurementRequest) -> int:
        assert ep.end is not None
        if ep.party is Party.ACQUIRER:
            cal_name = request.acquirer.holiday_calendar
            start_day = self.calendar.effective_start(ep.start, cal_name)
        else:
            cal_name = "default"
            start_day = ep.start.date()

        end_day = ep.end.date()
        if end_day < start_day:
            # Raised after cut-off and answered the same calendar day: the
            # acquirer's clock had not started yet.
            return 0
        return self.calendar.working_days_between(start_day, end_day, cal_name)
