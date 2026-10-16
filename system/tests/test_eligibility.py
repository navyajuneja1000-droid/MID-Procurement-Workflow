"""
Tests for the MCC catalogue and per-acquirer eligibility.

The behaviour worth protecting: eligibility is decided per acquirer, the five
risk levels stay distinct, instrument-level rules are honoured, a policy that
cannot be resolved says so instead of guessing, and an ineligible acquirer
never produces a document chase.
"""

import pytest

from config.acquirers import ACQUIRERS
from src.catalogue.eligibility import (
    POLICIES,
    PolicyShape,
    Verdict,
    check,
    check_all,
)
from src.catalogue.mcc import (
    CATALOGUE,
    CHANGE_LOG,
    Instrument,
    Risk,
    lookup,
    resolve,
)
from src.identifiers import gstin_checksum
from src.intake.readiness import check_intake
from src.model import Acquirer, Merchant, PriorityTier, Segment

CC = Instrument.CREDIT_CARD
DC = Instrument.DEBIT_CARD
EMI = Instrument.EMI
CLU = Instrument.CREDIT_LINE_UPI

_GST_BODY = "29AABCA1234K1Z"
GOOD_GST = _GST_BODY + gstin_checksum(_GST_BODY)


def merchant(**overrides) -> Merchant:
    fields = {
        "entity_legal_name": "Ambervale Wellness Private Limited",
        "company_pan": "AABCA1234K",
        "company_gst": GOOD_GST,
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


# --- the catalogue -------------------------------------------------------


def test_risk_has_five_distinct_levels():
    """Collapsing these into allowed/blocked is the mistake this guards
    against: 'restricted with licence' is a document request, not a refusal."""
    assert len(set(Risk)) == 5
    assert Risk.RESTRICTED_WITH_LICENCE.needs_licence
    assert not Risk.RESTRICTED_WITH_LICENCE.refused_outright
    assert Risk.RESTRICTED.refused_outright
    assert not Risk.HIGH.refused_outright


def test_every_licence_tier_names_the_licence():
    """A conditional acceptance is useless without saying what to produce."""
    for e in CATALOGUE.values():
        if e.risk.needs_licence:
            assert e.licence_required, f"{e.code} needs a licence but names none"


def test_codes_are_unique_and_well_formed():
    for code, e in CATALOGUE.items():
        assert code == e.code
        assert len(code) == 4 and code.isdigit()


def test_instrument_restrictions_only_on_codes_that_are_not_refused():
    """A refused code cannot also have instrument rules: if the category is
    out, which rail it would have run on is moot, and carrying both invites a
    contradiction nobody notices."""
    for e in CATALOGUE.values():
        if e.risk.refused_outright:
            assert not e.instrument_restrictions, (
                f"{e.code} is refused outright but also carries instrument rules"
            )


def test_change_log_entries_point_at_real_codes():
    for c in CHANGE_LOG:
        assert c.code in CATALOGUE


def test_resolve_blocks_only_the_requested_instruments():
    r = resolve("7501", (CC, DC))  # lending: credit instruments barred
    assert r is not None
    assert CC in r.blocked_instruments
    assert DC not in r.blocked_instruments
    assert r.allowed_instruments == (DC,)


def test_a_category_can_be_open_on_one_rail_and_closed_on_another():
    r = resolve("7726", (CC, DC))  # rent: fine on debit, not on credit
    assert r is not None
    assert not r.refused
    assert not r.fully_blocked_on_instruments
    assert r.allowed_instruments == (DC,)


def test_fully_blocked_on_instruments_is_not_a_category_refusal():
    """Distinct outcomes: the business is acceptable, the payment method is
    not, and the merchant may still go live on something else."""
    r = resolve("7501", (CC, CLU))
    assert r is not None
    assert r.fully_blocked_on_instruments
    assert not r.refused


def test_unknown_code_resolves_to_nothing():
    assert lookup("9999") is None
    assert resolve("9999", (CC,)) is None


# --- per-acquirer eligibility -------------------------------------------


def test_the_same_mcc_gets_different_answers_from_different_acquirers():
    """The finding this whole module exists for: there is no single
    restricted list."""
    verdicts = {r.verdict for r in check_all(list(POLICIES), "7428", (CC, DC))}
    assert len(verdicts) > 1


def test_at_least_one_category_splits_three_ways():
    splits = 0
    for code in CATALOGUE:
        if len({r.verdict for r in check_all(list(POLICIES), code, (CC, DC))}) >= 3:
            splits += 1
    assert splits >= 1


def test_two_platforms_of_one_bank_can_disagree():
    """Policy is held per rail, not per bank. CYBS and FSS are the same
    institution and do not share a list."""
    cybs = check("NORTHWAY_CYBS", "7726", (DC,))
    fss = check("NORTHWAY_FSS", "7726", (DC,))
    assert cybs.verdict is not fss.verdict


def test_a_catalogue_refusal_cannot_be_widened_by_any_acquirer():
    """Acquirer policy narrows, never widens. If the catalogue refuses a code,
    the permissive acquirer cannot take it either."""
    for code in (c for c, e in CATALOGUE.items() if e.risk.refused_outright):
        for r in check_all(list(POLICIES), code, (CC, DC)):
            assert r.verdict is Verdict.PROHIBITED


def test_the_permissive_acquirer_accepts_what_the_strict_one_refuses():
    strict = check("ALDRIDGE", "7733", (CC, DC))  # consulting
    permissive = check("CASTERLY", "7733", (CC, DC))
    assert strict.verdict is Verdict.PROHIBITED
    assert permissive.verdict is Verdict.ELIGIBLE


def test_a_licence_requirement_is_conditional_not_a_refusal():
    r = check("CASTERLY", "7407", (CC, DC))  # pharmacy
    assert r.verdict is Verdict.CONDITIONAL
    assert r.can_raise
    assert "licence" in r.condition.lower() or "Form" in r.condition


def test_instrument_level_ban_narrows_without_refusing():
    r = check("BRINDLE", "7726", (CC, DC))  # rent, credit barred
    assert r.verdict is Verdict.CONDITIONAL
    assert CC in r.blocked_instruments
    assert DC not in r.blocked_instruments
    assert r.can_raise


def test_every_requested_instrument_barred_is_a_prohibition():
    r = check("BRINDLE", "7501", (CC, CLU))  # lending on credit only
    assert r.verdict is Verdict.PROHIBITED
    assert not r.can_raise


def test_a_product_specific_ban_applies_only_to_that_product():
    """One acquirer keeps a second, stricter list for EMI alone."""
    with_emi = check("NORTHWAY_CYBS", "7308", (CC, EMI))
    without = check("NORTHWAY_CYBS", "7308", (CC, DC))
    assert EMI in with_emi.blocked_instruments
    assert not without.blocked_instruments


def test_a_prose_policy_answer_is_flagged_as_interpretation():
    """Matching a paragraph against a category tree is a judgement. The result
    carries lower confidence so a person can confirm it, rather than being
    passed off as a published code list."""
    r = check("VERRAN", "7224", (CC, DC))  # alcohol, matched from prose
    assert r.confidence < 1.0
    assert r.source == "prose policy"


def test_a_coded_policy_answer_is_certain():
    r = check("ALDRIDGE", "7733", (CC, DC))
    assert r.confidence == 1.0


def test_an_unknown_code_is_admitted_rather_than_guessed():
    r = check("ALDRIDGE", "9999", (CC,))
    assert r.verdict is Verdict.UNKNOWN
    assert r.confidence == 0.0
    assert not r.can_raise


def test_an_acquirer_with_no_policy_on_file_is_unknown():
    r = check("NOSUCHBANK", "7101", (CC,))
    assert r.verdict is Verdict.UNKNOWN
    assert not r.can_raise


def test_policies_cover_every_configured_acquirer():
    for code in ACQUIRERS:
        assert code in POLICIES, f"{code} has no eligibility policy"


def test_the_four_policy_shapes_are_all_represented():
    """The point of the dataset is that acquirers do not share a format."""
    shapes = {p.shape for p in POLICIES.values()}
    assert PolicyShape.CODED in shapes
    assert PolicyShape.CODED_BY_INSTRUMENT in shapes
    assert PolicyShape.CODED_WITH_PRODUCT_BAN in shapes
    assert PolicyShape.PROSE in shapes


# --- the policy read inside the intake check ----------------------------
#
# These exercise the phase 7 policy resolution reaching the intake check. How
# hard each outcome bites is the registry's job and is tested in
# tests/test_restrictions.py.


def _registry():
    from src.catalogue.restrictions import seed

    return seed()


def test_a_category_our_own_policy_refuses_raises_no_document_chase():
    """Sending BD after a PAN for a category nobody here raises starts a clock
    that can never finish."""
    result = check_intake(
        merchant(signatory_pan=None), list(ACQUIRERS.values()), mcc="7527",
        instruments=(CC, DC), registry=_registry(),
    )
    assert sorted(result.ineligible) == sorted(ACQUIRERS)
    assert result.bounces == []


def test_an_acquirers_own_refusal_still_chases_the_documents():
    """A bank's list is a fact about today, not a rule. The documents will be
    needed if the decision comes back yes."""
    result = check_intake(
        merchant(signatory_pan=None), list(ACQUIRERS.values()), mcc="7428",
        instruments=(CC, DC), registry=_registry(),
    )
    assert "NORTHWAY_CYBS" in result.awaiting_decision
    assert result.ineligible == []
    assert any("NORTHWAY_CYBS" in b.blocked_acquirers for b in result.bounces)


def test_partial_raise_still_works_alongside_a_pending_decision():
    result = check_intake(
        merchant(signatory_pan=None), list(ACQUIRERS.values()), mcc="7428",
        instruments=(CC, DC), registry=_registry(),
    )
    assert result.can_partially_raise
    assert "CASTERLY" in result.ready_now
    assert "awaiting an ops decision" in result.summary()


def test_conditions_are_surfaced_per_acquirer():
    result = check_intake(
        merchant(), list(ACQUIRERS.values()), mcc="7407", instruments=(CC, DC),
        registry=_registry(),
    )
    assert result.conditions
    for _code, condition in result.conditions:
        assert condition


def test_status_reads_plainly_for_each_outcome():
    result = check_intake(
        merchant(signatory_pan=None), list(ACQUIRERS.values()), mcc="7428",
        instruments=(CC, DC), registry=_registry(),
    )
    statuses = {a.status for a in result.per_acquirer}
    assert "blocked on documents" in statuses
    assert any("needs a decision" in s for s in statuses)


def test_a_clean_merchant_in_a_clean_category_is_ready_everywhere():
    result = check_intake(
        merchant(), list(ACQUIRERS.values()), mcc="7101", instruments=(CC, DC),
        registry=_registry(),
    )
    assert result.fully_ready
    assert not result.ineligible
    assert result.bounces == []


def test_intake_without_an_mcc_still_checks_documents():
    """Category assessment needs an MCC. Without one the check degrades to
    paperwork only rather than failing."""
    result = check_intake(
        merchant(signatory_pan=None), list(ACQUIRERS.values()), registry=_registry()
    )
    assert result.blocked
    assert all(a.assessment is None for a in result.per_acquirer)


def test_intake_without_a_registry_checks_documents_only():
    """A caller that does not hand over a registry gets no category answer at
    all, rather than a silent default that reads as a clean bill of health."""
    result = check_intake(
        merchant(), list(ACQUIRERS.values()), mcc="7428", instruments=(CC, DC)
    )
    assert all(a.assessment is None for a in result.per_acquirer)
    assert result.fully_ready


def test_the_mcc_on_the_merchant_record_is_ignored():
    """The MCC belongs to the request. A merchant-level one is at best one of
    several use cases, so it must not drive a policy answer."""
    result = check_intake(
        merchant(mcc="7428"), list(ACQUIRERS.values()), registry=_registry()
    )
    assert all(a.assessment is None for a in result.per_acquirer)
    assert result.ineligible == []
