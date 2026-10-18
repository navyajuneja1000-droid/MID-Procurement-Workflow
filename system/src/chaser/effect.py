"""
Did the chasing actually do anything?

This is the part a follow-up tool normally skips, and skipping it is how a
team ends up with a process everyone maintains and nobody has ever tested.

**The naive comparison is wrong, and wrong in a flattering direction is worse
than wrong at random.** Comparing "rounds we chased" against "rounds we did
not" compares a group selected for being slow against a group that was mostly
fast. Chased rounds had already survived past the SLA, by definition. The
unchased group is full of banks that answered on day two. Put those side by
side and chasing looks like it makes banks slower, which is nonsense produced
by the selection, not by the chasing.

**The fix is to compare like with like.** Take only the rounds that were still
open on the day a chase would have gone out. Every one of them was eligible.
Some were chased and some were not, for reasons unrelated to how the bank was
going to behave: a cooldown, a batch that went out late, the queue being cut
at a sensible length. Then count the working days each took *from that day
onward*. That is a fair comparison, and the difference between the two arms is
the closest thing to an effect this data can support.

It is still not a randomised test, and the module says so in its output rather
than quietly presenting a difference as causal. The honest claim is "the
rounds we chased closed N days sooner from the same starting point", not
"chasing saves N days".
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass
class Round:
    """One acquirer response round, reduced to what the comparison needs."""

    request_id: str
    acquirer_code: str
    days_held_total: int  # working days the bank held the round, start to answer
    chased_on_day: int | None = None  # working day of the round a chase went out

    def still_open_on(self, day: int) -> bool:
        """Was this round still unanswered at the start of the given day?"""
        return self.days_held_total >= day

    def days_remaining_from(self, day: int) -> int:
        return self.days_held_total - day


@dataclass
class Comparison:
    day: int
    chased: list[int]
    not_chased: list[int]

    @property
    def n_chased(self) -> int:
        return len(self.chased)

    @property
    def n_not_chased(self) -> int:
        return len(self.not_chased)

    @property
    def mean_chased(self) -> float | None:
        return mean(self.chased) if self.chased else None

    @property
    def mean_not_chased(self) -> float | None:
        return mean(self.not_chased) if self.not_chased else None

    @property
    def difference(self) -> float | None:
        """Working days sooner the chased arm closed. Positive means the chased
        rounds finished earlier."""
        if self.mean_chased is None or self.mean_not_chased is None:
            return None
        return self.mean_not_chased - self.mean_chased

    @property
    def usable(self) -> bool:
        """Below this the difference is noise with a decimal point on it.

        Stated as a rule rather than left to whoever reads the report, because
        a number that small will otherwise be quoted in a review deck.
        """
        return self.n_chased >= 20 and self.n_not_chased >= 20

    def verdict(self) -> str:
        if not self.usable:
            return (
                f"Not enough matched rounds to say anything: {self.n_chased} chased "
                f"against {self.n_not_chased} not chased."
            )
        d = self.difference or 0.0
        if abs(d) < 0.3:
            return (
                "No measurable difference. On this evidence the chasing is not "
                "changing when banks answer, and the time spent on it is worth "
                "questioning."
            )
        if d > 0:
            return (
                f"Chased rounds closed {d:.1f} working days sooner from the same "
                "starting point. Not a randomised test, so this is an association, "
                "not a proven cause."
            )
        return (
            f"Chased rounds closed {abs(d):.1f} working days later from the same "
            "starting point. Worth looking at before doing more of it: the likely "
            "explanation is that something else about these rounds is driving both."
        )


def compare_from_day(rounds: list[Round], day: int) -> Comparison:
    """Compare chased against unchased rounds, among those still open on `day`.

    Restricting to rounds that survived to the same day is the whole method.
    Without it, the comparison measures which rounds were eligible for chasing
    rather than what chasing did to them.
    """
    chased: list[int] = []
    not_chased: list[int] = []

    for r in rounds:
        if not r.still_open_on(day):
            continue
        if r.chased_on_day is not None and r.chased_on_day > day:
            # Eligible but not yet due a chase on this day. Counting it as a
            # control would conflate "deliberately not chased" with "not chased
            # yet", which is how the first version of this reported four banks
            # as having no chases at all.
            continue
        remaining = r.days_remaining_from(day)
        (chased if r.chased_on_day is not None else not_chased).append(remaining)

    return Comparison(day=day, chased=chased, not_chased=not_chased)


def naive_compare(rounds: list[Round]) -> Comparison:
    """The comparison to avoid, kept so the report can show why.

    Every round, chased or not, with no matching on how long it had already
    run. Printing this next to the matched version is the clearest way to show
    that the obvious analysis points the wrong way.
    """
    chased = [r.days_held_total for r in rounds if r.chased_on_day is not None]
    not_chased = [r.days_held_total for r in rounds if r.chased_on_day is None]
    return Comparison(day=0, chased=chased, not_chased=not_chased)


@dataclass
class Stratified:
    """The comparison done within each acquirer, then pooled.

    Necessary because the chase day is a fixed function of each acquirer's
    SLA. Compare across acquirers and the chased arm is the banks with short
    SLAs and the unchased arm is the banks with long ones, so the difference
    is bank mix rather than chasing. Within one acquirer that confound is gone,
    because every round in the stratum is measured against the same clock.
    """

    per_acquirer: dict[str, Comparison]

    @property
    def usable_strata(self) -> dict[str, Comparison]:
        return {k: c for k, c in self.per_acquirer.items() if c.usable}

    @property
    def pooled_difference(self) -> float | None:
        """Each acquirer's difference, weighted by how many chased rounds it
        contributed. An acquirer with four rounds should not move the headline
        as much as one with ninety."""
        usable = self.usable_strata
        if not usable:
            return None
        total = sum(c.n_chased for c in usable.values())
        if not total:
            return None
        return sum(
            (c.difference or 0.0) * c.n_chased for c in usable.values()
        ) / total

    def verdict(self) -> str:
        usable = self.usable_strata
        if not usable:
            counts = ", ".join(
                f"{k}: {c.n_chased} chased / {c.n_not_chased} held out"
                for k, c in sorted(self.per_acquirer.items())
            )
            return (
                "No acquirer has enough rounds on both sides to compare yet. "
                f"({counts}) The holdout needs to run for longer before this "
                "number means anything."
            )
        d = self.pooled_difference or 0.0
        where = f"across {len(usable)} acquirer(s) with enough data"
        if abs(d) < 0.3:
            return (
                f"No measurable difference {where}. On this evidence the chasing "
                "is not changing when banks answer."
            )
        if d > 0:
            return f"Chased rounds closed {d:.1f} working days sooner {where}."
        return (
            f"Chased rounds closed {abs(d):.1f} working days later {where}, which "
            "usually means something other than the chase is driving both."
        )


def compare_stratified(rounds: list[Round], day_by_acquirer: dict[str, int]) -> Stratified:
    """Run the matched comparison inside each acquirer, on that acquirer's own
    chase day.

    One shared day does not work: a bank whose chase goes out on day 7 has
    nobody chased on day 6, so every one of its rounds lands in the control arm
    and the stratum reports zero chases. Each acquirer is compared at the point
    where its own chase would have gone out, which is also the point at which
    the two arms become comparable: both have run past that bank's SLA, one got
    an email and one did not.
    """
    by_acquirer: dict[str, list[Round]] = {}
    for r in rounds:
        by_acquirer.setdefault(r.acquirer_code, []).append(r)
    return Stratified(
        per_acquirer={
            code: compare_from_day(rs, day_by_acquirer[code])
            for code, rs in sorted(by_acquirer.items())
            if code in day_by_acquirer
        }
    )
