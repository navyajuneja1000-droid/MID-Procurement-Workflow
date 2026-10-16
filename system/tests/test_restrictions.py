"""
Tests for the restriction registry, the assessment it drives, and overrides.

The behaviour worth protecting: only our own policy blocks, a bank's own
refusal becomes a recorded decision rather than a silent removal, an entry ops
adds takes effect without a code change, nothing is ever deleted, and the
evidence from overrides is read cautiously rather than acted on from one case.
"""

from datetime import date, datetime

import pytest

from config.acquirers import ACQUIRERS
from src.catalogue.assess import assess, assess_all, house_candidates
from src.catalogue.eligibility import Verdict
from src.catalogue.mcc import CATALOGUE, Instrument
from src.catalogue.overrides import (
    CATEGORY_REJECTIONS,
    Outcome,
    OverrideLog,
    Reason,
    outcome_from_rejection,
    override_and_raise,
)
from src.catalogue.restrictions import (
    CONFIDENCE,
    HOUSE,
    Enforcement,
    Registry,
    Restriction,
    Scope,
    Source,
    seed,
)
from src.intake.readiness import check_intake
from src.model import (
    ANNOTATION_EVENTS,
    Event,
    EventType,
    Merchant,
    PriorityTier,
    ProcurementRequest,
    RequestStatus,
    Segment,
)

CC = Instrument.CREDIT_CARD
DC = Instrument.DEBIT_CARD
EMI = Instrument.EMI

CLEAN = "7101"  # family clothing, nothing on file anywhere
REFUSED_BY_CATALOGUE = "7527"  # virtual digital assets
ON_ONE_LIST = "7733"  # consulting: Aldridge refuses it, others do not


@pytest.fixture
def registry() -> Registry:
    """A fresh seeded registry per test, never the file on disk.

    Tests that shared the saved registry would pass or fail depending on what
    somebody last added through the CLI, which is the opposite of a test.
    """
    return seed()


@pytest.fixture
def empty() -> Registry:
    return Registry()


def merchant(**overrides) -> Merchant:
    from src.identifiers import gstin_checksum

    body = "29AABCA1234K1Z"
    fields = {
        "entity_legal_name": "Ambervale Wellness Private Limited",
        "company_pan": "AABCA1234K",
        "company_gst": body + gstin_checksum(body),
        "registered_address": "14 Mill Road, Bengaluru 560001",
        "registration_number": "U51909KA2019PTC123456",
        "signatory_name": "Meera Iyer",
        "signatory_pan": "ABCPI9876L",
        "signatory_address": "8 Church Street, Bengaluru 560001",
        "website": "https://ambervale.test",
        "state": "Karnataka",
    }
    for k, v in overrides.items():
        fields.pop(k, None) if v is None else fields.update({k: v})
    return Merchant("M-TEST", fields["entity_legal_name"], Segment.SME,
                    PriorityTier.P3, fields.get("website"), fields)


def request(acquirer="ALDRIDGE", mcc=CLEAN, instruments=(CC, DC)) -> ProcurementRequest:
    return ProcurementRequest(
        request_id=f"REQ-00001-{mcc}-{acquirer}",
        merchant=merchant(),
        mcc=mcc,
        acquirer=ACQUIRERS[acquirer],
        instruments=instruments,
    )


# --- the registry --------------------------------------------------------


def test_anything_ops_adds_defaults_to_a_warning(empty):
    """A single entry typed in haste should flag tomorrow's batch, not remove
    an acquirer from every batch until somebody audits the volumes."""
    entry = empty.add(
        "ALDRIDGE", Scope.MCC, CLEAN, reason="RM mentioned it",
        source=Source.RM_CALL, added_by="ops",
    )
    assert entry.enforcement is Enforcement.WARN


def test_house_policy_defaults_to_a_block(empty):
    entry = empty.add(
        HOUSE, Scope.MCC, CLEAN, reason="not our market",
        source=Source.HOUSE_POLICY, added_by="risk",
    )
    assert entry.enforcement is Enforcement.BLOCK


