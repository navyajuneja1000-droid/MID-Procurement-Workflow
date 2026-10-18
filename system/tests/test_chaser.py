"""
Tests for the SLA chaser.

What is worth protecting: nobody is chased for something we owe them, lateness
is judged on the round the bank is holding now, the ladder ends instead of
emailing forever, and the effect measurement refuses to report a number it
cannot support. The last one has a test that plants a known effect and checks
the estimator recovers it, because an analysis nobody has tested against a
known answer is an opinion with arithmetic in it.
"""

from datetime import date, datetime

import pytest

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.chaser.draft import draft_for_acquirer, escalation_note
from src.chaser.due import Owes, find_overdue, owed_by, rank
from src.chaser.effect import (
    Round,
    compare_from_day,
    compare_stratified,
    naive_compare,
)
from src.chaser.holdout import HOLDOUT_SHARE, in_holdout
from src.chaser.ladder import COOLDOWN_DAYS, ChaseLog, Stage
from src.model import (
    Event,
    EventType,
    Merchant,
    PriorityTier,
    ProcurementRequest,
    RejectionReason,
    Segment,
)
from src.tat import TatEngine

CAL = load_calendar(HOLIDAYS)
ENGINE = TatEngine(CAL)
AS_OF = datetime(2026, 9, 30, 18, 0)


def request(acquirer="ALDRIDGE", tier=PriorityTier.P3, rid="REQ-1") -> ProcurementRequest:
    m = Merchant("M-1", "Harrowgate Trading Private Limited", Segment.SME, tier, None, {})
    return ProcurementRequest(rid, m, "7101", ACQUIRERS[acquirer])


def raised(r, day=1, hour=9):
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, day, hour)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, day, hour + 1)))
    return r


def overdue_for(r, as_of=AS_OF):
    return find_overdue(r, ENGINE.compute(r, as_of=as_of), r.acquirer, as_of)


# --- whose court ---------------------------------------------------------


def test_a_request_the_bank_is_holding_can_be_chased():
    r = raised(request())
    assert owed_by(r) is Owes.ACQUIRER_STATUS
    assert overdue_for(r) is not None


def test_a_request_waiting_on_us_is_never_chased():
    """The reply to that chase is a screenshot of their own email."""
    r = raised(request())
    r.add(Event(EventType.ACQUIRER_REQUESTED_DOCS, datetime(2026, 9, 2, 10)))
    assert owed_by(r) is Owes.US
    assert overdue_for(r) is None


def test_a_bounced_request_is_not_the_banks_problem():
    r = request()
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 1, 9)))
    from src.model import BounceReason

    r.add(Event(EventType.BOUNCED_TO_BD, datetime(2026, 9, 1, 11),
                bounce_reason=BounceReason.MISSING_SIGNATORY_PAN))
    assert owed_by(r) is Owes.US
    assert overdue_for(r) is None


def test_an_approved_request_still_waiting_for_the_mid_is_chaseable():
    """Approval and MID delivery are different events on different threads, and
    the gap between them is the one nobody watches."""
    r = raised(request())
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 2, 10)))
    item = overdue_for(r)
    assert owed_by(r) is Owes.ACQUIRER_MID
    assert item is not None
    assert item.what_is_owed == "the MID"
    assert item.sla_days == ACQUIRERS["ALDRIDGE"].mid_sla_days


def test_finished_and_deprioritised_requests_are_left_alone():
    done = raised(request(rid="REQ-done"))
    done.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 2, 10)))
    done.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 3, 10)))
    assert overdue_for(done) is None

    rejected = raised(request(rid="REQ-rej"))
    rejected.add(Event(EventType.ACQUIRER_REJECTED, datetime(2026, 9, 4, 10),
                       rejection_reason=RejectionReason.RESTRICTED_CATEGORY))
    assert overdue_for(rejected) is None

    parked = raised(request(rid="REQ-dep"))
    parked.add(Event(EventType.DEPRIORITISED, datetime(2026, 9, 2, 10)))
    assert overdue_for(parked) is None


