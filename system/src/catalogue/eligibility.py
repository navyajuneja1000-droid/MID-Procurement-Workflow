"""
Can this acquirer take this merchant at all?

The readiness check already answers "is the paperwork complete for this
acquirer". This answers the question that comes before it: will this acquirer
accept this line of business, whatever the paperwork says.

**The finding that drives this module: there is no single restricted list.**
Each acquirer keeps its own, and they do not agree with each other or even
share a format. Four shapes turn up in practice:

- a list of MCC codes with a prohibited or restricted flag against each
- MCC codes split by instrument, so a code is fine on one rail and not another
- MCC codes with a separate, narrower ban list for one product (EMI, say)
- a page of prose policy with no codes at all, written as business categories

A merchant on one MCC can therefore be eligible at one acquirer, conditionally
acceptable at a second, and prohibited at a third. That is the whole reason
this is decided per acquirer rather than once.

Two consequences worth being explicit about:

1. **A prose policy cannot be resolved with certainty.** Matching "alcohol" in
   a paragraph against a category tree is a judgement, not a lookup. Those
   results carry lower confidence and say so, rather than pretending the
   acquirer published a code list it did not publish.
2. **The strictest source wins.** If the catalogue refuses a code outright,
   no acquirer policy can make it eligible. Acquirer policy can only narrow.

Every policy here is invented. The shapes follow ordinary practice; the
content belongs to this project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .mcc import Instrument, MccEntry, Risk, lookup, resolve


class Verdict(str, Enum):
    ELIGIBLE = "eligible"
    # Acceptable, but something must be produced or agreed first: a licence,
    # a risk sign-off, a separate commercial agreement.
    CONDITIONAL = "conditional"
    PROHIBITED = "prohibited"
    # The policy could not be resolved for this code with confidence. Goes to
    # a person. Never silently treated as eligible.
    UNKNOWN = "unknown"


class PolicyShape(str, Enum):
    CODED = "coded"  # MCC list with prohibited/restricted flags
    CODED_BY_INSTRUMENT = "coded_by_instrument"  # per-rail MCC list
    CODED_WITH_PRODUCT_BAN = "coded_with_product_ban"  # plus a narrower product list
    PROSE = "prose"  # policy paragraphs, no codes


@dataclass
class AcquirerPolicy:
    acquirer_code: str
    shape: PolicyShape
    # CODED and friends
    prohibited_codes: set[str] = field(default_factory=set)
    restricted_codes: set[str] = field(default_factory=set)
    # CODED_BY_INSTRUMENT: code -> instruments this acquirer will not run
    instrument_bans: dict[str, tuple[Instrument, ...]] = field(default_factory=dict)
    # CODED_WITH_PRODUCT_BAN: a narrower list for one product only
    product_ban_label: str = ""
    product_banned_codes: set[str] = field(default_factory=set)
    # PROSE: phrases drawn from a written policy, matched against the tree
    prose_prohibited: tuple[str, ...] = ()
    prose_restricted: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class EligibilityResult:
    acquirer_code: str
    verdict: Verdict
    reason: str
    confidence: float = 1.0
    condition: str = ""
    blocked_instruments: tuple[Instrument, ...] = ()
    source: str = ""

    @property
    def can_raise(self) -> bool:
        """Conditional still goes out, with the condition attached. Only a
        prohibition stops the request, and an unknown waits for a person."""
        return self.verdict in (Verdict.ELIGIBLE, Verdict.CONDITIONAL)


# --- the six policies, deliberately in four different shapes ---------------

POLICIES: dict[str, AcquirerPolicy] = {
    # The conservative one. A long list, and it refuses several categories the
    # others merely restrict. Breadth of policy is itself a commercial
    # position: this acquirer competes on low dispute rates, not on coverage.
    "ALDRIDGE": AcquirerPolicy(
        acquirer_code="ALDRIDGE",
        shape=PolicyShape.CODED,
        prohibited_codes={
            "7521", "7527", "7630", "7616", "7229", "7140", "7733", "7712",
        },
        restricted_codes={
            "7134", "7726", "7501", "7515", "7224", "7428", "7747", "7120",
            "7609", "7301",
        },
        notes="The long list. Refuses consulting and test prep outright, which "
              "most acquirers only restrict.",
    ),
    # The permissive one. Bans almost nothing and prices the risk in instead.
    # A merchant refused elsewhere often lands here, at a worse rate.
    "CASTERLY": AcquirerPolicy(
        acquirer_code="CASTERLY",
        shape=PolicyShape.CODED,
        prohibited_codes={"7521", "7527", "7630"},
        restricted_codes={"7616", "7224"},
        notes="The short list: three prohibitions, everything else priced rather "
              "than refused. The fallback when a category is blocked elsewhere.",
    ),
    # Per-instrument. A code can be fine on one rail and barred on another, so
    # the answer depends on what the merchant asked for.
    "BRINDLE": AcquirerPolicy(
        acquirer_code="BRINDLE",
        shape=PolicyShape.CODED_BY_INSTRUMENT,
        prohibited_codes={"7521", "7527", "7630", "7229", "7140"},
        restricted_codes={"7134", "7616", "7428"},
        instrument_bans={
            "7726": (Instrument.CREDIT_CARD, Instrument.CREDIT_LINE_UPI),
            "7501": (Instrument.CREDIT_CARD, Instrument.CREDIT_LINE_UPI, Instrument.OVERDRAFT),
            "7515": (Instrument.CREDIT_CARD, Instrument.CREDIT_LINE_UPI),
            "7134": (Instrument.EMI,),
            "7301": (Instrument.EMI, Instrument.CREDIT_LINE_UPI),
            "7308": (Instrument.EMI,),
            "7747": (Instrument.CREDIT_CARD,),
            "7609": (Instrument.EMI,),
            "7120": (Instrument.CREDIT_LINE_UPI,),
        },
        notes="Few outright bans, but the longest instrument-level list: nine "
              "categories are open on one rail and closed on another.",
    ),
    # A coded list plus a separate, narrower ban for one product.
    "NORTHWAY_CYBS": AcquirerPolicy(
        acquirer_code="NORTHWAY_CYBS",
        shape=PolicyShape.CODED_WITH_PRODUCT_BAN,
        prohibited_codes={"7521", "7527", "7630", "7616", "7428"},
        restricted_codes={"7134", "7140", "7224", "7501", "7515", "7733"},
        product_ban_label="credit card EMI",
        product_banned_codes={
            "7134", "7140", "7301", "7308", "7712", "7733", "7747", "7609",
            "7726", "7224", "7501",
        },
        notes="Maintains a second, stricter list for EMI only, roughly twice the "
              "length of its main restricted list.",
    ),
    # Same bank, other platform, and the policies differ. Worth keeping: it is
    # a real pattern and it catches anyone assuming one policy per bank.
    "NORTHWAY_FSS": AcquirerPolicy(
        acquirer_code="NORTHWAY_FSS",
        shape=PolicyShape.CODED,
        prohibited_codes={"7521", "7527", "7630", "7616", "7726", "7747"},
        restricted_codes={"7134", "7224", "7501", "7515", "7140", "7120"},
        notes="Same bank as CYBS, different platform, different list. It refuses "
              "rent and donations outright where CYBS only restricts them, which "
              "is why policy is held per rail rather than per bank.",
    ),
    # Prose. No codes at all, which is why its answers carry lower confidence.
    "VERRAN": AcquirerPolicy(
        acquirer_code="VERRAN",
        shape=PolicyShape.PROSE,
        prose_prohibited=(
            "adult goods and services",
            "money transfer, remittance or quasi-cash",
            "virtual digital assets and cryptocurrency",
            "tobacco and tobacco products",
            "gambling, betting and real money gaming",
            "rent and property dues paid on credit",
        ),
        prose_restricted=(
            "alcohol and alcoholic beverages",
            "precious metals, jewellery and bullion",
            "lending and credit facilities",
            "securities, broking and investment platforms",
            "charitable and religious donations",
            "second-hand and refurbished goods",
        ),
        notes="Publishes policy paragraphs rather than an MCC list, so matching "
              "is a judgement and the result is flagged as such.",
    ),
}


# Phrases from a prose policy, mapped onto the tree. Each entry lists the
# words that have to appear in an MCC's own category text for the policy line
# to be considered a match. Kept explicit rather than fuzzy, because a loose
# match here silently refuses a merchant nobody meant to refuse.
_PROSE_HOOKS: dict[str, tuple[str, ...]] = {
    "adult goods and services": ("adult",),
    "money transfer, remittance or quasi-cash": ("money transfer", "quasi-cash", "remittance"),
    "virtual digital assets and cryptocurrency": ("virtual digital assets", "crypto"),
    "tobacco and tobacco products": ("tobacco",),
    "gambling, betting and real money gaming": ("real money gaming", "gambling", "betting"),
    "rent and property dues paid on credit": ("rent collection", "rent"),
    "alcohol and alcoholic beverages": ("alcohol", "liquor"),
    "precious metals, jewellery and bullion": ("jewellery", "precious metals", "bullion"),
    "lending and credit facilities": ("lending", "lenders"),
    "securities, broking and investment platforms": ("securities", "brokerage", "broking"),
    "charitable and religious donations": ("donations", "charity"),
    "second-hand and refurbished goods": ("second-hand", "refurbished"),
}


def _entry_text(entry: MccEntry) -> str:
    return " ".join(
        [entry.super_category, entry.category, entry.sub_category, entry.description]
    ).lower()


def _match_prose(entry: MccEntry, lines: tuple[str, ...]) -> str | None:
    text = _entry_text(entry)
    for line in lines:
        for hook in _PROSE_HOOKS.get(line, ()):
            if hook in text:
                return line
    return None


def check(
    acquirer_code: str,
    mcc: str,
    instruments: tuple[Instrument, ...] = (),
) -> EligibilityResult:
    """Will this acquirer take this line of business?

    Order matters. The catalogue is checked first, because a code refused
    there is refused everywhere and no acquirer policy can widen it. Only then
    does the acquirer's own policy narrow things further.
    """
    policy = POLICIES.get(acquirer_code)
    entry = lookup(mcc)

    if entry is None:
        return EligibilityResult(
            acquirer_code, Verdict.UNKNOWN,
            f"MCC {mcc} is not in the catalogue", confidence=0.0, source="catalogue",
        )
    if policy is None:
        return EligibilityResult(
            acquirer_code, Verdict.UNKNOWN,
            f"no policy on file for {acquirer_code}", confidence=0.0, source="policy",
        )

    # 1. The catalogue's own refusal beats everything.
    if entry.risk.refused_outright:
        return EligibilityResult(
            acquirer_code, Verdict.PROHIBITED,
            f"{entry.sub_category} is a restricted category across all acquirers",
            source="catalogue",
        )

    # 2. The acquirer's prohibitions.
    if policy.shape is PolicyShape.PROSE:
        hit = _match_prose(entry, policy.prose_prohibited)
        if hit:
            return EligibilityResult(
                acquirer_code, Verdict.PROHIBITED,
                f"policy prohibits '{hit}'",
                # A prose match is an interpretation, not a lookup. Flagged
                # so a reviewer can confirm it rather than take it as fact.
                confidence=0.7, source="prose policy",
            )
    elif mcc in policy.prohibited_codes:
        return EligibilityResult(
            acquirer_code, Verdict.PROHIBITED,
            f"MCC {mcc} is on {acquirer_code}'s prohibited list", source="acquirer list",
        )

    # 3. Instrument-level bans, from the catalogue and from the acquirer.
    res = resolve(mcc, instruments)
    blocked = set(res.blocked_instruments) if res else set()
    blocked |= {i for i in instruments if i in policy.instrument_bans.get(mcc, ())}

    if policy.shape is PolicyShape.CODED_WITH_PRODUCT_BAN and mcc in policy.product_banned_codes:
        if Instrument.EMI in instruments:
            blocked.add(Instrument.EMI)

    if instruments and blocked == set(instruments):
        return EligibilityResult(
            acquirer_code, Verdict.PROHIBITED,
            "every requested instrument is barred for this category",
            blocked_instruments=tuple(sorted(blocked, key=lambda i: i.value)),
            source="instrument rules",
        )

    # 4. Conditions: a licence from the catalogue, or restriction by policy.
    conditions: list[str] = []
    if entry.risk.needs_licence:
        conditions.append(entry.licence_required or "regulatory licence")

    restricted_here = False
    confidence = 1.0
    source = "acquirer list"
    if policy.shape is PolicyShape.PROSE:
        hit = _match_prose(entry, policy.prose_restricted)
        if hit:
            restricted_here = True
            confidence = 0.7
            source = "prose policy"
            conditions.append(f"risk sign-off, policy restricts '{hit}'")
    elif mcc in policy.restricted_codes:
        restricted_here = True
        conditions.append("risk sign-off for a restricted category")

    if entry.risk is Risk.HIGH and not restricted_here:
        conditions.append("enhanced due diligence for a high-risk category")

    if conditions or blocked:
        detail = "; ".join(conditions) if conditions else "instrument limits apply"
        return EligibilityResult(
            acquirer_code, Verdict.CONDITIONAL, detail,
            confidence=confidence, condition=detail,
            blocked_instruments=tuple(sorted(blocked, key=lambda i: i.value)),
            source=source,
        )

    return EligibilityResult(
        acquirer_code, Verdict.ELIGIBLE,
        f"{entry.sub_category} is accepted, risk {entry.risk.value}",
        source="acquirer list",
    )


def check_all(
    acquirer_codes: list[str], mcc: str, instruments: tuple[Instrument, ...] = ()
) -> list[EligibilityResult]:
    return [check(code, mcc, instruments) for code in acquirer_codes]


def prose_matches_entry(line: str, entry: MccEntry) -> bool:
    """Does one line of a prose policy cover this catalogue entry?

    Exposed because the restriction registry stores prose policy at category
    scope and has to resolve the same phrases. Two matchers would drift, and a
    drift here means the registry and the check disagree about what a paragraph
    means.
    """
    return _match_prose(entry, (line,)) is not None