def test_confidence_follows_the_source_and_is_not_typed_in(empty):
    """Whoever adds an entry is the least neutral judge of how solid their own
    information is."""
    circular = empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="published",
                         source=Source.CIRCULAR, added_by="ops")
    call = empty.add("BRINDLE", Scope.MCC, CLEAN, reason="said on a call",
                     source=Source.RM_CALL, added_by="ops")
    assert circular.confidence > call.confidence
    assert circular.confidence == CONFIDENCE[Source.CIRCULAR]
    assert not hasattr(Restriction, "set_confidence")


def test_an_entry_against_something_that_does_not_exist_is_refused(empty):
    """Worse than no entry: it sits in the list looking like cover and never
    matches anything."""
    with pytest.raises(ValueError, match="not in the catalogue"):
        empty.add("ALDRIDGE", Scope.MCC, "9999", reason="x",
                  source=Source.CIRCULAR, added_by="ops")
    with pytest.raises(ValueError, match="does not exist"):
        empty.add("ALDRIDGE", Scope.CATEGORY, "Nonsense", reason="x",
                  source=Source.CIRCULAR, added_by="ops")


def test_retiring_keeps_the_row(empty):
    """The question asked afterwards is always when a bank changed its
    position and on whose word. A deleted row cannot answer it."""
    entry = empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="x",
                      source=Source.CIRCULAR, added_by="ops")
    empty.retire(entry.entry_id, actor="ops", because="their new circular")
    assert entry in empty.entries
    assert not entry.live
    assert entry.retired_because == "their new circular"
    assert empty.live_entries() == []


def test_every_edit_lands_in_the_change_log(empty):
    entry = empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="x",
                      source=Source.CIRCULAR, added_by="asha")
    empty.retire(entry.entry_id, actor="dev", because="superseded")
    empty.set_enforcement(entry.entry_id, Enforcement.NOTE, actor="dev", because="softened")
    actions = [c.action for c in empty.changes]
    assert actions == ["added", "retired", "enforcement_changed"]
    assert empty.changes[0].actor == "asha"


def test_two_entries_for_the_same_thing_get_distinct_ids(empty):
    a = empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="first",
                  source=Source.CIRCULAR, added_by="ops")
    b = empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="second",
                  source=Source.EMAIL, added_by="ops")
    assert a.entry_id != b.entry_id


def test_a_category_entry_covers_every_code_in_it(empty):
    """Transcribing a category rule into individual codes loses the fact that
    it was a category rule, and the next code added to that category then sits
    outside it."""
    category = CATALOGUE[CLEAN].category
    empty.add("ALDRIDGE", Scope.CATEGORY, category, reason="whole category",
              source=Source.CIRCULAR, added_by="ops")
    in_category = [c for c, e in CATALOGUE.items() if e.category == category]
    assert len(in_category) > 1
    for code in in_category:
        assert empty.matching("ALDRIDGE", code)


def test_an_entry_naming_rails_does_not_apply_to_a_request_without_them(empty):
    empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="no EMI here",
              source=Source.CIRCULAR, added_by="ops", instruments=(EMI,))
    assert empty.matching("ALDRIDGE", CLEAN, (CC, DC)) == []
    assert empty.matching("ALDRIDGE", CLEAN, (CC, EMI))


def test_house_entries_come_back_alongside_the_acquirers_own(registry):
    hits = registry.matching("CASTERLY", REFUSED_BY_CATALOGUE)
    assert any(e.is_house for e in hits)
    # Strongest first, so the caller can take the first entry as the decider.
    assert hits[0].enforcement is Enforcement.BLOCK


def test_the_registry_survives_a_round_trip(registry):
    restored = Registry.from_json(registry.to_json())
    assert len(restored.entries) == len(registry.entries)
    assert len(restored.changes) == len(registry.changes)
    original = {e.entry_id: e for e in registry.entries}
    for e in restored.entries:
        was = original[e.entry_id]
        assert (e.enforcement, e.scope, e.source, e.instruments) == (
            was.enforcement, was.scope, was.source, was.instruments,
        )


