"""
Tests for the bank reply parser.

Most of these are about what the parser must refuse to do. A row it declines
to apply costs someone a minute; a row it applies to the wrong request writes
a wrong event into the clock, and every TAT figure downstream inherits it with
nothing flagged.
"""

import pytest

from src.model import EventType, RejectionReason
from src.parsing.classify import classify, classify_outcome, classify_reason
from src.parsing.extract import parse_email
from src.parsing.match import AUTO_APPLY, RequestIndex, normalise, similarity
from src.parsing.pipeline import process_email

ACQUIRERS = {"ALDRIDGE", "BRINDLE", "CASTERLY", "VERRAN", "NORTHWAY_CYBS", "NORTHWAY_FSS"}


def mail(subject, body, sender="aldridge.ops@example-acquirer.test", attachment_name=None):
    head = f"From: {sender}\nTo: midops@example-pa.test\nSubject: {subject}\nDate: Mon, 14 Sep 2026 11:00:00 +0530\n"
    if attachment_name:
        head += f"Attachment: {attachment_name}\n"
    return head + "\n" + body


# --- extraction ----------------------------------------------------------


def test_pipe_table_in_the_body():
    body = """Dear Team,

Please find below the status of the MID applications received up to 14-Sep-2026.

Merchant Name            | MCC   | Status   | Remarks
------------------------------------------------------
Harrowgate Trading Pvt Ltd | 5399 | Approved |
Verdant Retail Limited     | 5812 | Rejected | Payment page not loading at our end

Remaining cases are under review and will follow.

Regards,"""
    p = parse_email("x.txt", mail("RE: MID Request | ALDRIDGE | Status Tracker", body))
    assert p.kind == "status"
    assert p.shape == "body_table"
    assert len(p.rows) == 2
    assert p.rows[0].name == "Harrowgate Trading Pvt Ltd"
    assert p.rows[1].mcc == "5812"


def test_numbered_list_in_the_body():
    body = """Hi,

Status update as on 14/09/2026:

1. NIRVAAN SOLUTIONS INDIA PVT. LTD. (MCC 5921) - REJECTED. Merchant falls under restricted business category as per our internal policy
2. Sahasra Traders Pvt. Ltd. (MCC 4722) - Approved

Balance cases pending at our end.

Thanks"""
    p = parse_email("x.txt", mail("RE: MID Request | VERRAN | Status Tracker", body, "verran.ops@example-acquirer.test"))
    assert p.shape == "body_list"
    assert len(p.rows) == 2
    assert p.rows[0].mcc == "5921"
    assert "restricted business category" in p.rows[0].remarks


def test_attached_csv_with_the_acquirers_own_column_names():
    csv_text = """LEGAL NAME,MCC CODE,APPLICATION STATUS,COMMENTS
Brightmoor Exports LLP,5691,APPROVED,
Ironwood Logistics Pvt Ltd,8299,ADDL DOCS REQUIRED,Board resolution awaited"""
    p = parse_email(
        "x.txt",
        mail("RE: MID Request | BRINDLE | Status Tracker", "Please refer to the attached tracker.", attachment_name="t.csv"),
        attachment=csv_text,
    )
    assert p.shape == "csv"
    assert len(p.rows) == 2
    assert p.rows[1].status == "ADDL DOCS REQUIRED"


def test_boilerplate_is_not_read_as_data():
    body = """Dear Team,

Merchant Name | MCC | Status | Remarks
--------------------------------------
Harrowgate Trading Pvt Ltd | 5399 | Approved |

Remaining cases are under review and will follow.

Regards,"""
    p = parse_email("x.txt", mail("RE: MID Request | ALDRIDGE | Status Tracker", body))
    assert len(p.rows) == 1


def test_an_unrecognised_layout_fails_loudly():
    """A bank that changes its format must produce a complaint, not silence.
    Silent zero rows would mean a week of missing updates nobody noticed."""
    body = "Dear Team,\n\nSome prose with no table and no numbered list at all.\n\nRegards,"
    p = parse_email("x.txt", mail("RE: MID Request | ALDRIDGE | Status Tracker", body))
    assert p.rows == []
    assert p.problems


def test_credential_email_is_a_separate_stream():
    body = """Dear Team,

Please find the credentials for the approved cases.

Merchant : Harrowgate Trading Private Limited
MCC      : 5399
MID      : ALD1234567890
Encryption Key : abc123
Working Key    : def456

Please confirm once configured.

Regards,"""
    p = parse_email("x.txt", mail("MID Credentials | ALDRIDGE | 14-Sep-2026", body))
    assert p.kind == "mid"
    assert len(p.mids) == 1
    assert p.mids[0]["mid"] == "ALD1234567890"
    assert p.mids[0]["mcc"] == "5399"


