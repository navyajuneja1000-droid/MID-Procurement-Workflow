"""
These tests are the TAT rules written as executable checks. If a rule is ever
disputed, this file is the answer: it says exactly what the system counts and
why.
"""

from datetime import datetime

import pytest

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import WorkingCalendar, load_calendar
from src.model import (
    BOUNCE_ROUTING,
    BounceReason,
    Event,
    EventType,
    Merchant,
    Party,
    PriorityTier,
    ProcurementRequest,
    RejectionReason,
    RequestStatus,
    Segment,
)
from src.tat import MID_DELIVERY, SOLO, STATUS, WITH_ACQUIRER, TatEngine


@pytest.fixture
def cal() -> WorkingCalendar:
    return load_calendar(HOLIDAYS)


@pytest.fixture
def engine(cal: WorkingCalendar) -> TatEngine:
    return TatEngine(cal)


def make_request(acquirer: str = "ALDRIDGE", mcc: str = "5399") -> ProcurementRequest:
    merchant = Merchant(
        merchant_id="M001",
        legal_name="Harrowgate Trading Private Limited",
        segment=Segment.PREMIUM_SME,
        priority_tier=PriorityTier.P2,
        website="https://example-merchant.test",
    )
    return ProcurementRequest(
        request_id=f"REQ-{acquirer}-{mcc}",
        merchant=merchant,
        mcc=mcc,
        acquirer=ACQUIRERS[acquirer],
    )


# --- Calendar and cut-off ------------------------------------------------


def test_weekend_is_not_a_working_day(cal):
    assert not cal.is_working_day(datetime(2026, 9, 19).date())  # Saturday
    assert cal.is_working_day(datetime(2026, 9, 18).date())  # Friday


def test_before_cutoff_starts_same_day(cal):
    at = datetime(2026, 9, 18, 9, 30)  # Friday 9:30am
    assert cal.effective_start(at) == datetime(2026, 9, 18).date()


def test_after_cutoff_starts_next_working_day(cal):
    at = datetime(2026, 9, 18, 16, 45)  # Friday 4:45pm
    assert cal.effective_start(at) == datetime(2026, 9, 21).date()  # Monday


def test_exactly_at_cutoff_counts_as_same_day(cal):
    at = datetime(2026, 9, 18, 14, 0)
    assert cal.effective_start(at) == datetime(2026, 9, 18).date()


def test_acquirer_holiday_override(cal):
    # 1 Apr 2026 is a working day by default but a holiday for Verran.
    d = datetime(2026, 4, 1).date()
    assert cal.is_working_day(d, "default")
    assert not cal.is_working_day(d, "verran")


def test_same_day_handoff_counts_as_one_day(cal):
    d = datetime(2026, 9, 18).date()
    assert cal.working_days_between(d, d) == 1


def test_working_days_skip_the_weekend(cal):
    # Friday 18th to Monday 21st is one working day held (Friday).
    start = datetime(2026, 9, 18).date()
    end = datetime(2026, 9, 21).date()
    assert cal.working_days_between(start, end) == 1


# --- Ops episodes --------------------------------------------------------


def test_ops_bounce_produces_two_episodes_not_one_merged_figure(engine):
    """The rule: ops is measured per episode. A request ops touches twice is
    reported as two entries, and the waiting days in between are not ops'."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))  # Mon
    r.add(
        Event(
            EventType.BOUNCED_TO_BD,
            datetime(2026, 9, 15, 11, 0),  # Tue
            bounce_reason=BounceReason.MISSING_SIGNATORY_PAN,
        )
    )
    r.add(Event(EventType.RETURNED_TO_OPS, datetime(2026, 9, 21, 10, 0)))  # Mon
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 22, 9, 0)))  # Tue

    tat = engine.compute(r, as_of=datetime(2026, 9, 22, 18, 0))

    # Two genuine pickups: Mon 14th, then again Mon 21st after the fix came in.
    assert tat.ops_episode_count == 2
    assert tat.ops_days_by_episode == [1, 2]
    # Not merged into one figure spanning the whole 14th to 22nd.
    assert sum(tat.ops_days_by_episode) == 3


def test_raising_does_not_count_as_a_new_ops_pickup(engine):
    """Raising to the bank splits ops time into solo and overlap, but ops never
    put the request down. Counting that as rework would overstate bounces."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 15, 9, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 21, 11, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 23, 10, 0)))

    tat = engine.compute(r)

    assert tat.ops_episode_count == 1
    assert len([e for e in tat.episodes if e.party is Party.OPS]) == 2