def test_the_seed_carries_the_six_policies_without_blocking_on_them(registry):
    """The phase 7 policies become seed data, not the final word."""
    assert registry.acquirers_with_policy() == set(ACQUIRERS)
    seeded = [e for e in registry.live_entries() if e.source is Source.SEED]
    assert seeded
    assert all(e.enforcement is not Enforcement.BLOCK for e in seeded)


def test_only_house_policy_blocks_in_the_seed(registry):
    blocking = [e for e in registry.live_entries() if e.enforcement is Enforcement.BLOCK]
    assert blocking
    assert all(e.is_house for e in blocking)


def test_a_prose_policy_is_stored_at_category_scope(registry):
    """It was written as business categories. Storing it as codes would lose
    that, and the matcher would then disagree with the policy check."""
    verran = registry.live_entries("VERRAN")
    assert verran
    assert all(e.scope is Scope.CATEGORY for e in verran)


# --- assessment ----------------------------------------------------------


def test_our_own_refusal_blocks_at_every_acquirer(registry):
    for a in assess_all(registry, list(ACQUIRERS), REFUSED_BY_CATALOGUE, (CC, DC)):
        assert a.blocked
        assert a.status == "blocked by policy"


def test_an_acquirers_own_refusal_is_a_decision_not_a_block(registry):
    """The change this phase is about. A bank's list is a fact about today, and
    facts about today go stale without telling anyone."""
    a = assess(registry, "ALDRIDGE", ON_ONE_LIST, (CC, DC))
    assert a.needs_decision
    assert not a.blocked
    assert a.eligibility.verdict is Verdict.PROHIBITED


def test_a_category_nobody_has_written_anything_about_is_clear(registry):
    a = assess(registry, "CASTERLY", CLEAN, (CC, DC))
    assert a.enforcement is None
    assert a.status == "clear"
    assert a.explain() == "No restriction on file."


def test_an_entry_ops_adds_takes_effect_without_a_code_change(registry):
    """The whole point of the registry being data. Before the entry the
    application goes out on its own; after it, a person decides."""
    before = assess(registry, "CASTERLY", CLEAN, (CC, DC))
    assert before.can_raise_without_a_decision

    registry.add(
        "CASTERLY", Scope.MCC, CLEAN,
        reason="they told us on a call they have paused this category",
        source=Source.RM_CALL, added_by="n.juneja",
    )
    after = assess(registry, "CASTERLY", CLEAN, (CC, DC))
    assert after.needs_decision
    assert "paused this category" in after.explain()


def test_retiring_an_entry_clears_the_flag(registry):
    entry = registry.add(
        "CASTERLY", Scope.MCC, CLEAN, reason="paused", source=Source.RM_CALL,
        added_by="ops",
    )
    assert assess(registry, "CASTERLY", CLEAN, (CC, DC)).needs_decision
    registry.retire(entry.entry_id, actor="ops", because="they resumed it")
    assert assess(registry, "CASTERLY", CLEAN, (CC, DC)).can_raise_without_a_decision


def test_the_explanation_names_the_source_the_date_and_who_recorded_it(registry):
    """"Restricted category" tells a person nothing they can act on."""
    registry.add(
        "CASTERLY", Scope.MCC, CLEAN, reason="paused for now",
        source=Source.RM_CALL, added_by="n.juneja", on=date(2026, 10, 16),
    )
    text = assess(registry, "CASTERLY", CLEAN, (CC, DC)).explain()
    assert "rm_call" in text
    assert "2026-10-16" in text
    assert "n.juneja" in text
    assert "0.60" in text


def test_a_rail_restriction_goes_out_with_the_rails_it_can_use(registry):
    a = assess(registry, "BRINDLE", "7726", (CC, DC))
    assert a.can_raise_without_a_decision
    assert CC in a.blocked_instruments
    assert DC not in a.blocked_instruments
    assert a.conditions


