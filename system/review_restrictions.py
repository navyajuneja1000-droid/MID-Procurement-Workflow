"""
The restriction registry, end to end.

    python review_restrictions.py

What it does, in order:

  1. Loads the registry, seeding it from the phase 7 policies the first time.
  2. Assesses every open request against it, and reports what today's queue is
     waiting on: our own policy, an acquirer's list, or nothing.
  3. Replays the requests that already have an answer from the bank as if the
     restriction had been flagged and ops had decided, then reports what the
     banks did with those decisions. Marked as a replay in the output, because
     it is reconstructed from outcomes rather than collected from real
     decisions.
  4. Says which entries the evidence supports and which look stale.
  5. Lists what every acquirer refuses, as candidates for house policy, with
     the coverage caveat attached.

Writes data/restriction_report.txt.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime
from pathlib import Path

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.catalogue.assess import assess, house_candidates
from src.catalogue.mcc import CATALOGUE
from src.catalogue.overrides import (
    OverrideLog,
    Reason,
    outcome_from_rejection,
    override_and_raise,
)
from src.catalogue.restrictions import HOUSE, Enforcement, Registry
from src.generator import DataGenerator
from src.model import EventType, RequestStatus

AS_OF = datetime(2026, 10, 1, 18, 0)
DATA = Path("data")

# Why ops went ahead, in the proportions a real log tends to show: mostly a
# sign-off that already existed, occasionally a belief that the list is wrong.
REASON_WEIGHTS = {
    Reason.RISK_SIGNED_OFF: 6,
    Reason.LICENCE_ON_FILE: 3,
    Reason.RM_CONFIRMED: 4,
    Reason.EXCEPTION_AGREED: 2,
    Reason.ENTRY_LOOKS_STALE: 2,
    Reason.COMMERCIAL_PRIORITY: 1,
}


def main() -> None:
    cal = load_calendar(HOLIDAYS)
    gen = DataGenerator(ACQUIRERS, cal, seed=42, as_of=AS_OF)
    data = gen.generate()

    registry = Registry.load()
    rng = random.Random(7)

    lines = [
        "RESTRICTION REGISTRY",
        f"{len(registry.live_entries())} live entries across "
        f"{len(registry.acquirers_with_policy())} acquirers, plus house policy",
        "",
        "By strength",
    ]
    strength = Counter(e.enforcement.value for e in registry.live_entries())
    for level, note in (
        ("block", "our own policy; nobody raises it and no document is chased"),
        ("warn", "a bank's list says no; ops decides and the decision is recorded"),
        ("note", "goes out as normal, with the condition stated"),
    ):
        lines.append(f"  {level:<8}{strength.get(level, 0):>5}   {note}")

    lines += ["", "By source, because a circular and a phone call are not the same claim"]
    for source, n in Counter(e.source.value for e in registry.live_entries()).most_common():
        from src.catalogue.restrictions import CONFIDENCE, Source

        lines.append(f"  {source:<20}{n:>5}   confidence {CONFIDENCE[Source(source)]:.2f}")

    lines += ["", "By scope"]
    for scope, n in Counter(e.scope.value for e in registry.live_entries()).most_common():
        note = {
            "mcc": "written against a single code",
            "category": "written against a category, so a new code in it is covered",
            "super_category": "written against a whole super category",
        }[scope]
        lines.append(f"  {scope:<16}{n:>5}   {note}")

    # --- today's queue ---------------------------------------------------

    open_requests = [
        r for r in data.requests
        if r.status is RequestStatus.TO_BE_RAISED and not r.is_deprioritised
    ]
    buckets: Counter = Counter()
    pending: list[tuple] = []
    for r in open_requests:
        if r.mcc not in CATALOGUE:
            continue
        a = assess(registry, r.acquirer.code, r.mcc, r.instruments)
        buckets[a.status] += 1
        if a.needs_decision:
            pending.append((r, a))

    lines += [
        "",
        "=" * 70,
        f"TODAY'S QUEUE: {len(open_requests)} requests waiting",
        "=" * 70,
        "",
    ]
    for status in ("clear", "clear, with conditions", "needs a decision", "blocked by policy"):
        lines.append(f"  {status:<24}{buckets.get(status, 0):>5}")
    lines += [
        "",
        f"  {len(pending)} applications cannot go out on their own. None of them is",
        "  refused: each one has an entry on file saying a bank said no, and a",
        "  person has to decide whether that is still true.",
        "",
    ]

    for r, a in pending[:3]:
        lines += [
            f"  {r.merchant.legal_name} to {r.acquirer.name}",
            f"    {CATALOGUE[r.mcc].sub_category} ({r.mcc})",
            f"    {a.explain()}",
            "",
        ]

    # --- the replay ------------------------------------------------------

    log = OverrideLog()
    answered = [
        r for r in data.requests
        if r.status in (RequestStatus.PROCURED, RequestStatus.APPROVED, RequestStatus.REJECTED)
        and r.mcc in CATALOGUE
    ]

    for r in answered:
        a = assess(registry, r.acquirer.code, r.mcc, r.instruments)
        if not a.needs_decision:
            continue
        # The decision would have been taken when the request was raised.
        raised = next(
            (e.at for e in r.events if e.event_type is EventType.RAISED_TO_ACQUIRER),
            None,
        )
        if raised is None:
            continue
        reason = rng.choices(
            list(REASON_WEIGHTS), weights=list(REASON_WEIGHTS.values()), k=1
        )[0]
        # Recorded against a copy of the request, so the replay does not write
        # override events into the dataset the other reports read.
        override = log.record(
            request_id=r.request_id,
            acquirer_code=r.acquirer.code,
            mcc=r.mcc,
            entry_id=(a.deciding_entry.entry_id if a.deciding_entry else "catalogue"),
            reason=reason,
            actor="ops",
            note=a.explain(),
            on=raised.date(),
        )
        rejection = next(
            (e.rejection_reason for e in r.events
             if e.event_type is EventType.ACQUIRER_REJECTED),
            None,
        )
        outcome, detail = outcome_from_rejection(
            rejection.value if rejection is not None else None
        )
        log.resolve(r.request_id, outcome, detail)
        _ = override

    lines += [
        "=" * 70,
        "WHAT HAPPENED TO THE OVERRIDES",
        "=" * 70,
        "",
        f"  {len(log.overrides)} decisions replayed from requests the banks have",
        "  already answered. Replayed, not collected: the dataset holds the bank's",
        "  answer, and this reconstructs the decision that would have preceded it.",
        "",
    ]
    for outcome, n in sorted(log.outcome_counts().items(), key=lambda kv: -kv[1]):
        note = {
            "approved": "the bank took it, so the entry is wrong or out of date",
            "rejected": "the bank refused on the category, so the entry holds up",
            "withdrawn": "refused for an unrelated reason, which proves nothing either way",
            "pending": "still open",
        }[outcome]
        lines.append(f"  {outcome:<12}{n:>5}   {note}")

    rate = log.rejection_rate
    if rate is not None:
        lines += [
            "",
            f"  Of the overrides that got a category answer, {rate:.0%} were refused.",
            "",
            "  This is the number that decides whether the warnings are set right. Low,",
            "  and the restrictions are over-cautious and ops is right to push. High,",
            "  and ops is spending response rounds on applications that were never",
            "  going to land, and those entries should probably become blocks.",
        ]

    lines += ["", "Why ops went ahead"]
    for reason, n in sorted(log.by_reason().items(), key=lambda kv: -kv[1]):
        lines.append(f"  {reason:<22}{n:>5}")

    # --- what the evidence says about each entry -------------------------

    verdicts: dict[str, tuple[str, str]] = {}
    for entry_id in {o.entry_id for o in log.overrides}:
        verdicts[entry_id] = (log.verdict_on(entry_id), log.history_for(entry_id))

    stale = [k for k, (v, _) in verdicts.items() if v.startswith("looks stale")]
    holds = [k for k, (v, _) in verdicts.items() if v == "entry holds up"]

    lines += [
        "",
        "=" * 70,
        "WHICH ENTRIES THE EVIDENCE SUPPORTS",
        "=" * 70,
        "",
        f"  {len(holds)} entries hold up: every override was refused on the category.",
        f"  {len(stale)} look stale: three or more overrides approved and none refused.",
        "",
    ]
    for entry_id in sorted(stale)[:6]:
        entry = registry.get(entry_id)
        lines += [
            f"  {entry_id}",
            f"    {entry.reason}",
            f"    {verdicts[entry_id][1]}  ->  {verdicts[entry_id][0]}",
            "",
        ]
    if stale:
        lines += [
            "  Nothing is retired automatically. A bank quietly loosening a category is",
            "  the best news this registry gets, and it still needs someone to confirm",
            "  it before the entry comes off: three approvals could also be three",
            "  applications that slipped through underwriting.",
            "",
        ]

    # --- house policy candidates ----------------------------------------

    candidates = house_candidates(registry)
    coverage = len(registry.acquirers_with_policy())
    lines += [
        "=" * 70,
        "CANDIDATES FOR HOUSE POLICY",
        "=" * 70,
        "",
        f"  Categories more than one acquirer refuses, out of {coverage} we hold a",
        "  list for. Shown, never applied: the overlap gets wider the fewer lists we",
        "  hold, so inferring a block from it would make our rules strongest exactly",
        "  where our data is thinnest.",
        "",
    ]
    if candidates:
        for c in candidates:
            flag = "  (already house policy)" if c.already_house else ""
            confident = "promote" if c.confident else "watch"
            lines.append(
                f"  {c.scope_value}  {CATALOGUE[c.scope_value].sub_category:<30}"
                f"refused by {len(c.refusing)}/{c.coverage}  {confident}{flag}"
            )
        lines += [
            "",
            "  refusing: " + ", ".join(candidates[0].refusing)
            + f"  (for {candidates[0].scope_value})",
        ]
    else:
        lines.append("  None. No category is refused by more than one acquirer.")

    house = registry.live_entries(HOUSE)
    lines += [
        "",
        f"  House policy currently holds {len(house)} blocking entries:",
    ]
    for e in house:
        lines.append(f"    {e.value}  {e.reason}")

    # --- the change log --------------------------------------------------

    lines += [
        "",
        "=" * 70,
        "CHANGE LOG",
        "=" * 70,
        "",
        f"  {len(registry.changes)} recorded changes. Nothing is deleted: an entry that",
        "  no longer applies is retired with a date and a reason, because the question",
        "  asked afterwards is always when a bank changed its position and on whose",
        "  word, and a deleted row cannot answer it.",
        "",
    ]
    for c in registry.changes[:4]:
        lines.append(f"  {c.at}  {c.actor:<10}{c.action:<22}{c.entry_id}")
    lines.append(f"  ... and {max(0, len(registry.changes) - 4)} more")

    report = "\n".join(lines)
    DATA.mkdir(exist_ok=True)
    (DATA / "restriction_report.txt").write_text(report + "\n")
    registry.save()
    log.save()
    print(report)


if __name__ == "__main__":
    main()