# --- classification ------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Approved", EventType.ACQUIRER_APPROVED),
        ("APPROVED", EventType.ACQUIRER_APPROVED),
        ("Cleared", EventType.ACQUIRER_APPROVED),
        ("Approved - proceed", EventType.ACQUIRER_APPROVED),
        ("Rejected", EventType.ACQUIRER_REJECTED),
        ("Declined", EventType.ACQUIRER_REJECTED),
        ("Rejected by risk", EventType.ACQUIRER_REJECTED),
        ("Additional documents required", EventType.ACQUIRER_REQUESTED_DOCS),
        ("ADDL DOCS REQUIRED", EventType.ACQUIRER_REQUESTED_DOCS),
        ("Pending - docs awaited", EventType.ACQUIRER_REQUESTED_DOCS),
        ("Clarification required", EventType.ACQUIRER_REQUESTED_CLARIFICATION),
        ("Query raised", EventType.ACQUIRER_REQUESTED_CLARIFICATION),
    ],
)
def test_status_wordings_map_to_the_right_outcome(text, expected):
    outcome, confidence, _ = classify_outcome(text)
    assert outcome is expected
    assert confidence > 0.8


def test_not_approved_is_a_rejection_not_an_approval():
    """The single most dangerous string in the inbox. A substring test reads
    it as approved, stops the wrong clock, and marks a declined merchant as
    won."""
    outcome, confidence, _ = classify_outcome("Not Approved")
    assert outcome is EventType.ACQUIRER_REJECTED
    assert confidence > 0.9


def test_unknown_wording_is_admitted_rather_than_guessed():
    outcome, confidence, why = classify_outcome("Under examination by committee")
    assert outcome is None
    assert confidence == 0.0
    assert "no known status wording" in why


def test_empty_status_is_not_an_outcome():
    assert classify_outcome("")[0] is None


@pytest.mark.parametrize(
    "remark,expected",
    [
        ("Merchant falls under restricted business category as per our internal policy", RejectionReason.RESTRICTED_CATEGORY),
        ("Requested MCC is in the negative list", RejectionReason.RESTRICTED_MCC),
        ("Website does not appear genuine, no company details visible", RejectionReason.WEBSITE_NOT_LEGITIMATE),
        ("Checkout journey unclear, please share a demo", RejectionReason.UNCLEAR_CUSTOMER_JOURNEY),
        ("Multiple business lines on one site, single MCC cannot be assigned", RejectionReason.MULTIPLE_USE_CASES),
        ("Payment page not loading at our end", RejectionReason.PAYMENT_FLOW_BROKEN),
        ("MCC does not match the business as seen on the website", RejectionReason.INCORRECT_MCC),
        ("Name on PAN does not match the entity name provided", RejectionReason.DOCUMENT_DISCREPANCY),
    ],
)
def test_free_text_reasons_land_in_the_right_bucket(remark, expected):
    reason, confidence, _ = classify_reason(remark)
    assert reason is expected
    assert confidence >= 0.8


def test_an_unplaceable_reason_scores_zero_rather_than_guessing():
    reason, confidence, _ = classify_reason("Internal policy decision, no further comment")
    assert reason is RejectionReason.OTHER
    assert confidence == 0.0


def test_a_reason_is_only_read_for_a_rejection():
    c = classify("Approved", "Payment page not loading at our end")
    assert c.outcome is EventType.ACQUIRER_APPROVED
    assert c.reason is None


# --- matching ------------------------------------------------------------


def test_suffix_forms_normalise_to_the_same_entity():
    forms = [
        "Harrowgate Trading Private Limited",
        "Harrowgate Trading Pvt Ltd",
        "Harrowgate Trading Pvt. Ltd.",
        "HARROWGATE TRADING PRIVATE LIMITED",
    ]
    assert len({normalise(f) for f in forms}) == 1


def test_india_is_part_of_the_name_not_noise():
    """These are two different entities. Stripping 'India' merges them, and
    the matcher then joins a reply to the wrong one at full confidence."""
    a = "Oakhurst Ventures India Private Limited"
    b = "Oakhurst Ventures Private Limited"
    assert normalise(a) != normalise(b)
    assert similarity(a, b) < 1.0


def test_a_typo_still_scores_high():
    assert similarity("Thornbury Exports Limtied", "Thornbury Exports Limited") > 0.9


def test_different_merchants_score_low():
    assert similarity("Harrowgate Trading Pvt Ltd", "Verdant Retail Limited") < 0.6


def index_of(*rows):
    return RequestIndex(
        [
            {"request_id": r[0], "name": r[1], "mcc": r[2], "acquirer": r[3], "open": True}
            for r in rows
        ]
    )


def test_mcc_separates_two_use_cases_of_one_merchant():
    """One merchant with two MCCs has two live applications at the same
    acquirer. Matching on name alone would stop the wrong clock."""
    idx = index_of(
        ("R-TRAVEL", "Prabhat Ventures Limited", "4722", "ALDRIDGE"),
        ("R-RETAIL", "Prabhat Ventures Limited", "5399", "ALDRIDGE"),
    )
    m = idx.match("ALDRIDGE", "Prabhat Ventures Ltd", "5399")
    assert m.request_id == "R-RETAIL"
    assert m.auto


def test_two_similar_merchants_go_to_review_rather_than_a_coin_toss():
    idx = index_of(
        ("R-1", "Silverline Trading Private Limited", "5399", "ALDRIDGE"),
        ("R-2", "Silverline Trading Pvt Ltd", "5399", "ALDRIDGE"),
    )
    m = idx.match("ALDRIDGE", "Silverline Trading Limited", "5399")
    assert m.request_id is None
    assert not m.auto
    assert "ambiguous" in m.reason


