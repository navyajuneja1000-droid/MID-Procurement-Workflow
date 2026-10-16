"""
Core data model for cards MID procurement.

The unit of work is a ProcurementRequest, keyed on (merchant, MCC, acquirer).
Not (merchant, acquirer): one merchant can run several use cases, each with its
own MCC, and each needs its own MID request. Bank replies are matched on legal
name plus MCC for exactly this reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .catalogue.mcc import Instrument


class Party(str, Enum):
    """Who can hold a request. The merchant is deliberately not a party:
    time spent waiting on a merchant is counted under BD, as it is today."""

    OPS = "ops"
    ONBOARDING = "onboarding"
    BD = "bd"
    ACQUIRER = "acquirer"


class Segment(str, Enum):
    ENTERPRISE = "enterprise"
    PREMIUM_SME = "premium_sme"
    SME = "sme"


class PriorityTier(str, Enum):
    """GMV-based priority. Drives the order requests are raised in."""

    P0 = "p0"  # >= 1 Cr
    P1 = "p1"  # >= 25L
    P2 = "p2"  # >= 10L
    P3 = "p3"  # >= 5L
    P4 = "p4"  # < 5L


class Stream(str, Enum):
    """BAU inflow vs a bulk migration project. Reported separately, because a
    migration backlog otherwise destroys BAU averages."""

    BAU = "bau"
    MIGRATION = "migration"


class RequestStatus(str, Enum):
    PENDING_ONBOARDING = "pending_onboarding"
    TO_BE_RAISED = "to_be_raised"
    RAISED = "raised"
    DOCS_PENDING_BD = "additional_docs_pending_bd"
    DOCS_PENDING_ONBOARDING = "additional_docs_pending_onboarding"
    CLARIFICATION_REQUIRED = "clarification_required"
    REJECTED = "rejected_by_bank"
    APPROVED = "approved"
    PROCURED = "procured"
    DEPRIORITISED = "deprioritised"


class EventType(str, Enum):
    """Every handoff that moves a clock. Each one has a natural trigger in a
    real system (a Salesforce status change, a batch send, a parsed reply), so
    nothing depends on someone remembering to record a duration."""

    ASSIGNED_TO_OPS = "assigned_to_ops"
    BOUNCED_TO_BD = "bounced_to_bd"
    BOUNCED_TO_ONBOARDING = "bounced_to_onboarding"
    RETURNED_TO_OPS = "returned_to_ops"
    RAISED_TO_ACQUIRER = "raised_to_acquirer"
    ACQUIRER_REQUESTED_DOCS = "acquirer_requested_docs"
    ACQUIRER_REQUESTED_CLARIFICATION = "acquirer_requested_clarification"
    RESPONDED_TO_ACQUIRER = "responded_to_acquirer"
    ACQUIRER_APPROVED = "acquirer_approved"
    ACQUIRER_REJECTED = "acquirer_rejected"
    MID_RECEIVED = "mid_received"
    DEPRIORITISED = "deprioritised"
    # A person decided to raise a request against a restriction on file. It
    # moves no clock, so the TAT engine ignores it, but it belongs on the event
    # log rather than in a side table: the question asked afterwards is always
    # "who sent this, when, and what did they know at the time", and the event
    # log is the only place that answers it without being reconstructed.
    RESTRICTION_OVERRIDDEN = "restriction_overridden"


# Events that record something without moving the request on. They belong on
# the log for audit, and nowhere in the status or the clocks.
ANNOTATION_EVENTS = {EventType.RESTRICTION_OVERRIDDEN}


class BounceReason(str, Enum):
    """Fixed reason codes, so the back-ticket-reasons report builds itself
    instead of being bucketed by hand from free-text comments."""

    MISSING_SIGNATORY_PAN = "missing_signatory_pan"
    MISSING_SIGNATORY_ADDRESS = "missing_signatory_address"
    MISSING_COMPANY_GST = "missing_company_gst"
    MISSING_REGISTRATION_NUMBER = "missing_registration_number"
    MISSING_COMPANY_PAN = "missing_company_pan"
    INVALID_PAN_FORMAT = "invalid_pan_format"
    INVALID_GST_FORMAT = "invalid_gst_format"
    PAN_GST_MISMATCH = "pan_gst_mismatch"
    MCC_MISSING = "mcc_missing"
    MCC_INCORRECT = "mcc_incorrect"
    WEBSITE_POLICY_MISSING = "website_policy_missing"
    WEBSITE_CHECK_INCOMPLETE = "website_check_incomplete"


class RejectionReason(str, Enum):
    """Buckets for acquirer rejections, which arrive as free text."""

    RESTRICTED_CATEGORY = "restricted_category"
    RESTRICTED_MCC = "restricted_mcc"
    INCORRECT_MCC = "incorrect_mcc"
    WEBSITE_NOT_LEGITIMATE = "website_not_legitimate"
    UNCLEAR_CUSTOMER_JOURNEY = "unclear_customer_journey"
    PAYMENT_FLOW_BROKEN = "payment_flow_broken"
    MULTIPLE_USE_CASES = "multiple_use_cases"
    DOCUMENT_DISCREPANCY = "document_discrepancy"
    OTHER = "other"


# Which party a bounce reason routes to. Ops never decides this by hand.
BOUNCE_ROUTING: dict[BounceReason, Party] = {
    BounceReason.MISSING_SIGNATORY_PAN: Party.BD,
    BounceReason.MISSING_SIGNATORY_ADDRESS: Party.BD,
    BounceReason.MISSING_COMPANY_GST: Party.BD,
    BounceReason.MISSING_REGISTRATION_NUMBER: Party.BD,
    BounceReason.MISSING_COMPANY_PAN: Party.BD,
    BounceReason.INVALID_PAN_FORMAT: Party.BD,
    BounceReason.INVALID_GST_FORMAT: Party.BD,
    BounceReason.PAN_GST_MISMATCH: Party.BD,
    BounceReason.MCC_MISSING: Party.ONBOARDING,
    BounceReason.MCC_INCORRECT: Party.ONBOARDING,
    BounceReason.WEBSITE_POLICY_MISSING: Party.ONBOARDING,
    BounceReason.WEBSITE_CHECK_INCOMPLETE: Party.ONBOARDING,
}


@dataclass
class Acquirer:
    """An acquiring rail, not a bank. Some banks run two processing platforms
    and issue a MID on each, configured separately. Those are two acquirers
    here, because they are two separate procurement requests with separate
    clocks."""

    code: str
    name: str
    status_sla_days: int  # agreed working days to respond with a status
    mid_sla_days: int  # agreed working days from approval to MID delivery
    required_fields: list[str]
    holiday_calendar: str = "default"

    def __post_init__(self) -> None:
        if self.status_sla_days < 1 or self.mid_sla_days < 1:
            raise ValueError(f"{self.code}: SLA days must be at least 1")


@dataclass
class Merchant:
    merchant_id: str
    legal_name: str
    segment: Segment
    priority_tier: PriorityTier
    website: str | None = None
    fields: dict[str, str] = field(default_factory=dict)

    def missing_fields(self, required: list[str]) -> list[str]:
        """Fields that are absent or blank. Presence only; format and
        consistency checks live in the validation module."""
        return [f for f in required if not self.fields.get(f, "").strip()]


@dataclass
class Event:
    """An immutable fact: something happened at a point in time.

    Durations are never stored. Every TAT figure is derived from these events
    on demand, which is what makes the numbers current, auditable, and
    recalculable if a rule changes later."""

    event_type: EventType
    at: datetime
    bounce_reason: BounceReason | None = None
    rejection_reason: RejectionReason | None = None
    note: str = ""

    def __post_init__(self) -> None:
        bounce_types = {EventType.BOUNCED_TO_BD, EventType.BOUNCED_TO_ONBOARDING}
        if self.event_type in bounce_types and self.bounce_reason is None:
            raise ValueError(f"{self.event_type.value} needs a bounce_reason")
        if self.event_type is EventType.ACQUIRER_REJECTED and self.rejection_reason is None:
            raise ValueError("acquirer_rejected needs a rejection_reason")


@dataclass
class ProcurementRequest:
    """One merchant, one MCC, one acquirer. The unit everything else counts."""

    request_id: str
    merchant: Merchant
    mcc: str
    acquirer: Acquirer
    stream: Stream = Stream.BAU
    # What the merchant wants to accept on this MID. It belongs on the request
    # because acquirer policy is set per rail: the same category can be open on
    # debit and closed on credit, and one acquirer bans EMI on a longer list of
    # categories than it bans outright. A request that does not say which rails
    # it needs cannot be answered on eligibility.
    instruments: tuple[Instrument, ...] = (Instrument.CREDIT_CARD, Instrument.DEBIT_CARD)
    events: list[Event] = field(default_factory=list)

    def add(self, event: Event) -> None:
        """Events must arrive in chronological order. Out-of-order arrival
        usually means two sources disagree, and silently sorting would hide
        that rather than surface it."""
        if self.events and event.at < self.events[-1].at:
            raise ValueError(
                f"{self.request_id}: event {event.event_type.value} at {event.at} "
                f"precedes the last event at {self.events[-1].at}"
            )
        self.events.append(event)

    @property
    def is_deprioritised(self) -> bool:
        return any(e.event_type is EventType.DEPRIORITISED for e in self.events)

    @property
    def status(self) -> RequestStatus:
        """Derived from the event history, never stored. One source of truth."""
        if not self.events:
            return RequestStatus.PENDING_ONBOARDING

        # Some events annotate the request without moving it. An override is
        # one: a person recording that they are sending this anyway does not
        # change where it sits. Skipping them here is what keeps the mapping
        # below exhaustive, which is deliberate: an unmapped event raising is
        # how a new event type gets a status decided for it rather than
        # defaulting to something plausible and wrong.
        moving = [e for e in self.events if e.event_type not in ANNOTATION_EVENTS]
        if not moving:
            return RequestStatus.PENDING_ONBOARDING

        last = moving[-1].event_type
        mapping = {
            EventType.DEPRIORITISED: RequestStatus.DEPRIORITISED,
            EventType.MID_RECEIVED: RequestStatus.PROCURED,
            EventType.ACQUIRER_APPROVED: RequestStatus.APPROVED,
            EventType.ACQUIRER_REJECTED: RequestStatus.REJECTED,
            EventType.ACQUIRER_REQUESTED_DOCS: RequestStatus.DOCS_PENDING_BD,
            EventType.ACQUIRER_REQUESTED_CLARIFICATION: RequestStatus.CLARIFICATION_REQUIRED,
            EventType.RAISED_TO_ACQUIRER: RequestStatus.RAISED,
            EventType.RESPONDED_TO_ACQUIRER: RequestStatus.RAISED,
            EventType.ASSIGNED_TO_OPS: RequestStatus.TO_BE_RAISED,
            EventType.RETURNED_TO_OPS: RequestStatus.TO_BE_RAISED,
            EventType.BOUNCED_TO_BD: RequestStatus.DOCS_PENDING_BD,
            EventType.BOUNCED_TO_ONBOARDING: RequestStatus.DOCS_PENDING_ONBOARDING,
        }
        return mapping[last]