def test_every_requested_rail_barred_needs_a_decision(registry):
    """Notes that between them close every rail are not a note any more: there
    is nothing left to raise."""
    a = assess(registry, "BRINDLE", "7501", (CC,))
    assert CC in a.blocked_instruments
    assert a.needs_decision


def test_a_policy_we_cannot_read_needs_a_person(registry):
    a = assess(registry, "NOSUCHBANK", CLEAN, (CC,))
    assert a.eligibility.verdict is Verdict.UNKNOWN
    assert a.needs_decision


def test_the_strongest_entry_decides(empty):
    empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="a note",
              source=Source.CIRCULAR, added_by="ops", enforcement=Enforcement.NOTE)
    empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="a warning",
              source=Source.EMAIL, added_by="ops", enforcement=Enforcement.WARN)
    a = assess(empty, "ALDRIDGE", CLEAN, (CC, DC))
    assert a.needs_decision
    assert a.deciding_entry.reason == "a warning"


# --- the intake check ----------------------------------------------------


def test_a_block_stops_the_document_chase(registry):
    result = check_intake(
        merchant(signatory_pan=None), list(ACQUIRERS.values()),
        mcc=REFUSED_BY_CATALOGUE, instruments=(CC, DC), registry=registry,
    )
    assert result.bounces == []
    assert sorted(result.ineligible) == sorted(ACQUIRERS)


def test_a_pending_decision_does_not_stop_the_document_chase(registry):
    """The decision often comes back yes, and the chase would otherwise start
    from scratch after it."""
    result = check_intake(
        merchant(signatory_pan=None), [ACQUIRERS["ALDRIDGE"]],
        mcc=ON_ONE_LIST, instruments=(CC, DC), registry=registry,
    )
    assert result.awaiting_decision == ["ALDRIDGE"]
    assert result.bounces
    assert result.bounces[0].blocked_acquirers == ["ALDRIDGE"]


def test_a_pending_decision_is_not_an_automatic_raise(registry):
    result = check_intake(
        merchant(), [ACQUIRERS["ALDRIDGE"]], mcc=ON_ONE_LIST,
        instruments=(CC, DC), registry=registry,
    )
    assert result.ready_now == []
    assert result.awaiting_decision == ["ALDRIDGE"]
    assert not result.fully_ready


def test_each_decision_comes_with_the_entry_behind_it(registry):
    result = check_intake(
        merchant(), [ACQUIRERS["ALDRIDGE"]], mcc=ON_ONE_LIST,
        instruments=(CC, DC), registry=registry,
    )
    assert result.decisions_needed
    _code, explanation = result.decisions_needed[0]
    assert "seed" in explanation


def test_the_summary_distinguishes_the_three_reasons(registry):
    result = check_intake(
        merchant(signatory_pan=None), list(ACQUIRERS.values()), mcc=ON_ONE_LIST,
        instruments=(CC, DC), registry=registry,
    )
    text = result.summary()
    assert "awaiting an ops decision" in text or "blocked on documents" in text
    assert "blocked by our own policy" not in text


# --- overrides -----------------------------------------------------------


def test_an_override_is_recorded_against_the_entry_not_only_the_request():
    """"How often has this entry been overridden and what happened" cannot be
    reconstructed from request-level notes."""
    log = OverrideLog()
    log.record("REQ-1", "ALDRIDGE", ON_ONE_LIST, "ALDRIDGE-MCC-7733",
               Reason.RISK_SIGNED_OFF, actor="ops", on=date(2026, 10, 1))
    assert log.for_entry("ALDRIDGE-MCC-7733")


def test_one_request_cannot_carry_two_overrides():
    log = OverrideLog()
    log.record("REQ-1", "ALDRIDGE", ON_ONE_LIST, "E1", Reason.RM_CONFIRMED, actor="ops")
    with pytest.raises(ValueError, match="already carries"):
        log.record("REQ-1", "ALDRIDGE", ON_ONE_LIST, "E1", Reason.RM_CONFIRMED, actor="ops")


def test_an_approval_undermines_the_entry_and_a_category_refusal_supports_it():
    assert outcome_from_rejection(None)[0] is Outcome.APPROVED
    assert outcome_from_rejection("restricted_category")[0] is Outcome.REJECTED


