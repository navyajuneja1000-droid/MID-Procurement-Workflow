"""
Field-level validation for a merchant record.

Presence is not validity. A field can be filled and still be wrong, and a
wrong PAN costs a full acquirer cycle to discover: the application goes out,
sits in the bank's queue for a week, and comes back as a document discrepancy.

Three levels of check, in increasing order of what they catch:

1. **Present.** The field has something in it.
2. **Well formed.** It matches the shape the identifier actually has.
3. **Consistent.** It agrees with the other fields on the record. A GSTIN
   carries the entity's own PAN at characters 3 to 12, and its first two
   characters are the state it was issued in, so both can be checked against
   what the record says elsewhere.

Level 3 is the one worth having. A record can pass presence and format checks
on every field and still be internally contradictory, and that contradiction
is what an acquirer's underwriter spots.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..identifiers import gstin_matches_pan, is_valid_gstin, is_valid_pan
from ..model import BounceReason
from ..names import STATES

# Field -> the bounce reason raised when it is missing. The reason decides
# which team the ticket goes back to, so an entry here is a routing decision,
# not a label: documents the merchant must supply go to BD, analysis and
# record-keeping go to onboarding.
MISSING_REASON: dict[str, BounceReason] = {
    "company_pan": BounceReason.MISSING_COMPANY_PAN,
    "company_gst": BounceReason.MISSING_COMPANY_GST,
    "signatory_pan": BounceReason.MISSING_SIGNATORY_PAN,
    "signatory_address": BounceReason.MISSING_SIGNATORY_ADDRESS,
    "registration_number": BounceReason.MISSING_REGISTRATION_NUMBER,
    # Onboarding's work, not the merchant's.
    "mcc": BounceReason.MCC_MISSING,
    "website": BounceReason.WEBSITE_CHECK_INCOMPLETE,
    "entity_legal_name": BounceReason.WEBSITE_CHECK_INCOMPLETE,
    "registered_address": BounceReason.MISSING_SIGNATORY_ADDRESS,
    "signatory_name": BounceReason.MISSING_SIGNATORY_ADDRESS,
}

STATE_CODES = {name: code for name, code, _city, _pin in STATES}

LABEL = {
    "entity_legal_name": "entity legal name",
    "company_pan": "company PAN",
    "company_gst": "company GST",
    "registered_address": "registered address",
    "registration_number": "registration number",
    "signatory_name": "authorised signatory name",
    "signatory_pan": "authorised signatory PAN",
    "signatory_address": "authorised signatory address",
    "website": "website",
    "mcc": "MCC",
}


def label_for(field: str) -> str:
    return LABEL.get(field, field.replace("_", " "))


@dataclass
class Finding:
    """One thing wrong with the record."""

    field: str
    reason: BounceReason
    detail: str
    level: str  # "missing" | "format" | "consistency"

    @property
    def message(self) -> str:
        # Upper-case the first letter only. `.capitalize()` would lower the
        # rest and turn "company GST" into "Company gst".
        label = label_for(self.field)
        return f"{label[:1].upper()}{label[1:]}: {self.detail}"


def check_presence(fields: dict[str, str], required: list[str]) -> list[Finding]:
    out = []
    for name in required:
        if not (fields.get(name) or "").strip():
            if name not in MISSING_REASON:
                # No silent default. A field with no reason mapped has no
                # routing decision behind it, and guessing one would send
                # tickets to a team that cannot act on them. Adding a required
                # field means deciding who owns it.
                raise KeyError(
                    f"'{name}' is required by an acquirer but has no bounce "
                    "reason in MISSING_REASON, so it cannot be routed"
                )
            out.append(
                Finding(
                    field=name,
                    reason=MISSING_REASON[name],
                    detail="not provided",
                    level="missing",
                )
            )
    return out


def check_format(fields: dict[str, str]) -> list[Finding]:
    """Shape checks on the identifiers that have a defined shape."""
    out = []

    for name in ("company_pan", "signatory_pan"):
        value = (fields.get(name) or "").strip()
        if value and not is_valid_pan(value):
            out.append(
                Finding(
                    field=name,
                    reason=BounceReason.INVALID_PAN_FORMAT,
                    detail=f"'{value}' is not a valid PAN (expected five letters, "
                    "four digits, one letter)",
                    level="format",
                )
            )

    gst = (fields.get("company_gst") or "").strip()
    if gst and not is_valid_gstin(gst):
        out.append(
            Finding(
                field="company_gst",
                reason=BounceReason.INVALID_GST_FORMAT,
                detail=f"'{gst}' fails the GSTIN check digit",
                level="format",
            )
        )

    return out


def check_consistency(fields: dict[str, str]) -> list[Finding]:
    """Cross-field checks. These catch the records that look complete.

    A GSTIN embeds the entity's PAN and the state it was issued in, so a
    record can be fully populated, correctly formatted, and still describe two
    different companies.
    """
    out = []
    gst = (fields.get("company_gst") or "").strip().upper()
    pan = (fields.get("company_pan") or "").strip().upper()

    if gst and pan and is_valid_gstin(gst) and is_valid_pan(pan):
        if not gstin_matches_pan(gst, pan):
            out.append(
                Finding(
                    field="company_gst",
                    reason=BounceReason.PAN_GST_MISMATCH,
                    detail=f"GST carries PAN {gst[2:12]}, but the record says {pan}",
                    level="consistency",
                )
            )

    state = (fields.get("state") or "").strip()
    if gst and state and len(gst) >= 2 and state in STATE_CODES:
        if gst[:2] != STATE_CODES[state]:
            out.append(
                Finding(
                    field="company_gst",
                    reason=BounceReason.PAN_GST_MISMATCH,
                    detail=f"GST was issued in state code {gst[:2]}, but the "
                    f"registered address is in {state} ({STATE_CODES[state]})",
                    level="consistency",
                )
            )

    return out


def validate(fields: dict[str, str], required: list[str]) -> list[Finding]:
    """Every problem with this record, for this acquirer's requirements.

    Format and consistency run over the whole record, not only the required
    fields: a bad PAN is a bad PAN whether or not this particular acquirer
    asks for it, and catching it now saves the next acquirer a cycle.
    """
    findings = check_presence(fields, required)
    present = {f.field for f in findings}
    return (
        findings
        + [f for f in check_format(fields) if f.field not in present]
        + [f for f in check_consistency(fields) if f.field not in present]
    )
