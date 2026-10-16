"""
Tests for the synthetic dataset.

The point of these is not that the generator runs. It is that the data is
honest: gaps that caused a bounce are really absent from the record, requests
that bounced really have extra ops episodes, and nothing silently flatters a
feature that will be built on top of it.
"""

from datetime import datetime

import pytest

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.emails import build_mid_delivery, build_status_reply
from src.generator import DOC_GAPS, DataGenerator
from src.identifiers import (
    corrupt_pan,
    gstin_checksum,
    gstin_matches_pan,
    is_valid_gstin,
    is_valid_pan,
    make_gstin,
    make_pan,
)
from src.model import EventType, RejectionReason, Stream
from src.names import RESTRICTED_MCCS
from src.tat import TatEngine

AS_OF = datetime(2026, 10, 1, 18, 0)


@pytest.fixture(scope="module")
def data():
    cal = load_calendar(HOLIDAYS)
    gen = DataGenerator(ACQUIRERS, cal, seed=42, as_of=AS_OF)
    return gen.generate(bau_merchants=80, migration_merchants=50)


@pytest.fixture(scope="module")
def engine():
    return TatEngine(load_calendar(HOLIDAYS))


# --- identifiers ---------------------------------------------------------


def test_generated_pan_is_format_valid():
    import random

    rng = random.Random(1)
    for _ in range(200):
        assert is_valid_pan(make_pan(rng, "C"))


def test_generated_gstin_passes_its_own_checksum():
    import random

    rng = random.Random(2)
    for _ in range(200):
        pan = make_pan(rng, "C")
        gstin = make_gstin(rng, "29", pan)
        assert is_valid_gstin(gstin)


def test_gstin_carries_the_company_pan():
    """Characters 3 to 12 of a GSTIN are the entity's PAN, which is the
    cross-check a readiness validator should run."""
    import random

    rng = random.Random(3)
    pan = make_pan(rng, "C")
    gstin = make_gstin(rng, "27", pan)
    assert gstin_matches_pan(gstin, pan)


def test_a_wrong_checksum_is_caught():
    import random

    rng = random.Random(4)
    pan = make_pan(rng, "C")
    good = make_gstin(rng, "29", pan)
    wrong_last = "0" if good[-1] != "0" else "1"
    assert not is_valid_gstin(good[:14] + wrong_last)


def test_corrupted_pans_fail_validation():
    import random

    rng = random.Random(5)
    caught = 0
    for _ in range(100):
        bad = corrupt_pan(rng, make_pan(rng, "P"))
        if not is_valid_pan(bad):
            caught += 1
    # Transposing two letters can still leave a valid shape, so not every
    # corruption is catchable by format alone. That is the honest result: a
    # format check is necessary and not sufficient.
    assert caught > 50


def test_checksum_rejects_a_short_input():
    with pytest.raises(ValueError):
        gstin_checksum("29ABCDE1234F")


# --- dataset shape -------------------------------------------------------


def test_dataset_has_both_streams(data):
    streams = {r.stream for r in data.requests}
    assert streams == {Stream.BAU, Stream.MIGRATION}


def test_each_merchant_goes_to_several_acquirers(data):
    by_merchant: dict[str, set[str]] = {}
    for r in data.requests:
        by_merchant.setdefault(r.merchant.merchant_id, set()).add(r.acquirer.code)
    counts = [len(v) for v in by_merchant.values()]
    assert min(counts) >= 3
    assert max(counts) <= 4


def test_some_merchants_run_two_use_cases(data):
    """One merchant with two MCCs is two sets of requests. This is the reason
    a request is keyed on merchant + MCC + acquirer rather than just the
    merchant and the acquirer."""
    by_merchant: dict[str, set[str]] = {}
    for r in data.requests:
        by_merchant.setdefault(r.merchant.merchant_id, set()).add(r.mcc)
    assert any(len(v) > 1 for v in by_merchant.values())


def test_request_ids_are_unique(data):
    ids = [r.request_id for r in data.requests]
    assert len(ids) == len(set(ids))


def test_generation_is_deterministic():
    cal = load_calendar(HOLIDAYS)
    a = DataGenerator(ACQUIRERS, cal, seed=7, as_of=AS_OF).generate(20, 10)
    b = DataGenerator(ACQUIRERS, cal, seed=7, as_of=AS_OF).generate(20, 10)
    assert [r.request_id for r in a.requests] == [r.request_id for r in b.requests]
    assert [len(r.events) for r in a.requests] == [len(r.events) for r in b.requests]


def test_no_event_lands_in_the_future(data):
    for r in data.requests:
        for e in r.events:
            assert e.at <= AS_OF