def test_bd_and_onboarding_clocks_overlap(engine):
    """When ops bounces for a missing document, BD chases the merchant and
    onboarding still owns the ticket. Both clocks run over the same days."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(
        Event(
            EventType.BOUNCED_TO_BD,
            datetime(2026, 9, 15, 11, 0),
            bounce_reason=BounceReason.MISSING_SIGNATORY_PAN,
        )
    )
    r.add(Event(EventType.RETURNED_TO_OPS, datetime(2026, 9, 22, 10, 0)))

    tat = engine.compute(r, as_of=datetime(2026, 9, 22, 18, 0))

    assert tat.bd_days > 0
    assert tat.bd_days == tat.onboarding_days


def test_bounce_to_onboarding_does_not_start_bd(engine):
    """A wrong MCC is onboarding's to fix. BD is not involved and must not be
    charged for it."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(
        Event(
            EventType.BOUNCED_TO_ONBOARDING,
            datetime(2026, 9, 15, 11, 0),
            bounce_reason=BounceReason.MCC_INCORRECT,
        )
    )
    r.add(Event(EventType.RETURNED_TO_OPS, datetime(2026, 9, 18, 10, 0)))

    tat = engine.compute(r, as_of=datetime(2026, 9, 18, 18, 0))

    assert tat.bd_days == 0
    assert tat.onboarding_days > 0


# --- Ops and acquirer overlap -------------------------------------------


def test_ops_keeps_running_alongside_the_acquirer(engine):
    """Ops owns getting the MID, so its clock does not stop when the request
    goes to the bank. But that time is tagged as overlap, not solo."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))  # Mon
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 15, 9, 0)))  # Tue
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 21, 11, 0)))  # Mon
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 23, 10, 0)))  # Wed

    tat = engine.compute(r)

    assert tat.ops_solo_days == 1  # Mon only, before raising
    assert tat.ops_overlap_days > 0  # Tue onward, alongside the bank
    assert tat.ops_total_days == tat.ops_solo_days + tat.ops_overlap_days


def test_solo_and_overlap_separate_efficiency_from_waiting(engine):
    """The reporting rule: ops time and bank time under separate headers, plus
    a consolidated figure. Ops efficiency should not absorb the bank's queue."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 15, 9, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 10, 5, 11, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 10, 6, 10, 0)))

    tat = engine.compute(r)

    # A long bank wait must not inflate the figure ops is judged on.
    assert tat.ops_solo_days == 1
    assert tat.acquirer_status_days > 10


def test_party_days_exceed_elapsed_days_because_clocks_overlap(engine):
    """This is why a share-of-delay pie chart would be wrong. The test exists
    to make that explicit rather than leaving it as a footnote."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 15, 9, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 25, 11, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 28, 10, 0)))

    tat = engine.compute(r)
    summed = tat.ops_total_days + tat.bd_days + tat.onboarding_days + tat.acquirer_total_days

    assert summed > tat.consolidated_days


# --- Cut-off applied to the acquirer clock ------------------------------


def test_raising_after_cutoff_delays_the_acquirer_clock(engine):
    """Two identical requests, one raised at 9am and one at 4pm on the same
    Friday, both answered the following Friday. The late one owes the bank a
    day less, because the bank's clock started on Monday."""
    early = make_request()
    early.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 17, 10, 0)))
    early.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 18, 9, 0)))
    early.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 25, 11, 0)))

    late = make_request()
    late.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 17, 10, 0)))
    late.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 18, 16, 0)))
    late.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 25, 11, 0)))

    e_tat = engine.compute(early, as_of=datetime(2026, 9, 25, 18, 0))
    l_tat = engine.compute(late, as_of=datetime(2026, 9, 25, 18, 0))

    assert e_tat.acquirer_status_days == l_tat.acquirer_status_days + 1


# --- Acquirer phases and SLA --------------------------------------------


def test_approval_to_mid_is_measured_separately(engine):
    """Approval and MID delivery arrive on different email threads, so the gap
    between them is invisible today. It sits inside procurement TAT."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 15, 9, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 21, 11, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 24, 10, 0)))

    tat = engine.compute(r)

    assert tat.acquirer_status_days > 0
    assert tat.acquirer_mid_days > 0
    assert tat.acquirer_total_days == tat.acquirer_status_days + tat.acquirer_mid_days


def test_sla_breach_detected_against_the_agreed_tat(engine):
    acq = ACQUIRERS["CASTERLY"]  # 4 working days for status
    r = make_request("CASTERLY")
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 14, 11, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 28, 11, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 29, 10, 0)))

    tat = engine.compute(r)
    breach = tat.sla_breach(acq.status_sla_days, acq.mid_sla_days)

    assert breach["status"] is True


def test_sla_is_judged_per_round_not_on_total_held_time(engine):
    """An acquirer that answers twice, each time inside its SLA, has not
    breached. Summing both rounds would charge the bank for our turnaround
    between them."""
    acq = ACQUIRERS["ALDRIDGE"]  # 5 working days for status
    r = make_request("ALDRIDGE")
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 7, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 7, 11, 0)))  # Mon
    r.add(Event(EventType.ACQUIRER_REQUESTED_DOCS, datetime(2026, 9, 10, 15, 0)))  # +3
    r.add(Event(EventType.RESPONDED_TO_ACQUIRER, datetime(2026, 9, 21, 10, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 24, 12, 0)))  # +3
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 25, 10, 0)))

    tat = engine.compute(r)

    assert tat.acquirer_status_rounds == 2
    assert tat.acquirer_status_days == 6  # 3 + 3, more than the 5-day SLA
    assert tat.acquirer_worst_status_round == 3  # but no single round breached
    assert tat.sla_breach(acq.status_sla_days, acq.mid_sla_days)["status"] is False


def test_a_single_slow_round_is_still_a_breach(engine):
    acq = ACQUIRERS["CASTERLY"]  # 4 working days
    r = make_request("CASTERLY")
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 7, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 7, 11, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 21, 12, 0)))

    tat = engine.compute(r, as_of=datetime(2026, 9, 21, 18, 0))

    assert tat.acquirer_worst_status_round > acq.status_sla_days
    assert tat.sla_breach(acq.status_sla_days, acq.mid_sla_days)["status"] is True


def test_acquirer_clock_pauses_while_docs_are_with_us(engine):
    """When the bank asks for documents, its clock stops and ours starts. The
    bank is not charged for our turnaround."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 14, 11, 0)))
    r.add(Event(EventType.ACQUIRER_REQUESTED_DOCS, datetime(2026, 9, 16, 12, 0)))
    r.add(Event(EventType.RESPONDED_TO_ACQUIRER, datetime(2026, 9, 24, 10, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 25, 10, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 28, 10, 0)))

    tat = engine.compute(r)

    # The 16th to the 24th sat with us, so the bank's status clock is short.
    assert tat.acquirer_status_days <= 4
    assert tat.bd_days >= 5


