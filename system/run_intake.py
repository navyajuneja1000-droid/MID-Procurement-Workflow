"""
Run the intake check over every merchant and report what it would have caught.

    python run_intake.py

The question this answers is not "does the check work" but "what would it have
changed". Two figures matter:

  caught on arrival   gaps the check finds the moment the ticket reaches ops,
                      rather than days later while someone fills a template
  partial raises      merchants where some acquirers could go out today while
                      the rest wait, instead of the whole merchant waiting
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.catalogue.restrictions import Registry
from src.generator import DataGenerator
from src.intake.readiness import check_intake
from src.intake.validate import label_for
from src.model import Party

AS_OF = datetime(2026, 10, 1, 18, 0)


def main() -> None:
    cal = load_calendar(HOLIDAYS)
    # Loaded once rather than per request: it is a file, and this loop runs
    # several hundred times.
    registry = Registry.load()
    gen = DataGenerator(ACQUIRERS, cal, seed=42, as_of=AS_OF)
    data = gen.generate()

    # Which acquirers each merchant-and-MCC was raised to. The MCC belongs to
    # the request, not the merchant, because one merchant can run several use
    # cases. So the check runs per use case: a merchant with two MCCs is two
    # intake checks, and one can be ready while the other is not.
    targets: dict[tuple[str, str], list[str]] = {}
    instruments: dict[tuple[str, str], tuple] = {}
    merchants = {m.merchant_id: m for m in data.merchants}
    for r in data.requests:
        key = (r.merchant.merchant_id, r.mcc)
        targets.setdefault(key, [])
        instruments.setdefault(key, r.instruments)
        if r.acquirer.code not in targets[key]:
            targets[key].append(r.acquirer.code)

    fully_ready = 0
    partial = 0
    fully_blocked = 0
    field_counts: Counter = Counter()
    level_counts: Counter = Counter()
    party_counts: Counter = Counter()
    acq_blocked: Counter = Counter()
    acq_ineligible: Counter = Counter()
    acq_decision: Counter = Counter()
    verdicts: Counter = Counter()
    divergent = 0
    conditional_cases = 0
    examples = []
    divergent_examples = []

    for (merchant_id, mcc), codes in targets.items():
        m = merchants[merchant_id]
        acqs = [ACQUIRERS[c] for c in codes]
        result = check_intake(
            m, acqs, mcc=mcc, instruments=instruments[(merchant_id, mcc)],
            registry=registry,
        )

        if result.fully_ready:
            fully_ready += 1
        elif result.can_partially_raise:
            partial += 1
        else:
            fully_blocked += 1

        seen_fields = set()
        for a in result.per_acquirer:
            if a.assessment:
                verdicts[a.assessment.status] += 1
            if a.refused:
                acq_ineligible[a.acquirer_code] += 1
            if a.needs_decision:
                acq_decision[a.acquirer_code] += 1
            if not a.ready:
                acq_blocked[a.acquirer_code] += 1
            for f in a.findings:
                if f.field not in seen_fields:
                    seen_fields.add(f.field)
                    field_counts[f.field] += 1
                    level_counts[f.level] += 1

        for b in result.bounces:
            party_counts[b.party.value] += 1

        if result.conditions:
            conditional_cases += 1

        # The case that cannot be answered once for the merchant: the same MCC
        # read differently by the acquirers it was raised to.
        seen_verdicts = {
            a.assessment.status for a in result.per_acquirer if a.assessment
        }
        if len(seen_verdicts) > 1:
            divergent += 1
            # Keep the widest disagreement rather than the first one seen. The
            # first is usually a two-way split, which understates the finding.
            if result.awaiting_decision:
                if not divergent_examples or len(seen_verdicts) > divergent_examples[0][2]:
                    divergent_examples = [(mcc, result, len(seen_verdicts))]

        if result.can_partially_raise and len(examples) < 2:
            examples.append(result)

    total = fully_ready + partial + fully_blocked

    def pct(n: int) -> str:
        return f"{n / total * 100:4.1f}%" if total else "  n/a"

    lines = [
        "INTAKE CHECK",
        f"{total} merchant use cases, checked against the acquirers each was raised to",
        "",
        "On arrival at ops",
        "  Two things decide whether an application can go out: the paperwork is",
        "  complete, and the acquirer takes the category. Both are per acquirer.",
        "",
        f"  can go out to every acquirer  {fully_ready:>4}  {pct(fully_ready)}",
        f"  some can go out, some cannot  {partial:>4}  {pct(partial)}",
        f"  nothing can go out yet        {fully_blocked:>4}  {pct(fully_blocked)}",
        "",
        "What the partial cases are worth",
        f"  {partial} use cases have applications that could go out today while the",
        "  rest wait for a fix. Holding all of them back would give away those days",
        "  for a field only some acquirers asked for.",
        "",
        "Gaps found, by kind",
    ]
    for level in ("missing", "format", "consistency"):
        n = level_counts.get(level, 0)
        note = {
            "missing": "field is blank",
            "format": "filled in, but not a valid identifier",
            "consistency": "filled in and valid, but contradicts another field",
        }[level]
        lines.append(f"  {level:<14}{n:>5}   {note}")

    lines += ["", "Gaps by field"]
    for fieldname, n in field_counts.most_common():
        lines.append(f"  {label_for(fieldname):<32}{n:>5}")

    lines += ["", "Where the bounces route"]
    for party, n in party_counts.most_common():
        owner = "BD, who owns the merchant relationship" if party == "bd" else "onboarding"
        lines.append(f"  {party:<14}{n:>5}   {owner}")

    lines += [
        "",
        "Category, per application",
        "  Checked against the restriction registry: our own policy, plus each",
        "  acquirer's own list. The lists do not agree with each other and two of",
        "  them are not even code lists, so this cannot be answered once for the",
        "  merchant. How hard each answer bites differs too.",
        "",
    ]
    for status in ("clear", "clear, with conditions", "needs a decision", "blocked by policy"):
        n = verdicts.get(status, 0)
        note = {
            "clear": "nothing on file against this category at this acquirer",
            "clear, with conditions": "goes out, with a licence, sign-off or rail limit stated",
            "needs a decision": "a bank's list says no; ops decides and it is recorded",
            "blocked by policy": "our own policy; no document fixes it, so none is chased",
        }[status]
        lines.append(f"  {status:<24}{n:>5}   {note}")

    lines += [
        "",
        f"  {divergent} use cases where the acquirers disagree about the same MCC",
        f"  {conditional_cases} use cases carrying at least one condition to clear before go-live",
        "",
        "Awaiting an ops decision, by acquirer",
    ]
    if acq_decision:
        for code, n in sorted(acq_decision.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {code:<18}{n:>5}")
    else:
        lines.append("  none")

    lines += ["", "Blocked by our own policy, by acquirer"]
    if acq_ineligible:
        for code, n in sorted(acq_ineligible.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {code:<18}{n:>5}")
    else:
        lines.append("  none")

    for mcc, ex, _n in divergent_examples:
        lines += [
            "",
            "=" * 68,
            f"One MCC, {len({a.status for a in ex.per_acquirer})} different answers: {mcc} for {ex.merchant_name}",
            "=" * 68,
            "",
        ]
        for a in ex.per_acquirer:
            if not a.assessment:
                continue
            entry = a.assessment.deciding_entry
            detail = entry.reason if entry else (a.assessment.house_reason or "nothing on file")
            conf = (
                f"  [confidence {entry.confidence:.2f}, {entry.source.value}]"
                if entry else ""
            )
            lines.append(f"  {a.acquirer_name:<34}{a.status:<42}{detail}{conf}")
        lines += [
            "",
            "  Same business, same code, read differently by each. A single",
            "  restricted-category flag on the merchant record would have to pick",
            "  one of these and be wrong for the rest.",
        ]

    lines += ["", "Blocked applications by acquirer"]
    for code, n in sorted(acq_blocked.items()):
        lines.append(f"  {code:<18}{n:>5}   asks for {len(ACQUIRERS[code].required_fields)} fields")

    for ex in examples:
        lines += ["", "=" * 68, f"Example: {ex.merchant_name}", "=" * 68, "", ex.summary(), ""]
        for a in ex.per_acquirer:
            if a.refused:
                mark = "ineligible on category"
            elif a.ready:
                mark = a.status
            else:
                mark = "blocked on " + ", ".join(
                    label_for(f) for f in a.blocking_fields
                )
            lines.append(f"  {a.acquirer_name:<34}{mark}")
        for b in ex.bounces:
            lines += ["", f"  Ticket to {b.party.value.upper()}:"]
            lines += ["    " + ln for ln in b.comment.splitlines()]

    report = "\n".join(lines)
    Path("data").mkdir(exist_ok=True)
    (Path("data") / "intake_report.txt").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