# --- which clock ---------------------------------------------------------


def test_lateness_is_judged_on_the_round_the_bank_holds_now():
    """A bank that answered twice quickly and is two days into a third round is
    two days late, not twelve. Charging it the total would hand it a chase it
    can disprove, and the next one goes unread."""
    r = raised(request(), day=1)
    r.add(Event(EventType.ACQUIRER_REQUESTED_DOCS, datetime(2026, 9, 18, 10)))
    r.add(Event(EventType.RESPONDED_TO_ACQUIRER, datetime(2026, 9, 21, 10)))
    item = overdue_for(r, as_of=datetime(2026, 9, 23, 18))
    tat = ENGINE.compute(r, as_of=datetime(2026, 9, 23, 18))
    assert tat.acquirer_status_days > 10          # total held is large
    assert item is None or item.days_held <= 3    # the current round is young


def test_a_round_inside_the_agreed_time_is_not_chased():
    r = raised(request("VERRAN"), day=28)  # SLA 8 working days
    assert overdue_for(r, as_of=datetime(2026, 9, 30, 18)) is None


def test_the_round_number_is_reported_so_the_email_can_say_it():
    r = raised(request(), day=1)
    r.add(Event(EventType.ACQUIRER_REQUESTED_DOCS, datetime(2026, 9, 2, 10)))
    r.add(Event(EventType.RESPONDED_TO_ACQUIRER, datetime(2026, 9, 3, 10)))
    item = overdue_for(r)
    assert item is not None
    assert item.round_index == 2


# --- ranking -------------------------------------------------------------


def test_a_p0_one_day_late_outranks_a_p4_six_days_late():
    """A queue sorted by days overdue alone puts the wrong one first."""
    p0 = raised(request(tier=PriorityTier.P0, rid="REQ-p0"), day=22)
    p4 = raised(request(tier=PriorityTier.P4, rid="REQ-p4"), day=15)
    items = [overdue_for(p4), overdue_for(p0)]
    assert all(items)
    assert items[1].days_over < items[0].days_over
    assert rank(items)[0].tier is PriorityTier.P0


def test_a_low_tier_still_surfaces_once_it_has_rotted():
    """The weighting must not starve its own tail. A P4 twelve days over is a
    problem whatever tier it is."""
    p0 = raised(request(tier=PriorityTier.P0, rid="REQ-p0"), day=22)
    p4 = raised(request(tier=PriorityTier.P4, rid="REQ-p4"), day=8)
    items = [overdue_for(p0), overdue_for(p4)]
    assert all(items)
    assert rank(items)[0].tier is PriorityTier.P4


def test_the_order_is_stable_between_runs():
    a = raised(request(tier=PriorityTier.P3, rid="REQ-a"), day=10)
    b = raised(request(tier=PriorityTier.P3, rid="REQ-b"), day=10)
    items = [overdue_for(a), overdue_for(b)]
    assert [o.request.request_id for o in rank(items)] == \
           [o.request.request_id for o in rank(list(reversed(items)))]


# --- the ladder ----------------------------------------------------------


def test_the_ladder_ends_instead_of_emailing_forever():
    """Automation that escalates to a person at a defined point is credible.
    Automation that keeps sending is a robot arguing with a mail filter."""
    log = ChaseLog()
    assert log.next_stage("REQ-1", 1) is Stage.FIRST
    log.record("REQ-1", "ALDRIDGE", Stage.FIRST, 1, 2, date(2026, 9, 1))
    assert log.next_stage("REQ-1", 1) is Stage.SECOND
    log.record("REQ-1", "ALDRIDGE", Stage.SECOND, 1, 5, date(2026, 9, 5))
    assert log.next_stage("REQ-1", 1) is Stage.ESCALATE
    assert not Stage.ESCALATE.is_email


