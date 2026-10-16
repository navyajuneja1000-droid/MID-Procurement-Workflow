"""
Tests for the intake readiness check.

The behaviour worth protecting is not that it finds blanks. It is that
readiness is decided per acquirer, that a record can be complete and still
wrong, and that the ticket goes back once with everything on it rather than
once per acquirer that wanted the same field.
"""

import pytest

from config.acquirers import ACQUIRERS
from src.identifiers import make_gstin, make_pan
from src.intake.readiness import check_intake
from src.intake.validate import (
    MISSING_REASON,
    check_consistency,
    check_format,
    check_presence,
    validate,
)
from src.model import Acquirer, BounceReason, Merchant, Party, PriorityTier, Segment

GOOD_PAN = "AABCH1234K"
GOOD_GST = "29AABCH1234K1ZN"  # carries GOOD_PAN, Karnataka; check digit computed


def merchant(**overrides) -> Merchant:
    fields = {
        "entity_legal_name": "Harrowgate Trading Private Limited",
        "company_pan": GOOD_PAN,
        "company_gst": GOOD_GST,
        "registered_address": "14 Mill Road, Bengaluru 560001",
        "registration_number": "U51909KA2019PTC123456",
        "signatory_name": "Meera Iyer",
        "signatory_pan": "ABCPI9876L",
        "signatory_address": "8 Church Street, Bengaluru 560001",
        "website": "https://harrowgate.test",
        "state": "Karnataka",
        "mcc": "7101",  # catalogue code, eligible everywhere
    }
    for k, v in overrides.items():
        if v is None:
            fields.pop(k, None)
        else:
            fields[k] = v
    return Merchant(
        merchant_id="M-TEST",
        legal_name=fields["entity_legal_name"],
        segment=Segment.SME,
        priority_tier=PriorityTier.P3,
        website=fields.get("website"),
        fields=fields,
    )


def test_a_complete_record_is_ready_everywhere():
    result = check_intake(merchant(), list(ACQUIRERS.values()), mcc="7101")
    assert result.fully_ready
    assert result.bounces == []


def test_gstin_fixture_is_actually_valid():
    """If the fixture were invalid every other test would pass for the wrong
    reason."""
    from src.identifiers import gstin_matches_pan, is_valid_gstin

    assert is_valid_gstin(GOOD_GST)
    assert gstin_matches_pan(GOOD_GST, GOOD_PAN)


# --- presence ------------------------------------------------------------


def test_a_missing_field_is_found():
    findings = check_presence({"company_pan": ""}, ["company_pan"])
    assert len(findings) == 1
    assert findings[0].reason is BounceReason.MISSING_COMPANY_PAN
    assert findings[0].level == "missing"


def test_whitespace_is_not_a_value():
    assert check_presence({"company_pan": "   "}, ["company_pan"])


def test_an_unroutable_required_field_fails_loudly():
    """A field with no reason mapped has no routing decision behind it.
    Guessing one would send tickets to a team that cannot act on them."""
    with pytest.raises(KeyError, match="cannot be routed"):
        check_presence({}, ["some_new_field"])


def test_every_required_field_across_acquirers_can_be_routed():
    for acq in ACQUIRERS.values():
        for name in acq.required_fields:
            assert name in MISSING_REASON, f"{acq.code} requires unroutable '{name}'"


# --- format --------------------------------------------------------------


def test_a_malformed_pan_is_caught_even_though_it_is_present():
    findings = check_format({"company_pan": "AABC1234K"})
    assert findings
    assert findings[0].reason is BounceReason.INVALID_PAN_FORMAT
    assert findings[0].level == "format"


def test_a_gstin_with_a_bad_check_digit_is_caught():
    bad = GOOD_GST[:14] + ("0" if GOOD_GST[14] != "0" else "1")
    findings = check_format({"company_gst": bad})
    assert findings
    assert findings[0].reason is BounceReason.INVALID_GST_FORMAT


def test_a_blank_field_is_not_also_a_format_error():
    """A missing field should raise one finding, not two. Two findings on one
    field would double-count it in the bounce comment."""
    assert check_format({"company_pan": ""}) == []


# --- consistency ---------------------------------------------------------


def test_a_gst_issued_against_another_entity_is_caught():
    """The record is complete and every field is well formed, which is exactly
    why only a cross-check finds this. An acquirer's underwriter would."""
    import random

    rng = random.Random(1)
    other = make_gstin(rng, "29", make_pan(rng, "C"))
    findings = check_consistency({"company_gst": other, "company_pan": GOOD_PAN})
    assert findings
    assert findings[0].reason is BounceReason.PAN_GST_MISMATCH
    assert findings[0].level == "consistency"


def test_a_gst_from_the_wrong_state_is_caught():
    findings = check_consistency(
        {"company_gst": GOOD_GST, "company_pan": GOOD_PAN, "state": "Maharashtra"}
    )
    assert any("state code" in f.detail for f in findings)


