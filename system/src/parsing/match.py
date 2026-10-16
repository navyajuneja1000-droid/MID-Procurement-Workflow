"""
Match a row from a bank reply back to an open request.

The bank types the merchant's legal name its own way: a different suffix form,
dropped punctuation, upper case, the occasional typo. So the join is on legal
name plus MCC with a tolerance, which is exactly how it is done by hand today.

Three rules decide what this module does:

1. **MCC is part of the key, not a tiebreak.** One merchant can run several use
   cases, each with its own MCC and its own MID request. Matching on name alone
   would stop the wrong clock for a merchant with two live applications.
2. **Only open requests are candidates.** A reply cannot be about a request
   that was already decided, and allowing closed ones back in would let a
   duplicate email reopen a settled case.
3. **Ambiguity is reported, never resolved by guessing.** Two candidates
   scoring within a whisker of each other go to a human. A wrong match writes
   a wrong event into the clock, and a wrong clock is worse than a slow one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Suffix forms that all mean the same entity type. Normalising these removes
# most of the drift before any fuzzy comparison happens.
SUFFIX_CANON = {
    "private limited": "pvt",
    "pvt ltd": "pvt",
    "pvt limited": "pvt",
    "private ltd": "pvt",
    "p ltd": "pvt",
    "llp": "llp",
    "limited": "ltd",
    "ltd": "ltd",
}

PUNCT = re.compile(r"[.,'()\[\]-]")
SPACE = re.compile(r"\s+")

# Scores at or above this are applied automatically; below it, a human looks.
AUTO_APPLY = 0.88
# A second candidate this close to the best one means the row is ambiguous.
AMBIGUITY_GAP = 0.04


def normalise(name: str) -> str:
    """Strip the things that drift: case, punctuation, spacing, suffix form."""
    s = PUNCT.sub(" ", (name or "").lower())
    s = SPACE.sub(" ", s).strip()
    # Longest suffix first, so "private limited" is not eaten by "limited".
    for form in sorted(SUFFIX_CANON, key=len, reverse=True):
        if s.endswith(" " + form) or s == form:
            return SPACE.sub(" ", s[: -len(form)].strip() + " " + SUFFIX_CANON[form]).strip()

    # No exact suffix. A misspelled one ("Limtied", "Privte Limited") is still
    # that suffix, and it is only the entity type, not the distinctive part of
    # the name, so correcting it cannot merge two different merchants. Tying
    # this to a similarity test rather than a list of known typos means the
    # next misspelling is handled too.
    tail = s.rsplit(" ", 2)[-2:]
    for size in (2, 1):
        if len(tail) < size:
            continue
        candidate = " ".join(tail[-size:])
        for form, canon in SUFFIX_CANON.items():
            if abs(len(candidate) - len(form)) <= 2 and SequenceMatcher(
                None, candidate, form
            ).ratio() >= 0.82:
                head = s[: -len(candidate)].strip()
                return SPACE.sub(" ", f"{head} {canon}").strip()
    # "India" is deliberately NOT stripped. It looks like noise in a legal
    # name, but "Oakhurst Ventures India Private Limited" and "Oakhurst
    # Ventures Pvt Ltd" are two different entities, and removing it collapses
    # them into one. The matcher then joins a reply to the wrong request at
    # full confidence, which is the worst failure available here: a wrong
    # event written into the clock with nothing flagged for review.
    return SPACE.sub(" ", s).strip()


def similarity(a: str, b: str) -> float:
    """Character similarity on normalised names, with a bonus when the
    distinctive leading words agree. Merchant names here lead with the
    distinctive part and end with a generic suffix, so an agreeing first token
    is worth more than an agreeing last one."""
    na, nb = normalise(a), normalise(b)
    if na == nb:
        return 1.0

    base = SequenceMatcher(None, na, nb).ratio()
    ta, tb = na.split(), nb.split()
    if ta and tb and ta[0] == tb[0]:
        base = min(1.0, base + 0.06)
    return base


@dataclass
class Candidate:
    request_id: str
    score: float
    name: str
    mcc: str


@dataclass
class MatchResult:
    request_id: str | None
    confidence: float
    reason: str
    alternatives: list[Candidate]

    @property
    def auto(self) -> bool:
        return self.request_id is not None and self.confidence >= AUTO_APPLY


class RequestIndex:
    """The open requests a reply could be about, grouped by acquirer and MCC."""

    def __init__(self, rows: list[dict]) -> None:
        """`rows` carry request_id, name, mcc, acquirer and open (bool)."""
        self._by_key: dict[tuple[str, str], list[dict]] = {}
        self._by_acquirer: dict[str, list[dict]] = {}
        for r in rows:
            if not r.get("open", True):
                continue
            self._by_key.setdefault((r["acquirer"], r["mcc"]), []).append(r)
            self._by_acquirer.setdefault(r["acquirer"], []).append(r)

    def match(self, acquirer: str, name: str, mcc: str) -> MatchResult:
        pool = self._by_key.get((acquirer, mcc)) if mcc else None
        narrowed_by_mcc = bool(pool)

        if not pool:
            # No MCC on the row, or no open request at that MCC. Fall back to
            # the acquirer's whole open book, and say so: a match found this
            # way is weaker and should clear a higher bar before it is applied.
            pool = self._by_acquirer.get(acquirer, [])

        if not pool:
            return MatchResult(None, 0.0, f"no open requests for {acquirer}", [])

        scored = sorted(
            (
                Candidate(r["request_id"], similarity(name, r["name"]), r["name"], r["mcc"])
                for r in pool
            ),
            key=lambda c: -c.score,
        )
        best = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None

        if runner_up and best.score - runner_up.score < AMBIGUITY_GAP and best.score < 1.0:
            return MatchResult(
                None,
                best.score,
                f"ambiguous: '{best.name}' and '{runner_up.name}' score within "
                f"{AMBIGUITY_GAP} of each other",
                scored[:3],
            )

        confidence = best.score
        reason = f"name similarity {best.score:.2f}"

        if not narrowed_by_mcc:
            # Matched without the MCC narrowing the field. Discount it, so a
            # name-only match has to be near perfect to go through unseen.
            confidence *= 0.85
            reason += " (MCC did not narrow the candidates)"

        if confidence < AUTO_APPLY:
            return MatchResult(
                best.request_id if best.score > 0.6 else None,
                confidence,
                reason + " — below the auto-apply bar",
                scored[:3],
            )

        return MatchResult(best.request_id, confidence, reason, scored[:3])
