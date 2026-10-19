"""
Synthetic dataset generator.

Produces merchants, procurement requests and event histories that behave like
the real pipeline: most requests clean, a third bouncing for missing data,
some rejected on category, a few deprioritised, and a queue of requests
assigned to ops but not yet raised.

Two principles make the data worth building on:

1. **Gaps are real.** If a request bounces for a missing signatory PAN, that
   field is genuinely absent from the merchant record. A later readiness check
   must be able to find it. Seeding a bounce event without the underlying gap
   would produce a dataset that flatters every downstream feature.

2. **Volumes and shapes follow the real pipeline.** Roughly 1,000 requests a
   month across 6 to 7 acquirers, 3 to 4 acquirers per merchant, a 30 to 40%
   bounce rate, and a large bulk-migration backlog sitting in "to be raised".
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .calendar_rules import WorkingCalendar
from .identifiers import corrupt_pan, make_gstin, make_pan
from .catalogue.mcc import Instrument
from .model import (
    BounceReason,
    Event,
    EventType,
    Merchant,
    PriorityTier,
    ProcurementRequest,
    RejectionReason,
    Segment,
    Stream,
)
from .names import (
    ENTITY_TYPE_BY_SUFFIX,
    MIDDLES,
    PREFIXES,
    RESTRICTED_MCCS,
    SAFE_MCCS,
    SIGNATORY_FIRST,
    SIGNATORY_LAST,
    IFSC_PREFIXES,
    SETTLEMENT_BANKS,
    STATES,
    STREETS,
    SUFFIXES,
)

# How a request plays out, and how often. Weights are deliberate, not even:
# most requests go through, a third bounce, a meaningful slice is rejected on
# category, and a visible queue never gets raised at all.
SCENARIOS: dict[str, int] = {
    "clean": 24,
    "bounce_then_clean": 17,
    "bank_requests_docs": 11,
    "rejected": 13,
    "bounce_twice": 5,
    "bank_docs_then_rejected": 3,
    "deprioritised": 4,
    "awaiting_bank": 6,
    "in_ops_queue": 9,
    # Open states. Without these the dataset has no work in progress, and an
    # ops exception list would have nothing to show.
    "pending_with_bd": 5,
    "bank_docs_open": 3,
}

# Document gaps go to BD; analysis and data gaps go to onboarding.
DOC_GAPS = [
    (BounceReason.MISSING_SIGNATORY_PAN, "signatory_pan"),
    (BounceReason.MISSING_SIGNATORY_ADDRESS, "signatory_address"),
    (BounceReason.MISSING_COMPANY_GST, "company_gst"),
    (BounceReason.MISSING_REGISTRATION_NUMBER, "registration_number"),
]

ANALYSIS_GAPS = [
    BounceReason.MCC_INCORRECT,
    BounceReason.WEBSITE_POLICY_MISSING,
    BounceReason.WEBSITE_CHECK_INCOMPLETE,
]

# Rejection reasons weighted the way they actually arrive.
REJECTIONS: dict[RejectionReason, int] = {
    RejectionReason.RESTRICTED_CATEGORY: 22,
    RejectionReason.RESTRICTED_MCC: 14,
    RejectionReason.WEBSITE_NOT_LEGITIMATE: 18,
    RejectionReason.UNCLEAR_CUSTOMER_JOURNEY: 13,
    RejectionReason.MULTIPLE_USE_CASES: 12,
    RejectionReason.PAYMENT_FLOW_BROKEN: 10,
    RejectionReason.INCORRECT_MCC: 7,
    RejectionReason.DOCUMENT_DISCREPANCY: 4,
}

SEGMENT_WEIGHTS = {Segment.ENTERPRISE: 8, Segment.PREMIUM_SME: 22, Segment.SME: 70}

# BAU skews toward the smaller tiers; a bulk migration is almost all tail.
TIER_WEIGHTS_BAU = {
    PriorityTier.P0: 4,
    PriorityTier.P1: 9,
    PriorityTier.P2: 16,
    PriorityTier.P3: 28,
    PriorityTier.P4: 43,
}
TIER_WEIGHTS_MIGRATION = {
    PriorityTier.P0: 1,
    PriorityTier.P1: 2,
    PriorityTier.P2: 5,
    PriorityTier.P3: 25,
    PriorityTier.P4: 67,
}


@dataclass
class MerchantGaps:
    """Gaps live on the merchant, not the request.

    A missing authorised signatory PAN is a fact about the merchant. It blocks
    every acquirer that asks for that field and no others, which is exactly
    why partial raising is possible: a merchant can be ready for two acquirers
    and blocked for the other two.

    Seeding gaps per request instead would let one request quietly fix a field
    another request is still bouncing on, and any readiness check built on that
    data would look flawless because the gaps it was meant to find had already
    been filled in.
    """

    unresolved: dict[str, BounceReason]  # merchant never supplied it
    resolved: dict[str, BounceReason]  # BD chased it and got it


@dataclass
class GeneratedData:
    merchants: list[Merchant] = field(default_factory=list)
    requests: list[ProcurementRequest] = field(default_factory=list)
    # The commercial terms BD agreed with the merchant, keyed by merchant id.
    # They live apart from the merchant record because they are a different
    # team's data with a different owner: ops cannot fix a missing settlement
    # cycle, only chase it. A share of them is incomplete, which is what makes
    # "the bank file is blocked on sales" a measurable figure rather than an
    # anecdote.
    deals: dict[str, dict[str, str]] = field(default_factory=dict)
    generator: "DataGenerator | None" = None

    def by_scenario(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.requests:
            s = getattr(r, "scenario", "unknown")
            counts[s] = counts.get(s, 0) + 1
        return counts


def _weighted(rng: random.Random, weights: dict):
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _entity_type(legal_name: str) -> str:
    """The form's dropdown wording, not the name's own.

    Longest suffix first, because "India Private Limited" also ends in
    "Limited" and would otherwise file a private company as a public one.
    """
    for suffix in sorted(ENTITY_TYPE_BY_SUFFIX, key=len, reverse=True):
        if legal_name.endswith(suffix):
            return ENTITY_TYPE_BY_SUFFIX[suffix]
    return "Private Limited"


class DataGenerator:
    """Deterministic given a seed, so a demo shows the same numbers twice."""

    def __init__(
        self,
        acquirers: dict,
        calendar: WorkingCalendar,
        seed: int = 42,
        as_of: datetime | None = None,
    ) -> None:
        self.acquirers = acquirers
        self.calendar = calendar
        self.rng = random.Random(seed)
        self.as_of = as_of or datetime(2026, 10, 1, 18, 0)
        self._used_names: set[str] = set()
        self.gaps: dict[str, MerchantGaps] = {}

    # --- merchants ---------------------------------------------------

    def _merchant_name(self) -> str:
        for _ in range(60):
            name = (
                f"{self.rng.choice(PREFIXES)} {self.rng.choice(MIDDLES)} "
                f"{self.rng.choice(SUFFIXES)}"
            )
            if name not in self._used_names:
                self._used_names.add(name)
                return name
        # Fall back to a numbered suffix rather than silently colliding.
        base = f"{self.rng.choice(PREFIXES)} {self.rng.choice(MIDDLES)}"
        name = f"{base} {len(self._used_names)} Private Limited"
        self._used_names.add(name)
        return name

    def make_merchant(self, index: int, stream: Stream) -> Merchant:
        name = self._merchant_name()
        state, state_code, city, pincode = self.rng.choice(STATES)
        pan = make_pan(self.rng, "C")
        gstin = make_gstin(self.rng, state_code, pan)
        signatory = (
            f"{self.rng.choice(SIGNATORY_FIRST)} {self.rng.choice(SIGNATORY_LAST)}"
        )
        slug = name.split()[0].lower()

        tier_weights = (
            TIER_WEIGHTS_MIGRATION if stream is Stream.MIGRATION else TIER_WEIGHTS_BAU
        )

        merchant = Merchant(
            merchant_id=f"M-{index:05d}",
            legal_name=name,
            segment=_weighted(self.rng, SEGMENT_WEIGHTS),
            priority_tier=_weighted(self.rng, tier_weights),
            website=f"https://{slug}{index}.test",
            fields={
                "entity_legal_name": name,
                "company_pan": pan,
                "company_gst": gstin,
                "registered_address": (
                    f"{self.rng.randint(1, 240)} {self.rng.choice(STREETS)}, "
                    f"{city} {pincode}"
                ),
                "registration_number": f"U{self.rng.randint(10000, 99999)}"
                f"{state_code}{self.rng.randint(2015, 2025)}PTC{self.rng.randint(100000, 999999)}",
                "signatory_name": signatory,
                "signatory_pan": make_pan(self.rng, "P"),
                "signatory_address": (
                    f"{self.rng.randint(1, 180)} {self.rng.choice(STREETS)}, "
                    f"{city} {pincode}"
                ),
                "website": f"https://{slug}{index}.test",
                "state": state,
                # Fields the bank submission form asks for that the TAT engine
                # never needed. They are on the merchant because they describe
                # the entity, not the application: one of them being wrong
                # fails the file at every acquirer at once, which is exactly
                # why the pre-flight check runs on the batch rather than on
                # whatever the person filling the template happens to notice.
                "state_code": state_code,
                "city": city,
                "pincode": pincode,
                "entity_type": _entity_type(name),
                "incorporation_date": date(
                    self.rng.randint(2015, 2024),
                    self.rng.randint(1, 12),
                    self.rng.randint(1, 28),
                ).isoformat(),
                "contact_name": signatory,
                "contact_mobile": f"9{self.rng.randint(100000000, 999999999)}",
                "contact_email": f"accounts@{slug}{index}.test",
                "settlement_bank": self.rng.choice(SETTLEMENT_BANKS),
                "settlement_account_name": name,
                "settlement_account_number": str(
                    self.rng.randint(10**11, 10**12 - 1)
                ),
                "settlement_ifsc": f"{self.rng.choice(IFSC_PREFIXES)}0"
                f"{self.rng.randint(100000, 999999)}",
            },
        )

        self.gaps[merchant.merchant_id] = self._seed_gaps(merchant)
        self._corrupt_some_values(merchant)
        return merchant

    def _seed_gaps(self, merchant: Merchant) -> MerchantGaps:
        """Decide once, per merchant, which fields are missing and whether BD
        ever got them.

        Roughly a third of merchants arrive incomplete, matching the 30 to 40%
        of tickets that bounce. Of those, most are eventually chased down; a
        minority never are, and those requests are still sitting open.
        """
        unresolved: dict[str, BounceReason] = {}
        resolved: dict[str, BounceReason] = {}

        if self.rng.random() >= 0.34:
            return MerchantGaps(unresolved, resolved)

        how_many = 1 if self.rng.random() < 0.75 else 2
        for reason, field_name in self.rng.sample(DOC_GAPS, how_many):
            if self.rng.random() < 0.28:
                # Never supplied. Strip it from the record for good.
                merchant.fields.pop(field_name, None)
                unresolved[field_name] = reason
            else:
                # BD chased it and got it, so the record is complete now even
                # though the request bounced on the way.
                resolved[field_name] = reason

        return MerchantGaps(unresolved, resolved)

    def _blocking_gap(
        self, request: ProcurementRequest
    ) -> tuple[str, BounceReason, bool] | None:
        """The gap, if any, that blocks this particular acquirer.

        An acquirer that does not ask for the missing field is not blocked by
        it, which is what makes partial raising possible.
        """
        gaps = self.gaps.get(request.merchant.merchant_id)
        if not gaps:
            return None
        required = set(request.acquirer.required_fields)

        for field_name, reason in gaps.unresolved.items():
            if field_name in required:
                return field_name, reason, False
        for field_name, reason in gaps.resolved.items():
            if field_name in required:
                return field_name, reason, True
        return None

    # --- timing helpers ----------------------------------------------

    def _advance(self, at: datetime, lo: int, hi: int, morning: bool = False) -> datetime:
        """Move forward a few working days and land at a plausible hour.

        Never past the as-of moment: an event dated in the future would be a
        fact that has not happened yet, and would quietly corrupt every
        average built on the data.
        """
        days = self.rng.randint(lo, hi)
        cursor = at.date()
        moved = 0
        while moved < days:
            cursor += timedelta(days=1)
            if self.calendar.is_working_day(cursor):
                moved += 1
        hour = self.rng.randint(9, 11) if morning else self.rng.randint(9, 18)
        return datetime(
            cursor.year, cursor.month, cursor.day, hour, self.rng.choice([0, 15, 30, 45])
        )

    def _too_late(self, at: datetime) -> bool:
        """Whether this event would fall after the as-of moment.

        Events past as-of are dropped rather than pulled back to as-of.
        Clamping looks harmless but stacks several events onto the same day,
        which produces a merchant appearing twice in one bank reply with two
        different outcomes. Dropping them leaves the request open, which is
        what 'it has not happened yet' actually means.
        """
        return at > self.as_of

    def _start_date(self, stream: Stream, scenario: str = "") -> datetime:
        """Requests land across the last few months. Migration requests cluster
        in a burst, the way a bulk project actually arrives.

        Requests that are still open start recently. A BAU request genuinely
        sitting unraised for four months would be an escalation, not a queue,
        and letting the generator produce them would wreck every average with
        cases that would never survive a weekly review.
        """
        open_scenarios = {"in_ops_queue", "pending_with_bd", "bank_docs_open", "awaiting_bank"}

        if scenario in open_scenarios:
            offset = self.rng.randint(3, 30) if stream is Stream.BAU else self.rng.randint(8, 40)
        elif stream is Stream.MIGRATION:
            offset = self.rng.randint(20, 70)
        else:
            offset = self.rng.randint(1, 110)

        d = (self.as_of - timedelta(days=offset)).date()
        while not self.calendar.is_working_day(d):
            d -= timedelta(days=1)
        return datetime(d.year, d.month, d.day, self.rng.randint(9, 16), 0)

    # --- requests ----------------------------------------------------

    def make_requests_for(
        self, merchant: Merchant, stream: Stream, seq: int
    ) -> list[ProcurementRequest]:
        """One merchant goes to 3 or 4 acquirers. Occasionally it runs two use
        cases, which means two MCCs and therefore two sets of requests: the
        reason a request is keyed on merchant + MCC + acquirer."""
        codes = self.rng.sample(list(self.acquirers), self.rng.choice([3, 3, 4, 4]))

        mccs = [self._pick_mcc()]
        if self.rng.random() < 0.08:  # a minority run a second use case
            second = self._pick_mcc()
            if second != mccs[0]:
                mccs.append(second)

        # A cards MID is credit plus debit by default. A minority of merchants
        # also ask for EMI on the same MID, which matters because one acquirer
        # keeps a separate, longer ban list for EMI alone: the same merchant can
        # be accepted on cards and refused on EMI at the same bank.
        instruments: tuple[Instrument, ...] = (Instrument.CREDIT_CARD, Instrument.DEBIT_CARD)
        if self.rng.random() < 0.18:
            instruments = instruments + (Instrument.EMI,)

        requests: list[ProcurementRequest] = []
        for mcc in mccs:
            for code in codes:
                r = ProcurementRequest(
                    request_id=f"REQ-{seq:05d}-{mcc}-{code}",
                    merchant=merchant,
                    mcc=mcc,
                    acquirer=self.acquirers[code],
                    stream=stream,
                    instruments=instruments,
                )
                requests.append(r)
        return requests

    def _pick_mcc(self) -> str:
        """Most merchants sit in a clean category. A minority do not, which is
        where the restricted-category rejections come from."""
        if self.rng.random() < 0.09:
            return self.rng.choice(list(RESTRICTED_MCCS))
        return self.rng.choice(list(SAFE_MCCS))

    # --- event histories ---------------------------------------------

    def play(self, request: ProcurementRequest) -> str:
        """Write an event history onto a request and return the scenario used."""
        scenario = _weighted(self.rng, SCENARIOS)

        # A gap on the merchant record overrides the dice. If this acquirer
        # asks for a field the merchant never supplied, the request cannot
        # have gone through, whatever scenario was drawn.
        gap = self._blocking_gap(request)
        if gap and scenario not in ("deprioritised", "in_ops_queue"):
            scenario = "bounce_then_clean" if gap[2] else "pending_with_bd"

        # A restricted MCC overrides everything else. Letting a gambling or
        # money-transfer merchant sail through to approval would teach every
        # downstream model the wrong thing, so this check runs last and wins.
        if request.mcc in RESTRICTED_MCCS and scenario not in (
            "deprioritised",
            "in_ops_queue",
            "pending_with_bd",
        ):
            scenario = "rejected"
            gap = None

        at = self._start_date(request.stream, scenario)
        request.add(Event(EventType.ASSIGNED_TO_OPS, at))

        if scenario == "in_ops_queue":
            # Sits in "to be raised". Ops time accrues against an open clock,
            # which is exactly the queue time a tracker that only records
            # dates on action cannot see.
            return scenario

        if scenario == "pending_with_bd":
            # Bounced and still out. The clock is running on BD and onboarding.
            self._bounce(request, at, leave_open=True, gap=gap)
            return scenario

        if scenario in ("bounce_then_clean", "bounce_twice"):
            at = self._bounce(request, at, gap=gap)
            if at is None:
                return "pending_with_bd"
        if scenario == "bounce_twice":
            at = self._bounce(request, at)
            if at is None:
                return "pending_with_bd"

        if scenario == "deprioritised":
            at = self._advance(at, 1, 6)
            if self._too_late(at):
                return "in_ops_queue"
            request.add(Event(EventType.DEPRIORITISED, at))
            return scenario

        # Raised to the bank, usually first thing in the morning.
        at = self._advance(at, 1, 3, morning=True)
        if self._too_late(at):
            return "in_ops_queue"  # not raised yet, so it is still in the queue
        request.add(Event(EventType.RAISED_TO_ACQUIRER, at))

        if scenario == "awaiting_bank":
            return scenario  # still open with the acquirer

        if scenario in ("bank_requests_docs", "bank_docs_then_rejected", "bank_docs_open"):
            # Asking for a document is itself a response, so it is held to the
            # same agreed TAT as a decision.
            at = self._advance(at, *self._status_window(request))
            if self._too_late(at):
                return "awaiting_bank"
            which = self.rng.choice(
                [
                    EventType.ACQUIRER_REQUESTED_DOCS,
                    EventType.ACQUIRER_REQUESTED_CLARIFICATION,
                ]
            )
            request.add(Event(which, at))

            if scenario == "bank_docs_open":
                return scenario  # still gathering, clock running on BD

            at = self._advance(at, 3, 11)  # merchant turnaround, charged to BD
            if self._too_late(at):
                return "bank_docs_open"
            request.add(Event(EventType.RESPONDED_TO_ACQUIRER, at))

        if scenario in ("rejected", "bank_docs_then_rejected"):
            at = self._advance(at, *self._status_window(request))
            if self._too_late(at):
                return "awaiting_bank"
            reason = self._rejection_for(request)
            request.add(Event(EventType.ACQUIRER_REJECTED, at, rejection_reason=reason))
            return scenario

        # Approved, then the MID arrives separately a few days later.
        # Breach rates are set deliberately rather than falling out of a wide
        # random range: most acquirers meet their agreed TAT most of the time,
        # and a breach is an exception worth chasing, not the norm. One
        # acquirer is made noticeably worse than the rest so the acquirer
        # comparison has something real to show.
        at = self._advance(at, *self._status_window(request))
        if self._too_late(at):
            return "awaiting_bank"
        request.add(Event(EventType.ACQUIRER_APPROVED, at))

        at = self._advance(at, 1, request.acquirer.mid_sla_days + 3)
        if self._too_late(at):
            return "awaiting_mid"  # approved, credentials not through yet
        request.add(Event(EventType.MID_RECEIVED, at))
        return scenario

    # Share of responses that miss the agreed SLA, per acquirer. VERRAN is the
    # laggard, so the acquirer comparison has a real difference to surface.
    BREACH_RATE: dict[str, float] = {
        "ALDRIDGE": 0.14,
        "BRINDLE": 0.11,
        "CASTERLY": 0.18,
        "NORTHWAY_CYBS": 0.16,
        "NORTHWAY_FSS": 0.15,
        "VERRAN": 0.34,
    }

    def _status_window(self, request: ProcurementRequest) -> tuple[int, int]:
        """Days the acquirer takes to answer this round, as a (lo, hi) range."""
        sla = request.acquirer.status_sla_days
        rate = self.BREACH_RATE.get(request.acquirer.code, 0.15)
        if self.rng.random() < rate:
            return sla + 1, sla + 6  # missed it
        return max(1, sla - 3), sla  # inside the agreed TAT

    def _bounce(
        self,
        request: ProcurementRequest,
        at: datetime,
        leave_open: bool = False,
        gap: tuple[str, BounceReason, bool] | None = None,
    ) -> datetime | None:
        """Ops finds a gap and bounces to whoever owns it.

        When a real merchant-level gap blocks this acquirer, the bounce cites
        that exact reason, so the event and the record agree. Otherwise it is
        an analysis gap, which onboarding fixes internally and faster.
        """
        at = self._advance(at, 1, 3)
        if self._too_late(at):
            return None  # ops has not got to it yet

        if gap is not None:
            _, reason, _ = gap
            request.add(Event(EventType.BOUNCED_TO_BD, at, bounce_reason=reason))
            if leave_open:
                return at  # still with BD, nothing comes back yet
            back = self._advance(at, 3, 14)  # waiting on the merchant
        elif self.rng.random() < 0.45:
            reason = self.rng.choice(ANALYSIS_GAPS)
            request.add(Event(EventType.BOUNCED_TO_ONBOARDING, at, bounce_reason=reason))
            if leave_open:
                return at
            back = self._advance(at, 1, 6)  # internal fix, faster
        else:
            reason, _field = self.rng.choice(DOC_GAPS)
            request.add(Event(EventType.BOUNCED_TO_BD, at, bounce_reason=reason))
            if leave_open:
                return at
            back = self._advance(at, 3, 14)

        if self._too_late(back):
            return None  # still out with whoever we bounced it to
        request.add(Event(EventType.RETURNED_TO_OPS, back))
        return back

    def _corrupt_some_values(self, merchant: Merchant) -> None:
        """A field can be filled and still be wrong.

        Presence is not validity, and a wrong PAN costs a full bank rejection
        cycle to discover. A small share of records carry a mistyped PAN or a
        GST issued against a different entity, so a validator has something
        real to catch rather than only blanks.
        """
        if self.rng.random() < 0.06:
            pan = merchant.fields.get("signatory_pan")
            if pan:
                merchant.fields["signatory_pan"] = corrupt_pan(self.rng, pan)

        # Only ever corrupt a value that is there. Writing one back into a
        # field that was stripped as a gap would quietly repair the gap.
        if merchant.fields.get("company_gst") and self.rng.random() < 0.04:
            # GST that does not carry this entity's PAN: valid on its own,
            # wrong in context. Only a cross-check finds it.
            other_pan = make_pan(self.rng, "C")
            state_code = self.rng.choice(STATES)[1]
            merchant.fields["company_gst"] = make_gstin(self.rng, state_code, other_pan)

    def _rejection_for(self, request: ProcurementRequest) -> RejectionReason:
        if request.mcc in RESTRICTED_MCCS:
            return self.rng.choice(
                [RejectionReason.RESTRICTED_CATEGORY, RejectionReason.RESTRICTED_MCC]
            )
        return _weighted(self.rng, REJECTIONS)

    # --- top level ----------------------------------------------------

    def make_deal(self, merchant: Merchant) -> dict[str, str]:
        """The commercial terms BD agreed, as ops receives them.

        Rates are per bank in reality and are left out here on purpose: this
        project does not model a rate card, and inventing one would put numbers
        in a demonstration build that look like a real pricing sheet. What is
        modelled is ownership. These values arrive from another team, a share
        of them never arrive at all, and ops cannot supply them, only chase.
        """
        deal = {
            "settlement_cycle": _weighted(self.rng, {"Daily": 6, "Weekly": 2, "Monthly": 2}),
            "expected_transaction": str(self.rng.choice([5, 10, 25, 50, 100, 250]) * 100000),
            "relationship_manager": (
                f"{self.rng.choice(SIGNATORY_FIRST)} {self.rng.choice(SIGNATORY_LAST)}"
            ),
            "business_model": _weighted(self.rng, {"B2C": 7, "B2B": 2, "Both": 1}),
            "point_of_contact": _weighted(self.rng, {"Merchant": 7, "Reseller": 2, "RM": 1}),
            "sales_hod": f"{self.rng.choice(SIGNATORY_FIRST)} {self.rng.choice(SIGNATORY_LAST)}",
            "zonal_head": f"{self.rng.choice(SIGNATORY_FIRST)} {self.rng.choice(SIGNATORY_LAST)}",
        }

        # The deal sheet arrives incomplete often enough that it is a named
        # reason a batch misses its morning slot.
        if self.rng.random() < 0.12:
            for key in self.rng.sample(
                ["settlement_cycle", "expected_transaction", "relationship_manager"],
                k=self.rng.choice([1, 1, 2]),
            ):
                deal[key] = ""

        # One in forty carries a wording the bank's dropdown will not take.
        # Typed by a person into a free-text field, invisible to a reviewer,
        # and enough to have the whole file refused on upload.
        if self.rng.random() < 0.025 and deal["settlement_cycle"]:
            deal["settlement_cycle"] = self.rng.choice(["T+1", "daily", "Fortnightly"])

        return deal


    def generate(
        self, bau_merchants: int = 220, migration_merchants: int = 140
    ) -> GeneratedData:
        data = GeneratedData(generator=self)
        seq = 0

        for count, stream in (
            (bau_merchants, Stream.BAU),
            (migration_merchants, Stream.MIGRATION),
        ):
            for _ in range(count):
                seq += 1
                merchant = self.make_merchant(seq, stream)
                data.merchants.append(merchant)
                data.deals[merchant.merchant_id] = self.make_deal(merchant)
                for request in self.make_requests_for(merchant, stream, seq):
                    scenario = self.play(request)
                    request.scenario = scenario  # type: ignore[attr-defined]
                    data.requests.append(request)

        # A bulk migration arrives faster than three people can raise it, so a
        # large share of it is still sitting in the queue. That backlog is the
        # clearest signal of a capacity problem, and it is invisible in any
        # report that only starts counting once a request is raised.
        for r in data.requests:
            if (
                r.stream is Stream.MIGRATION
                and len(r.events) > 1
                and self.rng.random() < 0.55
            ):
                first = r.events[0]
                r.events.clear()
                r.events.append(first)
                r.scenario = "in_ops_queue"  # type: ignore[attr-defined]

        return data
