"""
Is this merchant ready to be raised, and to whom?

Two ideas do most of the work here.

**Readiness is per acquirer, not per merchant.** Acquirers ask for different
fields, so a missing signatory PAN blocks the three that want it and leaves
the fourth untouched. Treating readiness as one yes-or-no for the merchant
would hold every application back for a field only one bank needs.

**The gap check runs the moment the ticket arrives.** Today gaps surface while
someone is filling in bank templates, which is several ops days after the
ticket landed. By then ops time has been spent, and the bounce still has to
happen. Finding the same gaps on arrival turns a late bounce into an early
one, and the days in between stop being spent twice.

The output answers three questions at once: what can go out today, what is
blocked and on whom, and what the bounce comment should say.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..catalogue.assess import Assessment, assess
from ..catalogue.eligibility import EligibilityResult, Verdict
from ..catalogue.mcc import Instrument
from ..catalogue.restrictions import Registry
from ..model import BOUNCE_ROUTING, Acquirer, BounceReason, Merchant, Party
from .validate import Finding, label_for, validate


@dataclass
class AcquirerReadiness:
    """Three separate questions about one acquirer, deliberately kept apart.

    *Blocked* asks whether anyone here raises this category at all.
    *Needs a decision* asks whether a bank's list says no, which is a fact
    about today rather than a rule, so a person decides and the decision is
    recorded.
    *Ready* asks whether the paperwork is complete.

    Collapsing them loses the distinction that matters to whoever has to act.
    Missing paperwork is a document request. A category our own policy refuses
    is a dead end, and chasing a document for it wastes the week. A category a
    bank refuses is a conversation, and the person having it often knows
    something the list does not.
    """

    acquirer_code: str
    acquirer_name: str
    ready: bool
    findings: list[Finding] = field(default_factory=list)
    assessment: Assessment | None = None

    @property
    def eligibility(self) -> EligibilityResult | None:
        """The acquirer policy read, kept available on its own.

        The assessment layers enforcement on top of it; the underlying verdict
        is still what the file and the covering email quote.
        """
        return self.assessment.eligibility if self.assessment else None

    @property
    def blocking_fields(self) -> list[str]:
        return sorted({f.field for f in self.findings})

    @property
    def refused(self) -> bool:
        """Our own policy, or a category the catalogue refuses outright.

        The only case where the paperwork is pointless, which is why it is the
        only case that suppresses the document chase.
        """
        return self.assessment is not None and self.assessment.blocked

    @property
    def needs_decision(self) -> bool:
        return (
            self.assessment is not None
            and self.assessment.needs_decision
            and not self.refused
        )

    @property
    def eligible(self) -> bool:
        return not self.refused

    @property
    def can_raise(self) -> bool:
        """Goes out today without anyone being asked.

        A restriction that needs a decision is excluded here, because the
        request can go out but not on its own: somebody has to look at the
        entry and say so, and that is the whole point of the change.
        """
        return (
            self.ready
            and not self.refused
            and (self.assessment is None or self.assessment.can_raise_without_a_decision)
        )

    @property
    def conditions(self) -> list[str]:
        return list(self.assessment.conditions) if self.assessment else []

    @property
    def status(self) -> str:
        if self.refused:
            return "blocked by policy"
        if not self.ready and self.needs_decision:
            return "blocked on documents, and needs a decision"
        if not self.ready:
            return "blocked on documents"
        if self.needs_decision:
            return "needs a decision"
        if self.conditions:
            return "ready, with conditions"
        return "ready"


@dataclass
class BounceInstruction:
    """One ticket going back to one party, with everything that party owns."""

    party: Party
    reasons: list[BounceReason]
    fields: list[str]
    blocked_acquirers: list[str]
    comment: str


@dataclass
class IntakeResult:
    merchant_id: str
    merchant_name: str
    per_acquirer: list[AcquirerReadiness]
    bounces: list[BounceInstruction]

    @property
    def ready_now(self) -> list[str]:
        return [a.acquirer_code for a in self.per_acquirer if a.can_raise]

    @property
    def blocked(self) -> list[str]:
        """Blocked on paperwork, which a document will fix."""
        return [a.acquirer_code for a in self.per_acquirer if not a.refused and not a.ready]

    @property
    def ineligible(self) -> list[str]:
        """Our own policy refuses the category. No document fixes this, so
        chasing one wastes the week."""
        return [a.acquirer_code for a in self.per_acquirer if a.refused]

    @property
    def awaiting_decision(self) -> list[str]:
        """A bank's list says no, and somebody has to decide whether to send it
        anyway. The paperwork is chased as normal in the meantime, because the
        decision often comes back yes and the chase would otherwise start from
        scratch after it."""
        return [a.acquirer_code for a in self.per_acquirer if a.needs_decision]

    @property
    def eligibility_unresolved(self) -> list[str]:
        """Policy could not be read for this code. Goes to a person, and is
        never quietly counted as acceptable: submitting a file to a bank
        without knowing whether it takes the category is how avoidable
        rejections happen, which is the thing this phase exists to stop."""
        return [
            a.acquirer_code
            for a in self.per_acquirer
            if a.eligibility and a.eligibility.verdict is Verdict.UNKNOWN
        ]

    @property
    def conditional(self) -> list[str]:
        return [
            a.acquirer_code
            for a in self.per_acquirer
            if a.eligibility and a.eligibility.verdict is Verdict.CONDITIONAL
        ]

    @property
    def fully_ready(self) -> bool:
        return all(a.can_raise for a in self.per_acquirer)

    @property
    def fully_blocked(self) -> bool:
        return not self.ready_now

    @property
    def can_partially_raise(self) -> bool:
        """The case worth catching: some acquirers can go out today while the
        rest wait for a fix. Holding all of them back is pure TAT given away."""
        return bool(self.ready_now) and bool(
            self.blocked
            or self.ineligible
            or self.awaiting_decision
            or self.eligibility_unresolved
        )

    @property
    def conditions(self) -> list[tuple[str, str]]:
        """What has to be produced or agreed, per acquirer, before go-live."""
        out: list[tuple[str, str]] = []
        for a in self.per_acquirer:
            seen = set()
            for condition in a.conditions:
                if condition not in seen:
                    seen.add(condition)
                    out.append((a.acquirer_code, condition))
            if a.eligibility and a.eligibility.condition not in seen and a.eligibility.condition:
                out.append((a.acquirer_code, a.eligibility.condition))
        return out

    @property
    def decisions_needed(self) -> list[tuple[str, str]]:
        """Each pending decision with the entry behind it, so whoever decides
        can see its source, its date and who recorded it."""
        return [
            (a.acquirer_code, a.assessment.explain())
            for a in self.per_acquirer
            if a.needs_decision and a.assessment
        ]

    def summary(self) -> str:
        n = len(self.per_acquirer)
        if self.fully_ready:
            return f"Ready for all {n} acquirers."
        if self.fully_blocked:
            parts = []
            if self.blocked:
                parts.append(f"{len(self.blocked)} blocked on documents")
            if self.ineligible:
                parts.append(f"{len(self.ineligible)} will not take the category")
            if self.awaiting_decision:
                parts.append(f"{len(self.awaiting_decision)} awaiting an ops decision")
            if self.eligibility_unresolved:
                parts.append(f"{len(self.eligibility_unresolved)} awaiting a policy read")
            return f"Cannot raise anywhere: {', '.join(parts)}."

        bits = [f"Raise to {len(self.ready_now)} of {n} now ({', '.join(self.ready_now)})"]
        if self.blocked:
            bits.append(f"{len(self.blocked)} waiting on documents")
        if self.ineligible:
            # Said plainly, because this is the one that cannot be chased.
            bits.append(f"{len(self.ineligible)} blocked by our own policy")
        if self.awaiting_decision:
            bits.append(f"{len(self.awaiting_decision)} awaiting an ops decision")
        if self.eligibility_unresolved:
            bits.append(f"{len(self.eligibility_unresolved)} awaiting a policy read")
        return "; ".join(bits) + "."


def _comment(party: Party, findings: list[Finding], blocked: dict[str, list[str]]) -> str:
    """Write the bounce comment ops would otherwise type by hand.

    It names the field, says what is wrong with it, and names which acquirers
    it is holding up, because 'missing PAN' without the consequence does not
    tell the receiving team how urgent it is.
    """
    lines = []
    for f in sorted(findings, key=lambda x: (x.level, x.field)):
        holding = blocked.get(f.field, [])
        tail = f" (blocks {', '.join(holding)})" if holding else ""
        lines.append(f"- {f.message}{tail}")

    who = "BD" if party is Party.BD else "onboarding"
    head = f"Cannot raise. {len(findings)} item{'' if len(findings) == 1 else 's'} for {who}:"
    return head + "\n" + "\n".join(lines)


def check_intake(
    merchant: Merchant,
    acquirers: list[Acquirer],
    mcc: str | None = None,
    instruments: tuple[Instrument, ...] = (),
    registry: Registry | None = None,
) -> IntakeResult:
    """Run the whole intake check for one merchant against the acquirers it is
    going to.

    Category is assessed before paperwork. An acquirer our own policy rules out
    is reported as blocked rather than as a long list of missing fields,
    because the fields are beside the point. An acquirer whose own list says no
    is reported as needing a decision, and its paperwork is chased as normal.

    The registry is passed in rather than read from disk here, so a caller
    running over a thousand requests loads it once and so a test can hand in
    exactly the entries it means to test.
    """
    fields = dict(merchant.fields)
    if mcc:
        fields["mcc"] = mcc

    # Eligibility keys on the MCC of *this request*, passed in, and never on an
    # MCC sitting in the merchant record. One merchant runs several use cases,
    # so a merchant-level MCC is at best one of them and at worst a leftover
    # from the first. Reading it off the record is the phase 5 bug, and getting
    # it wrong here would answer a policy question about the wrong business.
    effective_mcc = mcc or ""

    per_acquirer: list[AcquirerReadiness] = []
    # field -> the acquirers it is holding up, for the bounce comment
    blocked_by_field: dict[str, list[str]] = {}
    # party -> the findings that party owns, deduplicated by field
    by_party: dict[Party, dict[str, Finding]] = {}

    for acq in acquirers:
        assessment = (
            assess(registry, acq.code, effective_mcc, instruments)
            if effective_mcc and registry is not None
            else None
        )
        findings = validate(fields, acq.required_fields)
        per_acquirer.append(
            AcquirerReadiness(
                acquirer_code=acq.code,
                acquirer_name=acq.name,
                ready=not findings,
                findings=findings,
                assessment=assessment,
            )
        )

        # A refused acquirer raises no bounce. Sending BD to chase a PAN for a
        # bank that will not take the category at any paperwork level starts a
        # clock that can never finish. An *unresolved* policy is different: the
        # documents will be needed if the answer comes back yes, so the chase
        # goes out anyway and the policy question runs beside it.
        if per_acquirer[-1].refused:
            continue

        for f in findings:
            blocked_by_field.setdefault(f.field, []).append(acq.code)
            party = BOUNCE_ROUTING[f.reason]
            # One field, one finding, however many acquirers it blocks. The
            # ticket goes back once with everything on it, rather than bouncing
            # again for each acquirer that wanted the same missing field.
            by_party.setdefault(party, {}).setdefault(f.field, f)

    bounces = [
        BounceInstruction(
            party=party,
            reasons=sorted({f.reason for f in found.values()}, key=lambda r: r.value),
            fields=sorted(found),
            blocked_acquirers=sorted(
                {a for fld in found for a in blocked_by_field.get(fld, [])}
            ),
            comment=_comment(party, list(found.values()), blocked_by_field),
        )
        for party, found in sorted(by_party.items(), key=lambda kv: kv[0].value)
    ]

    return IntakeResult(
        merchant_id=merchant.merchant_id,
        merchant_name=merchant.legal_name,
        per_acquirer=per_acquirer,
        bounces=bounces,
    )
