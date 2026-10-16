"""
One answer per application: can this go out, and if not, whose call is it?

Phase 7 produced a verdict. This produces a verdict plus an enforcement level,
which is the difference between a system that decides and a system that
informs. The distinction matters because of where the knowledge sits. A list
knows what a bank said last quarter. The person raising the file knows whether
risk already signed this merchant off, whether the licence came through
yesterday, and what the relationship manager agreed on Tuesday. A system that
blocks on the list throws that away; a system that flags and records the
decision keeps both.

Four outcomes, in order of strength:

- **Blocked.** Our own policy, or a category the catalogue refuses outright.
  Nobody at this company raises this, so no document is chased for it either.
- **Needs a decision.** A bank's list says no, or we cannot read its policy.
  Ops sees the entry, its source and its date, and decides. The decision is
  recorded, which is what later tells us whether the entry was right.
- **Goes out with a condition.** Accepted on fewer rails, or accepted subject
  to a sign-off. The condition travels with the file and into the covering
  email.
- **Clear.**

The asymmetry is deliberate. Only our own policy blocks, because only our own
policy is a rule. Everything a bank tells us is a fact about today, and facts
about today go stale without telling anyone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .eligibility import EligibilityResult, Verdict, check
from .mcc import CATALOGUE, Instrument, lookup
from .restrictions import HOUSE, Enforcement, Registry, Restriction

# How strong each enforcement level is, so the strongest applying entry decides.
RANK = {Enforcement.BLOCK: 3, Enforcement.WARN: 2, Enforcement.NOTE: 1}


@dataclass
class Assessment:
    acquirer_code: str
    mcc: str
    enforcement: Enforcement | None  # None means nothing applies at all
    eligibility: EligibilityResult
    entries: list[Restriction] = field(default_factory=list)
    blocked_instruments: tuple[Instrument, ...] = ()
    conditions: list[str] = field(default_factory=list)
    # Set when the block comes from the catalogue rather than from a registry
    # entry, so the message can say so instead of naming an entry id nobody
    # can look up.
    house_reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.enforcement is Enforcement.BLOCK

    @property
    def needs_decision(self) -> bool:
        """Raise only on a recorded decision. Not the same as blocked: the
        request may well go out today, it just does not go out silently."""
        return self.enforcement is Enforcement.WARN

    @property
    def can_raise_without_a_decision(self) -> bool:
        return self.enforcement in (None, Enforcement.NOTE)

    @property
    def deciding_entry(self) -> Restriction | None:
        for e in self.entries:
            if e.enforcement is self.enforcement:
                return e
        return None

    @property
    def status(self) -> str:
        if self.blocked:
            return "blocked by policy"
        if self.needs_decision:
            return "needs a decision"
        if self.conditions:
            return "clear, with conditions"
        return "clear"

    def explain(self) -> str:
        """What ops reads at the moment of deciding.

        Names the entry, where it came from, when, and how confident that
        source is. "Restricted category" on its own tells a person nothing they
        can act on; "their circular, January, and we have overridden it twice
        with both approved" tells them what to do.
        """
        if self.blocked and self.house_reason:
            return f"Blocked: {self.house_reason}"
        entry = self.deciding_entry
        if entry is None:
            return "No restriction on file."
        who = "our own policy" if entry.is_house else entry.acquirer_code
        rails = (
            ", on " + ",".join(i.value for i in entry.instruments)
            if entry.instruments
            else ""
        )
        return (
            f"{entry.enforcement.value.title()}: {entry.reason}{rails}. "
            f"Source {entry.source.value}, added {entry.added_on} by "
            f"{entry.added_by}, confidence {entry.confidence:.2f} ({who})."
        )


def assess(
    registry: Registry,
    acquirer_code: str,
    mcc: str,
    instruments: tuple[Instrument, ...] = (),
) -> Assessment:
    """Resolve one application against the registry and the acquirer policy."""
    eligibility = check(acquirer_code, mcc, instruments)
    entry = lookup(mcc)

    # The catalogue's own refusal. Kept ahead of the registry because it is a
    # statement about the business we are in, not about any bank's appetite,
    # and no registry edit should be able to widen it.
    if entry is not None and entry.risk.refused_outright:
        return Assessment(
            acquirer_code=acquirer_code,
            mcc=mcc,
            enforcement=Enforcement.BLOCK,
            eligibility=eligibility,
            house_reason=(
                f"{entry.sub_category} is a category this business does not "
                "onboard, whoever would accept it"
            ),
        )

    hits = registry.matching(acquirer_code, mcc, instruments)

    blocked: list[Instrument] = list(eligibility.blocked_instruments)
    conditions: list[str] = []
    for r in hits:
        if r.enforcement is Enforcement.NOTE:
            for i in r.instruments:
                if i in instruments and i not in blocked:
                    blocked.append(i)
            if r.condition and r.condition not in conditions:
                conditions.append(r.condition)

    strongest = max((r.enforcement for r in hits), key=lambda e: RANK[e], default=None)

    # A policy we cannot read is a decision for a person, even with no entry
    # against it. Treating "we do not know" as "fine" is how a merchant is sent
    # to a bank that was never going to take it.
    if eligibility.verdict is Verdict.UNKNOWN and strongest is None:
        strongest = Enforcement.WARN

    # Every requested rail barred is not a rail restriction, it is a refusal,
    # and it deserves the stronger level even if each entry was only a note.
    if instruments and blocked and all(i in blocked for i in instruments):
        if strongest is None or RANK[strongest] < RANK[Enforcement.WARN]:
            strongest = Enforcement.WARN

    return Assessment(
        acquirer_code=acquirer_code,
        mcc=mcc,
        enforcement=strongest,
        eligibility=eligibility,
        entries=hits,
        blocked_instruments=tuple(blocked),
        conditions=conditions,
    )


def assess_all(
    registry: Registry,
    acquirer_codes: list[str],
    mcc: str,
    instruments: tuple[Instrument, ...] = (),
) -> list[Assessment]:
    return [assess(registry, code, mcc, instruments) for code in acquirer_codes]


@dataclass
class Candidate:
    """A category every acquirer currently refuses, offered as house policy.

    Shown rather than applied. The intersection of the lists gets wider the
    fewer lists we hold, so inferring a block from it would make our rules
    stronger precisely where our data is thinnest. `coverage` is printed beside
    every candidate for that reason.
    """

    scope_value: str
    refusing: list[str]
    coverage: int  # how many acquirers we hold any policy for
    already_house: bool

    @property
    def unanimous(self) -> bool:
        return len(self.refusing) == self.coverage

    @property
    def confident(self) -> bool:
        """Unanimous means little across two lists. Four is a signal."""
        return self.unanimous and self.coverage >= 4


def house_candidates(registry: Registry, min_refusing: int = 2) -> list[Candidate]:
    """Categories several acquirers refuse, ranked, as candidates for house policy.

    Driven off the registry rather than the seeded policies, so an entry ops
    added this morning counts towards the overlap. That is the point of the
    view: the thing worth promoting to our own policy is the category the team
    keeps being told no about, and they are the ones recording the noes.

    Unanimity alone is not the signal. Two lists agreeing means little; four
    agreeing is a pattern. `coverage` is carried on every candidate so nobody
    reads "all of them" without seeing how many that was.
    """
    acquirers = sorted(registry.acquirers_with_policy())
    coverage = len(acquirers)
    house_blocked = {
        e.value
        for e in registry.live_entries(HOUSE)
        if e.enforcement is Enforcement.BLOCK
    }

    out: list[Candidate] = []
    for code, entry in sorted(CATALOGUE.items()):
        if entry.risk.refused_outright:
            continue  # the catalogue already refuses it; nothing to promote
        refusing = [
            acquirer
            for acquirer in acquirers
            if any(
                r.enforcement in (Enforcement.WARN, Enforcement.BLOCK)
                and not r.is_house
                for r in registry.matching(acquirer, code)
            )
        ]
        if len(refusing) >= min_refusing:
            out.append(
                Candidate(
                    scope_value=code,
                    refusing=refusing,
                    coverage=coverage,
                    already_house=code in house_blocked,
                )
            )
    return sorted(out, key=lambda c: (-len(c.refusing), c.scope_value))