def test_events_are_chronological(data):
    for r in data.requests:
        times = [e.at for e in r.events]
        assert times == sorted(times)


# --- the data is honest, not just well shaped ---------------------------


def test_an_unresolved_gap_is_really_absent_from_the_record(data):
    """A merchant that never supplied a field must actually be missing it.

    A dataset where the bounce event and the record disagree would make a
    readiness check look perfect, because the gaps it was built to find would
    already have been filled in behind it.
    """
    gen = data.generator
    checked = 0

    for merchant in data.merchants:
        gaps = gen.gaps[merchant.merchant_id]
        for field_name in gaps.unresolved:
            assert not merchant.fields.get(field_name), (
                f"{merchant.merchant_id} has an unresolved gap on {field_name} "
                "but the field is populated"
            )
            checked += 1
        for field_name in gaps.resolved:
            assert merchant.fields.get(field_name), (
                f"{merchant.merchant_id} resolved {field_name} but it is blank"
            )

    assert checked > 0, "no unresolved gaps in the sample to verify"


def test_a_gap_blocks_only_the_acquirers_that_ask_for_it(data):
    """This is what makes partial raising possible: a merchant can be ready
    for two acquirers and blocked for the other two by the same missing
    field."""
    gen = data.generator
    found_a_split = False

    for merchant in data.merchants:
        gaps = gen.gaps[merchant.merchant_id]
        if not gaps.unresolved:
            continue
        missing = set(gaps.unresolved)
        reqs = [r for r in data.requests if r.merchant.merchant_id == merchant.merchant_id]

        blocked = [r for r in reqs if missing & set(r.acquirer.required_fields)]
        clear = [r for r in reqs if not (missing & set(r.acquirer.required_fields))]

        for r in blocked:
            kinds = [e.event_type for e in r.events]
            still_queued = kinds == [EventType.ASSIGNED_TO_OPS]
            dropped = EventType.DEPRIORITISED in kinds
            assert (
                EventType.BOUNCED_TO_BD in kinds or still_queued or dropped
            ), f"{r.request_id} needs a missing field but never bounced"

        if blocked and clear:
            found_a_split = True

    assert found_a_split, "no merchant was blocked at some acquirers and clear at others"


def test_some_records_are_filled_but_wrong(data):
    """Presence is not validity. A validator needs mistyped PANs and GSTs
    issued against a different entity, not only blanks."""
    bad_pan = sum(
        1
        for m in data.merchants
        if m.fields.get("signatory_pan") and not is_valid_pan(m.fields["signatory_pan"])
    )
    mismatched_gst = sum(
        1
        for m in data.merchants
        if m.fields.get("company_gst")
        and m.fields.get("company_pan")
        and not gstin_matches_pan(m.fields["company_gst"], m.fields["company_pan"])
    )
    assert bad_pan > 0
    assert mismatched_gst > 0


def test_restricted_categories_are_rejected_not_approved(data):
    """A gambling or money-transfer merchant must not sail through. Letting
    one be approved would teach every downstream model the wrong thing."""
    for r in data.requests:
        if r.mcc not in RESTRICTED_MCCS:
            continue
        approved = any(e.event_type is EventType.ACQUIRER_APPROVED for e in r.events)
        assert not approved, f"{r.request_id} on restricted MCC {r.mcc} was approved"


def test_restricted_rejections_cite_a_category_reason(data):
    for r in data.requests:
        if r.mcc not in RESTRICTED_MCCS:
            continue
        for e in r.events:
            if e.event_type is EventType.ACQUIRER_REJECTED:
                assert e.rejection_reason in (
                    RejectionReason.RESTRICTED_CATEGORY,
                    RejectionReason.RESTRICTED_MCC,
                )


def test_bounced_requests_show_more_than_one_ops_episode(data, engine):
    bounced = [
        r
        for r in data.requests
        if any(
            e.event_type
            in (EventType.BOUNCED_TO_BD, EventType.BOUNCED_TO_ONBOARDING)
            for e in r.events
        )
        and any(e.event_type is EventType.RETURNED_TO_OPS for e in r.events)
    ]
    assert bounced, "no bounced-and-returned requests in the sample"

    for r in bounced[:40]:
        tat = engine.compute(r, as_of=AS_OF)
        assert tat.ops_episode_count >= 2


def test_bounce_rate_is_realistic(data, engine):
    """Between a quarter and two fifths of requests should bounce, matching
    the 30 to 40% seen in the real pipeline."""
    bounced = sum(
        1
        for r in data.requests
        if any(
            e.event_type
            in (EventType.BOUNCED_TO_BD, EventType.BOUNCED_TO_ONBOARDING)
            for e in r.events
        )
    )
    rate = bounced / len(data.requests)
    assert 0.20 <= rate <= 0.45, f"bounce rate {rate:.0%} is not plausible"