def test_ops_clock_also_stops_while_waiting_on_bd(engine):
    """Ops cannot act while BD chases the merchant, so ops is not charged."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 14, 11, 0)))
    r.add(Event(EventType.ACQUIRER_REQUESTED_DOCS, datetime(2026, 9, 15, 12, 0)))
    r.add(Event(EventType.RESPONDED_TO_ACQUIRER, datetime(2026, 9, 25, 10, 0)))

    tat = engine.compute(r, as_of=datetime(2026, 9, 25, 18, 0))

    # Ops held it 14th-15th and again from the 25th, not the ten days between.
    assert tat.ops_total_days < tat.bd_days


# --- Terminal states -----------------------------------------------------


def test_deprioritisation_stops_every_clock(engine):
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 14, 11, 0)))
    r.add(Event(EventType.DEPRIORITISED, datetime(2026, 9, 18, 10, 0)))

    tat = engine.compute(r, as_of=datetime(2026, 10, 30, 10, 0))

    assert tat.deprioritised is True
    assert all(not e.is_open for e in tat.episodes)
    # Nothing accrues after the 18th, six weeks of as_of notwithstanding.
    assert tat.ops_total_days <= 5


def test_rejection_stops_every_clock(engine):
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 14, 11, 0)))
    r.add(
        Event(
            EventType.ACQUIRER_REJECTED,
            datetime(2026, 9, 18, 10, 0),
            rejection_reason=RejectionReason.RESTRICTED_CATEGORY,
        )
    )

    tat = engine.compute(r, as_of=datetime(2026, 10, 30, 10, 0))

    assert all(not e.is_open for e in tat.episodes)
    assert r.status is RequestStatus.REJECTED


def test_open_request_accrues_time_up_to_now(engine):
    """A request sitting in 'to be raised' is still costing ops days. That
    queue time is invisible in a tracker that only records dates on action."""
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))

    tat = engine.compute(r, as_of=datetime(2026, 9, 25, 10, 0))

    assert tat.ops_solo_days == 9  # two working weeks minus the weekend
    assert any(e.open_at_cutoff for e in tat.episodes)


# --- Model guarantees ----------------------------------------------------


def test_request_is_keyed_on_merchant_mcc_and_acquirer(engine):
    """One merchant with two use cases is two separate requests at the same
    acquirer, each with its own clock. This is why bank replies are matched on
    legal name plus MCC."""
    travel = make_request("ALDRIDGE", mcc="4722")
    retail = make_request("ALDRIDGE", mcc="5399")

    assert travel.request_id != retail.request_id
    assert travel.merchant.legal_name == retail.merchant.legal_name


def test_events_must_arrive_in_order():
    r = make_request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    with pytest.raises(ValueError):
        r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 10, 10, 0)))


def test_bounce_requires_a_reason_code():
    with pytest.raises(ValueError):
        Event(EventType.BOUNCED_TO_BD, datetime(2026, 9, 14, 10, 0))


def test_every_bounce_reason_routes_to_a_party():
    for reason in BounceReason:
        assert reason in BOUNCE_ROUTING


def test_document_gaps_go_to_bd_and_analysis_gaps_to_onboarding():
    assert BOUNCE_ROUTING[BounceReason.MISSING_SIGNATORY_PAN] is Party.BD
    assert BOUNCE_ROUTING[BounceReason.MCC_INCORRECT] is Party.ONBOARDING
    assert BOUNCE_ROUTING[BounceReason.WEBSITE_POLICY_MISSING] is Party.ONBOARDING


def test_status_is_derived_from_events_not_stored(engine):
    r = make_request()
    assert r.status is RequestStatus.PENDING_ONBOARDING
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 14, 10, 0)))
    assert r.status is RequestStatus.TO_BE_RAISED
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 15, 9, 0)))
    assert r.status is RequestStatus.RAISED