def test_a_new_round_starts_the_ladder_again():
    """A bank that answered, asked for a document and went quiet again is on a
    fresh round. Carrying the count over would escalate it to a phone call for
    a round it has barely held."""
    log = ChaseLog()
    log.record("REQ-1", "ALDRIDGE", Stage.FIRST, 1, 2, date(2026, 9, 1))
    log.record("REQ-1", "ALDRIDGE", Stage.SECOND, 1, 5, date(2026, 9, 5))
    assert log.next_stage("REQ-1", 2) is Stage.FIRST


def test_nothing_is_chased_twice_in_two_days():
    log = ChaseLog()
    log.record("REQ-1", "ALDRIDGE", Stage.FIRST, 1, 2, date(2026, 9, 1))
    assert log.in_cooldown("REQ-1", date(2026, 9, 2), CAL.working_days_between)
    assert not log.in_cooldown(
        "REQ-1", date(2026, 9, 1 + COOLDOWN_DAYS + 4), CAL.working_days_between
    )


def test_an_unchased_request_is_not_in_cooldown():
    assert not ChaseLog().in_cooldown("REQ-new", date(2026, 9, 2), CAL.working_days_between)


def test_the_chase_log_survives_a_round_trip():
    log = ChaseLog()
    log.record("REQ-1", "ALDRIDGE", Stage.SECOND, 2, 4, date(2026, 9, 1))
    log.answered("REQ-1", date(2026, 9, 4), 3)
    back = ChaseLog.from_json(log.to_json())
    assert back.chases[0].stage is Stage.SECOND
    assert back.chases[0].working_days_to_answer == 3


# --- the drafts ----------------------------------------------------------


def _two_overdue():
    a = raised(request(rid="REQ-a"), day=10)
    b = raised(request(rid="REQ-b"), day=12)
    return [overdue_for(a), overdue_for(b)]


def test_one_email_covers_everything_overdue_at_one_acquirer():
    """Eleven separate emails is one piece of information and ten
    interruptions."""
    items = _two_overdue()
    note = draft_for_acquirer(items, Stage.FIRST, date(2026, 9, 30), "x@y.test")
    assert len(note.request_ids) == 2
    assert note.body.count("  - ") == 2


def test_every_line_quotes_the_date_and_the_agreed_time():
    """The only chase a bank cannot argue with is one that quotes its own
    commitment back with a date on it."""
    items = _two_overdue()
    body = draft_for_acquirer(items, Stage.FIRST, date(2026, 9, 30), "x@y.test").body
    assert "working days on this round against" in body
    assert "raised " in body


def test_the_second_chase_does_not_read_like_the_first():
    items = _two_overdue()
    first = draft_for_acquirer(items, Stage.FIRST, date(2026, 9, 30), "x@y.test")
    second = draft_for_acquirer(items, Stage.SECOND, date(2026, 9, 30), "x@y.test")
    assert first.body != second.body
    assert "Second follow-up" in second.subject


def test_the_draft_invites_a_correction():
    """Half the point. If it is actually waiting on us, we want to be told
    rather than to keep chasing."""
    items = _two_overdue()
    body = draft_for_acquirer(items, Stage.FIRST, date(2026, 9, 30), "x@y.test").body
    assert "waiting on us rather than on you" in body


def test_the_escalation_goes_to_a_person_not_the_bank():
    note = escalation_note(_two_overdue())
    assert "relationship manager" in note
    assert "Dear Team" not in note


def test_nothing_in_the_chaser_can_send_anything():
    import inspect

    from src.chaser import draft as draft_mod
    from src.chaser import ladder as ladder_mod

    for module in (draft_mod, ladder_mod):
        source = inspect.getsource(module)
        for forbidden in ("smtplib", "sendmail", "requests.post", "urlopen"):
            assert forbidden not in source


# --- the holdout ---------------------------------------------------------


def test_the_holdout_is_stable_for_the_same_request():
    assert in_holdout("REQ-00042") == in_holdout("REQ-00042")


def test_the_holdout_is_roughly_the_share_it_claims():
    ids = [f"REQ-{i:05d}" for i in range(4000)]
    share = sum(in_holdout(i) for i in ids) / len(ids)
    assert abs(share - HOLDOUT_SHARE) < 0.02