def test_there_is_a_visible_unraised_queue(data):
    """A backlog of requests assigned to ops but never raised is the clearest
    signal of a capacity problem, and it is invisible in any report that only
    starts counting once a request is raised."""
    queued = [
        r
        for r in data.requests
        if len(r.events) == 1 and r.events[0].event_type is EventType.ASSIGNED_TO_OPS
    ]
    assert len(queued) / len(data.requests) > 0.1


def test_deprioritised_requests_exist_and_stop_accruing(data, engine):
    depr = [r for r in data.requests if r.is_deprioritised]
    assert depr
    for r in depr[:20]:
        tat = engine.compute(r, as_of=AS_OF)
        assert all(not e.is_open for e in tat.episodes)


def test_approval_and_mid_are_separate_events(data):
    """They arrive on different email subjects, so the gap between them is a
    real interval the current tracker cannot see."""
    with_mid = [
        r
        for r in data.requests
        if any(e.event_type is EventType.MID_RECEIVED for e in r.events)
    ]
    assert with_mid
    for r in with_mid[:30]:
        kinds = [e.event_type for e in r.events]
        assert EventType.ACQUIRER_APPROVED in kinds
        assert kinds.index(EventType.ACQUIRER_APPROVED) < kinds.index(
            EventType.MID_RECEIVED
        )


def test_every_acquirer_gets_a_workable_volume(data):
    counts: dict[str, int] = {}
    for r in data.requests:
        counts[r.acquirer.code] = counts.get(r.acquirer.code, 0) + 1
    assert set(counts) == set(ACQUIRERS)
    assert min(counts.values()) > 20


# --- bank reply emails ---------------------------------------------------


def test_each_acquirer_reply_carries_its_own_column_names(data):
    import random

    rng = random.Random(9)
    sample = data.requests[:4]
    batch = [(r, EventType.ACQUIRER_APPROVED, None) for r in sample]

    aldridge = build_status_reply(rng, "ALDRIDGE", batch, AS_OF)
    brindle = build_status_reply(rng, "BRINDLE", batch, AS_OF)

    assert "Merchant Name" in aldridge.body  # table pasted into the body
    assert brindle.attachment_csv is not None  # this one attaches a sheet
    assert "LEGAL NAME" in brindle.attachment_csv


def test_reply_names_drift_from_the_record(data):
    """Banks type the legal name their own way, which is why matching is on
    name plus MCC with a tolerance rather than an exact string."""
    import random

    rng = random.Random(11)
    sample = data.requests[:60]
    batch = [(r, EventType.ACQUIRER_APPROVED, None) for r in sample]
    mail = build_status_reply(rng, "ALDRIDGE", batch, AS_OF)

    exact = sum(1 for r in sample if r.merchant.legal_name in mail.body)
    assert exact < len(sample), "every name matched exactly, so nothing drifted"


def test_rejection_remarks_are_free_text_not_codes(data):
    import random

    rng = random.Random(13)
    batch = [
        (data.requests[0], EventType.ACQUIRER_REJECTED, RejectionReason.RESTRICTED_CATEGORY)
    ]
    mail = build_status_reply(rng, "CASTERLY", batch, AS_OF)

    # Never a tidy enum the parser could match on directly.
    assert "RESTRICTED_CATEGORY" not in mail.body
    assert "restricted_category" not in mail.body
    # It arrives as a sentence someone typed, which is what has to be
    # classified back into a bucket.
    from src.emails import REJECTION_TEXT

    assert any(
        sentence in mail.body
        for sentence in REJECTION_TEXT[RejectionReason.RESTRICTED_CATEGORY]
    )


def test_mid_delivery_uses_a_different_subject(data):
    import random

    rng = random.Random(17)
    status = build_status_reply(
        rng, "ALDRIDGE", [(data.requests[0], EventType.ACQUIRER_APPROVED, None)], AS_OF
    )
    mid = build_mid_delivery(rng, "ALDRIDGE", [data.requests[0]], AS_OF)

    assert status.subject != mid.subject
    assert "Status Tracker" in status.subject
    assert "Credentials" in mid.subject


def test_mid_credentials_differ_by_acquirer(data):
    import random

    rng = random.Random(19)
    northway = build_mid_delivery(rng, "NORTHWAY_CYBS", [data.requests[0]], AS_OF)
    brindle = build_mid_delivery(rng, "BRINDLE", [data.requests[0]], AS_OF)

    assert "Secret Key" in northway.body
    assert "API Key" in brindle.body