def test_a_weak_name_match_is_not_applied_automatically():
    idx = index_of(("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"))
    m = idx.match("ALDRIDGE", "Harrogate Tradng Co", "5399")
    assert not m.auto


def test_a_match_without_mcc_support_is_discounted():
    """Falling back to the acquirer's whole open book is a weaker join, so it
    has to clear a higher bar before it is trusted."""
    idx = index_of(("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"))
    with_mcc = idx.match("ALDRIDGE", "Harrowgate Trading Pvt Ltd", "5399")
    without = idx.match("ALDRIDGE", "Harrowgate Trading Pvt Ltd", "")
    assert without.confidence < with_mcc.confidence


def test_a_reply_for_an_unknown_acquirer_matches_nothing():
    idx = index_of(("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"))
    m = idx.match("CASTERLY", "Harrowgate Trading Pvt Ltd", "5399")
    assert m.request_id is None


def test_auto_apply_bar_is_high():
    assert AUTO_APPLY >= 0.85


# --- the pipeline's decision ---------------------------------------------


def run(body, index, subject="RE: MID Request | ALDRIDGE | Status Tracker", **kw):
    return process_email("x.txt", mail(subject, body, **kw), kw.get("attachment"), index, ACQUIRERS)


def test_a_confident_row_is_applied_immediately():
    idx = index_of(("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"))
    body = """Dear Team,

Merchant Name | MCC | Status | Remarks
--------------------------------------
Harrowgate Trading Pvt Ltd | 5399 | Approved |

Regards,"""
    updates, _ = run(body, idx)
    assert len(updates) == 1
    assert updates[0].applied
    assert updates[0].outcome is EventType.ACQUIRER_APPROVED


def test_an_unreadable_status_sends_the_row_to_review():
    idx = index_of(("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"))
    body = """Dear Team,

Merchant Name | MCC | Status | Remarks
--------------------------------------
Harrowgate Trading Pvt Ltd | 5399 | Under committee review |

Regards,"""
    updates, _ = run(body, idx)
    assert not updates[0].applied
    assert "status wording" in updates[0].review_reason


def test_confident_rows_are_not_held_back_by_doubtful_ones():
    """Thirty-five correct updates should not wait on five uncertain ones.
    Those clocks stop now."""
    idx = index_of(
        ("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"),
        ("R-2", "Verdant Retail Limited", "5812", "ALDRIDGE"),
    )
    body = """Dear Team,

Merchant Name | MCC | Status | Remarks
--------------------------------------
Harrowgate Trading Pvt Ltd | 5399 | Approved |
Verdant Retail Ltd | 5812 | Under committee review |

Regards,"""
    updates, _ = run(body, idx)
    assert len(updates) == 2
    assert sum(u.applied for u in updates) == 1
    assert sum(not u.applied for u in updates) == 1


def test_a_rejection_applies_even_when_its_reason_is_unclear():
    """The decision is certain, so the clock stops. The reason is flagged,
    because a mis-bucketed reason corrupts the report ops relies on."""
    idx = index_of(("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"))
    body = """Dear Team,

Merchant Name | MCC | Status | Remarks
--------------------------------------
Harrowgate Trading Pvt Ltd | 5399 | Rejected | Internal policy decision

Regards,"""
    updates, _ = run(body, idx)
    u = updates[0]
    assert u.applied
    assert u.outcome is EventType.ACQUIRER_REJECTED
    assert "reason needs confirming" in u.review_reason


def test_two_rails_of_one_bank_are_never_confused():
    """Northway runs two processing platforms with separate applications and
    separate clocks. A partial hint must not be enough to pick between them."""
    idx = index_of(
        ("R-CYBS", "Harrowgate Trading Private Limited", "5399", "NORTHWAY_CYBS"),
        ("R-FSS", "Harrowgate Trading Private Limited", "5399", "NORTHWAY_FSS"),
    )
    body = """Dear Team,

Merchant Name | MCC | Status | Remarks
--------------------------------------
Harrowgate Trading Pvt Ltd | 5399 | Approved |

Regards,"""
    updates, problems = process_email(
        "x.txt",
        mail("RE: MID Request | Status Tracker", body, "northway.ops@example-acquirer.test"),
        None,
        idx,
        ACQUIRERS,
    )
    assert updates == []
    assert problems


def test_credential_email_yields_the_mid():
    idx = index_of(("R-1", "Harrowgate Trading Private Limited", "5399", "ALDRIDGE"))
    body = """Dear Team,

Merchant : Harrowgate Trading Pvt Ltd
MCC      : 5399
MID      : ALD9988776655
Encryption Key : abc

Regards,"""
    updates, _ = run(body, idx, subject="MID Credentials | ALDRIDGE | 14-Sep-2026")
    assert len(updates) == 1
    assert updates[0].applied
    assert updates[0].mid == "ALD9988776655"
    assert updates[0].outcome is EventType.MID_RECEIVED