def test_p0_merchants_are_never_held_out():
    """The experiment is not worth running on the accounts someone outside ops
    is already asking about."""
    held = [
        f"REQ-{i:05d}" for i in range(2000) if in_holdout(f"REQ-{i:05d}")
    ]
    assert held  # the holdout is not empty for other tiers
    assert not any(in_holdout(r, PriorityTier.P0) for r in held)


# --- does chasing work ---------------------------------------------------


def _round(held, chased_on=None, acq="ALDRIDGE", rid="R"):
    return Round(request_id=rid, acquirer_code=acq, days_held_total=held,
                 chased_on_day=chased_on)


def test_the_naive_comparison_points_the_wrong_way():
    """The test exists to prove the trap is real, not to endorse the method.
    Chased rounds are selected for having already run long."""
    rounds = [_round(2) for _ in range(50)] + [_round(9, chased_on=6) for _ in range(20)]
    naive = naive_compare(rounds)
    assert naive.difference < 0  # chasing looks like it makes banks slower


def test_the_matched_comparison_removes_that_bias():
    """Same data, compared only among rounds that survived to the chase day."""
    rounds = [_round(2) for _ in range(50)] + [_round(9, chased_on=6) for _ in range(20)]
    matched = compare_from_day(rounds, 6)
    assert matched.n_not_chased == 0  # the day-2 rounds are correctly excluded


def test_a_round_not_yet_due_a_chase_is_not_counted_as_a_control():
    """Conflating 'not chased yet' with 'deliberately not chased' is what made
    the first version report four banks as having no chases at all."""
    rounds = [_round(10, chased_on=8)]
    assert compare_from_day(rounds, 6).n_chased == 0
    assert compare_from_day(rounds, 6).n_not_chased == 0


def test_the_estimator_recovers_a_planted_effect():
    """The one that makes the rest of it trustworthy: plant a known two-day
    effect and check the number comes back."""
    rounds = []
    for i in range(120):  # chased: answer two days after the chase
        rounds.append(_round(8, chased_on=6, rid=f"c{i}"))
    for i in range(120):  # held out: answer four days after that same point
        rounds.append(_round(10, rid=f"h{i}"))
    c = compare_from_day(rounds, 6)
    assert c.usable
    assert c.difference == pytest.approx(2.0, abs=0.01)
    assert "sooner" in c.verdict()


def test_it_refuses_to_report_a_number_from_too_few_rounds():
    rounds = [_round(8, chased_on=6) for _ in range(5)] + [_round(10) for _ in range(5)]
    c = compare_from_day(rounds, 6)
    assert not c.usable
    assert "Not enough" in c.verdict()


def test_each_acquirer_is_compared_on_its_own_chase_day():
    """One shared day puts every round of a slow-SLA bank in the control arm,
    because none of them is due a chase yet."""
    rounds = [
        _round(9, chased_on=6, acq="ALDRIDGE", rid=f"a{i}") for i in range(30)
    ] + [
        _round(11, chased_on=9, acq="VERRAN", rid=f"v{i}") for i in range(30)
    ]
    strat = compare_stratified(rounds, {"ALDRIDGE": 6, "VERRAN": 9})
    assert strat.per_acquirer["ALDRIDGE"].n_chased == 30
    assert strat.per_acquirer["VERRAN"].n_chased == 30


def test_pooling_weights_each_acquirer_by_its_size():
    """An acquirer with four rounds should not move the headline as much as one
    with ninety."""
    big = [_round(8, chased_on=6, acq="A", rid=f"bc{i}") for i in range(100)]
    big += [_round(10, acq="A", rid=f"bh{i}") for i in range(100)]
    small = [_round(8, chased_on=6, acq="B", rid=f"sc{i}") for i in range(25)]
    small += [_round(20, acq="B", rid=f"sh{i}") for i in range(25)]
    strat = compare_stratified(big + small, {"A": 6, "B": 6})
    pooled = strat.pooled_difference
    assert 2.0 < pooled < 4.5  # dragged towards A, not sitting at B's 12