def test_a_refusal_for_an_unrelated_reason_proves_nothing_either_way():
    """Counting a broken payment flow as proof the category is barred would
    have the registry grow on evidence that has nothing to do with it."""
    outcome, detail = outcome_from_rejection("payment_flow_broken")
    assert outcome is Outcome.WITHDRAWN
    assert "says nothing" in detail


def test_only_category_reasons_count_as_evidence():
    assert "restricted_category" in CATEGORY_REJECTIONS
    assert "document_discrepancy" not in CATEGORY_REJECTIONS
    assert "website_not_legitimate" not in CATEGORY_REJECTIONS


def _log_with(outcomes: list[Outcome]) -> OverrideLog:
    log = OverrideLog()
    for i, outcome in enumerate(outcomes):
        log.record(f"REQ-{i}", "ALDRIDGE", ON_ONE_LIST, "E1",
                   Reason.RISK_SIGNED_OFF, actor="ops")
        log.resolve(f"REQ-{i}", outcome)
    return log


def test_one_approval_is_not_treated_as_proof_a_bank_changed_its_mind():
    """A single application can slip through underwriting for reasons nobody
    records."""
    assert _log_with([Outcome.APPROVED]).verdict_on("E1") == "possibly stale, watch it"


def test_three_approvals_and_no_refusals_is_worth_acting_on():
    log = _log_with([Outcome.APPROVED] * 3)
    assert log.verdict_on("E1") == "looks stale, consider retiring"


def test_any_category_refusal_means_the_entry_holds_up():
    log = _log_with([Outcome.APPROVED] * 3 + [Outcome.REJECTED])
    assert log.verdict_on("E1") == "mixed, keep it"
    assert _log_with([Outcome.REJECTED]).verdict_on("E1") == "entry holds up"


def test_an_entry_with_no_answers_yet_claims_nothing():
    log = OverrideLog()
    log.record("REQ-1", "ALDRIDGE", ON_ONE_LIST, "E1", Reason.RM_CONFIRMED, actor="ops")
    assert log.verdict_on("E1") == "no evidence yet"


def test_the_rejection_rate_ignores_the_ones_that_prove_nothing():
    log = _log_with([Outcome.APPROVED, Outcome.REJECTED, Outcome.WITHDRAWN])
    assert log.rejection_rate == 0.5


def test_the_rejection_rate_is_none_before_any_answer():
    log = OverrideLog()
    log.record("REQ-1", "ALDRIDGE", ON_ONE_LIST, "E1", Reason.RM_CONFIRMED, actor="ops")
    assert log.rejection_rate is None


def test_the_history_line_is_what_makes_the_flag_worth_reading():
    log = _log_with([Outcome.APPROVED, Outcome.APPROVED])
    assert log.history_for("E1") == "overridden 2 times, 2 approved"
    assert OverrideLog().history_for("E1") == "never overridden"


def test_the_override_log_survives_a_round_trip():
    log = _log_with([Outcome.APPROVED, Outcome.REJECTED])
    restored = OverrideLog.from_json(log.to_json())
    assert [o.outcome for o in restored.overrides] == [
        Outcome.APPROVED, Outcome.REJECTED
    ]


def test_the_decision_is_written_to_the_event_log_and_the_override_log(registry):
    """Two places, two questions. The event log says what this request did; the
    override log says whether the entry deserves to exist."""
    r = request("ALDRIDGE", ON_ONE_LIST)
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 10, 1, 9, 0)))
    a = assess(registry, "ALDRIDGE", ON_ONE_LIST, (CC, DC))
    log = OverrideLog()

    override_and_raise(r, a, Reason.RISK_SIGNED_OFF, "n.juneja", log,
                       datetime(2026, 10, 1, 10, 0))

    assert any(e.event_type is EventType.RESTRICTION_OVERRIDDEN for e in r.events)
    assert log.overrides[0].entry_id == a.deciding_entry.entry_id
    assert log.overrides[0].actor == "n.juneja"