def test_a_consistent_record_raises_nothing():
    assert (
        check_consistency(
            {"company_gst": GOOD_GST, "company_pan": GOOD_PAN, "state": "Karnataka"}
        )
        == []
    )


def test_consistency_is_not_checked_on_a_malformed_value():
    """A GST that fails its own checksum should be reported as a format
    problem once, not also as a mismatch. Two findings for one bad value
    overstates how broken the record is."""
    bad = "29AABCH1234K1Z0"  # same body, wrong check digit
    out = check_consistency({"company_gst": bad, "company_pan": GOOD_PAN})
    assert out == []


# --- readiness per acquirer ---------------------------------------------


def one_acquirer(code: str, fields: list[str]) -> Acquirer:
    return Acquirer(code=code, name=code.title(), status_sla_days=5, mid_sla_days=3, required_fields=fields)


def test_a_gap_blocks_only_the_acquirers_that_ask_for_the_field():
    """This is what makes partial raising possible, and it is the main reason
    readiness is decided per acquirer rather than per merchant."""
    wants_gst = one_acquirer("WANTS", ["entity_legal_name", "company_gst"])
    ignores_gst = one_acquirer("IGNORES", ["entity_legal_name", "company_pan"])

    result = check_intake(merchant(company_gst=None), [wants_gst, ignores_gst])

    assert result.ready_now == ["IGNORES"]
    assert result.blocked == ["WANTS"]
    assert result.can_partially_raise


def test_the_summary_names_what_can_go_out_today():
    wants_gst = one_acquirer("WANTS", ["company_gst"])
    ignores_gst = one_acquirer("IGNORES", ["company_pan"])
    result = check_intake(merchant(company_gst=None), [wants_gst, ignores_gst])
    assert "Raise to 1 of 2 now" in result.summary()


def test_a_merchant_blocked_everywhere_is_not_a_partial_raise():
    a = one_acquirer("A", ["company_gst"])
    b = one_acquirer("B", ["company_gst"])
    result = check_intake(merchant(company_gst=None), [a, b])
    assert result.fully_blocked
    assert not result.can_partially_raise


# --- the bounce ----------------------------------------------------------


def test_one_field_blocking_three_acquirers_bounces_once():
    """The ticket goes back once with everything on it. Bouncing per acquirer
    would send the same missing field to the same team three times and start
    three clocks for one problem."""
    acqs = [one_acquirer(c, ["company_gst"]) for c in ("A", "B", "C")]
    result = check_intake(merchant(company_gst=None), acqs)

    assert len(result.bounces) == 1
    bounce = result.bounces[0]
    assert bounce.fields == ["company_gst"]
    assert bounce.blocked_acquirers == ["A", "B", "C"]


def test_documents_go_to_bd_and_analysis_goes_to_onboarding():
    acq = one_acquirer("A", ["signatory_pan", "mcc"])
    result = check_intake(merchant(signatory_pan=None, mcc=None), [acq])

    parties = {b.party for b in result.bounces}
    assert parties == {Party.BD, Party.ONBOARDING}

    bd = next(b for b in result.bounces if b.party is Party.BD)
    onb = next(b for b in result.bounces if b.party is Party.ONBOARDING)
    assert bd.fields == ["signatory_pan"]
    assert onb.fields == ["mcc"]


def test_the_bounce_comment_names_the_field_and_the_consequence():
    """'Missing PAN' without the consequence does not tell the receiving team
    how urgent it is."""
    acqs = [one_acquirer(c, ["signatory_pan"]) for c in ("ALPHA", "BETA")]
    result = check_intake(merchant(signatory_pan=None), acqs)
    comment = result.bounces[0].comment

    assert "Authorised signatory PAN" in comment
    assert "not provided" in comment
    assert "blocks ALPHA, BETA" in comment


def test_label_capitalisation_is_preserved():
    acq = one_acquirer("A", ["company_gst"])
    result = check_intake(merchant(company_gst=None), [acq])
    assert "Company GST" in result.bounces[0].comment


# --- validate, end to end -----------------------------------------------


def test_format_problems_are_reported_beyond_the_required_list():
    """A bad PAN is a bad PAN whether or not this acquirer asks for it.
    Catching it now saves the next acquirer a full cycle."""
    fields = dict(merchant(signatory_pan="NOTAPAN1").fields)
    findings = validate(fields, ["entity_legal_name"])
    assert any(f.reason is BounceReason.INVALID_PAN_FORMAT for f in findings)


def test_a_missing_field_is_reported_once_not_twice():
    fields = dict(merchant(company_pan=None).fields)
    findings = validate(fields, ["company_pan"])
    assert len([f for f in findings if f.field == "company_pan"]) == 1