def test_an_override_moves_no_clock_and_changes_no_status(registry):
    """It records a decision. It is not a handoff, so charging anyone a day for
    it would overstate the work."""
    from src.calendar_rules import load_calendar
    from src.tat import TatEngine
    from config.acquirers import HOLIDAYS

    r = request("ALDRIDGE", ON_ONE_LIST)
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 10, 1, 9, 0)))
    before = r.status

    engine = TatEngine(load_calendar(HOLIDAYS))
    clean = engine.compute(r, as_of=datetime(2026, 10, 5, 18, 0))

    r.add(Event(EventType.RESTRICTION_OVERRIDDEN, datetime(2026, 10, 2, 10, 0),
                note="risk_signed_off by ops"))
    after = engine.compute(r, as_of=datetime(2026, 10, 5, 18, 0))

    assert r.status is before is RequestStatus.TO_BE_RAISED
    assert len(after.episodes) == len(clean.episodes)
    assert EventType.RESTRICTION_OVERRIDDEN in ANNOTATION_EVENTS


# --- house policy candidates ---------------------------------------------


def test_candidates_carry_how_many_lists_they_were_drawn_from(registry):
    """"Every acquirer refuses this" means much less across two lists than
    across six, so the coverage travels with the claim."""
    for c in house_candidates(registry):
        assert c.coverage == len(registry.acquirers_with_policy())


def test_unanimity_across_a_few_lists_is_not_called_confident(empty):
    empty.add("ALDRIDGE", Scope.MCC, CLEAN, reason="no", source=Source.CIRCULAR,
              added_by="ops", enforcement=Enforcement.WARN)
    empty.add("BRINDLE", Scope.MCC, CLEAN, reason="no", source=Source.CIRCULAR,
              added_by="ops", enforcement=Enforcement.WARN)
    candidate = next(c for c in house_candidates(empty) if c.scope_value == CLEAN)
    assert candidate.unanimous
    assert not candidate.confident


def test_unanimity_across_four_lists_is(empty):
    for code in ("ALDRIDGE", "BRINDLE", "CASTERLY", "VERRAN"):
        empty.add(code, Scope.MCC, CLEAN, reason="no", source=Source.CIRCULAR,
                  added_by="ops", enforcement=Enforcement.WARN)
    candidate = next(c for c in house_candidates(empty) if c.scope_value == CLEAN)
    assert candidate.confident


def test_candidates_are_never_applied_on_their_own(registry):
    """The intersection widens as our coverage narrows, so inferring a block
    from it would make the rules strongest where the data is thinnest."""
    candidates = house_candidates(registry)
    assert candidates
    for c in candidates:
        for a in assess_all(registry, list(ACQUIRERS), c.scope_value, (CC, DC)):
            assert not a.blocked


def test_promotion_is_recorded_as_a_decision_and_then_it_blocks(registry):
    candidate = house_candidates(registry)[0]
    registry.promote_to_house(
        Scope.MCC, candidate.scope_value, actor="risk",
        because="refused everywhere we asked, and we are not pricing it",
    )
    for a in assess_all(registry, list(ACQUIRERS), candidate.scope_value, (CC, DC)):
        assert a.blocked
    assert any(c.action == "promoted" for c in registry.changes)


def test_a_category_already_refused_by_the_catalogue_is_not_offered(registry):
    offered = {c.scope_value for c in house_candidates(registry)}
    assert REFUSED_BY_CATALOGUE not in offered


def test_an_entry_ops_adds_counts_towards_the_overlap(registry):
    """The thing worth promoting is the category the team keeps being told no
    about, and they are the ones recording the noes."""
    before = {c.scope_value for c in house_candidates(registry)}
    assert CLEAN not in before
    for code in ("ALDRIDGE", "BRINDLE"):
        registry.add(code, Scope.MCC, CLEAN, reason="they stopped taking it",
                     source=Source.EMAIL, added_by="ops")
    after = {c.scope_value for c in house_candidates(registry)}
    assert CLEAN in after
