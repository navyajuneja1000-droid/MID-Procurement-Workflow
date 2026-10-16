"""
Add, retire and inspect restrictions without touching the code.

    python manage_restrictions.py list --acquirer ALDRIDGE
    python manage_restrictions.py check 7428 --instruments CC,DC
    python manage_restrictions.py add ALDRIDGE mcc 7428 --source rm_call \\
        --reason "RM says they have stopped taking nutraceuticals" --by n.juneja
    python manage_restrictions.py retire ALDRIDGE-MCC-7428 \\
        --because "their circular of 12 Oct lists it as acceptable again" --by n.juneja
    python manage_restrictions.py candidates
    python manage_restrictions.py promote mcc 7726 --because "repeated refusals" --by risk
    python manage_restrictions.py overrides

The whole point of this file is that nobody needs a release to record what a
bank said this morning. A restriction list maintained by engineers is a list
that lags the news by a sprint, and ops works around a list that lags.

Defaults are deliberate: anything added lands as a warning, not a block. A
single entry typed in haste should raise a flag on tomorrow's batch, not
silently remove an acquirer from every batch until somebody audits the volumes.
"""

from __future__ import annotations

import argparse
from datetime import date

from config.acquirers import ACQUIRERS
from src.catalogue.assess import assess, house_candidates
from src.catalogue.mcc import CATALOGUE, Instrument
from src.catalogue.overrides import OverrideLog
from src.catalogue.restrictions import (
    HOUSE,
    Enforcement,
    Registry,
    Scope,
    Source,
)

INSTRUMENT_BY_CODE = {
    "CC": Instrument.CREDIT_CARD,
    "DC": Instrument.DEBIT_CARD,
    "EMI": Instrument.EMI,
    "UPI": Instrument.CREDIT_LINE_UPI,
    "OD": Instrument.OVERDRAFT,
}


def parse_instruments(value: str | None) -> tuple[Instrument, ...]:
    if not value:
        return ()
    out = []
    for token in value.split(","):
        token = token.strip().upper()
        if token not in INSTRUMENT_BY_CODE:
            raise SystemExit(
                f"unknown instrument {token!r}; use one of "
                f"{', '.join(INSTRUMENT_BY_CODE)}"
            )
        out.append(INSTRUMENT_BY_CODE[token])
    return tuple(out)


def cmd_list(args) -> None:
    registry = Registry.load()
    entries = registry.live_entries(args.acquirer)
    if args.enforcement:
        entries = [e for e in entries if e.enforcement.value == args.enforcement]
    log = OverrideLog.load()

    print(f"{len(entries)} live entries" + (f" for {args.acquirer}" if args.acquirer else ""))
    for e in sorted(entries, key=lambda x: (x.acquirer_code, x.value)):
        name = CATALOGUE[e.value].sub_category if e.scope is Scope.MCC else e.value
        print(f"\n  {e.entry_id}")
        print(f"    {name}")
        print(f"    {e.enforcement.value}, {e.source.value}, added {e.added_on} by {e.added_by}")
        print(f"    {e.reason}")
        history = log.history_for(e.entry_id)
        if history != "never overridden":
            print(f"    {history}  ->  {log.verdict_on(e.entry_id)}")


def cmd_check(args) -> None:
    """What every acquirer would say about one category right now."""
    registry = Registry.load()
    instruments = parse_instruments(args.instruments)
    entry = CATALOGUE.get(args.mcc)
    if entry is None:
        raise SystemExit(f"{args.mcc} is not in the catalogue")

    print(f"{args.mcc}  {entry.sub_category}  ({entry.category}, risk {entry.risk.value})")
    if instruments:
        print("asked for: " + ",".join(i.value for i in instruments))
    print()
    for code in sorted(ACQUIRERS):
        a = assess(registry, code, args.mcc, instruments)
        print(f"  {ACQUIRERS[code].name:<34}{a.status}")
        if a.enforcement is not None:
            print(f"      {a.explain()}")
        if a.blocked_instruments:
            print(
                "      not run on: "
                + ",".join(i.value for i in a.blocked_instruments)
            )


def cmd_add(args) -> None:
    registry = Registry.load()
    enforcement = Enforcement(args.enforcement) if args.enforcement else None
    try:
        entry = registry.add(
            acquirer_code=args.acquirer,
            scope=Scope(args.scope),
            value=args.value,
            reason=args.reason,
            source=Source(args.source),
            added_by=args.by,
            enforcement=enforcement,
            instruments=parse_instruments(args.instruments),
            condition=args.condition,
            on=date.today(),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    registry.save()
    print(f"added {entry.entry_id}")
    print(f"  {entry.describe()}")
    print(f"  confidence {entry.confidence:.2f} (from its source)")
    if entry.enforcement is Enforcement.WARN:
        print(
            "  This will flag on the next batch and wait for a decision. It does "
            "not stop anything going out."
        )


def cmd_retire(args) -> None:
    registry = Registry.load()
    try:
        registry.retire(args.entry_id, actor=args.by, because=args.because)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    registry.save()
    print(f"retired {args.entry_id}; the row stays, with the date and the reason")


def cmd_promote(args) -> None:
    registry = Registry.load()
    entry = registry.promote_to_house(
        scope=Scope(args.scope), value=args.value, actor=args.by, because=args.because
    )
    registry.save()
    print(f"promoted to house policy: {entry.entry_id}")
    print("  This one blocks. No acquirer will be offered it and no document is chased.")


def cmd_candidates(args) -> None:
    registry = Registry.load()
    candidates = house_candidates(registry, min_refusing=args.min)
    coverage = len(registry.acquirers_with_policy())
    print(f"Candidates for house policy, from {coverage} acquirer lists\n")
    if not candidates:
        print("  none at this threshold")
        return
    for c in candidates:
        name = CATALOGUE[c.scope_value].sub_category
        verdict = "promote" if c.confident else "watch"
        tail = "  (already house policy)" if c.already_house else ""
        print(
            f"  {c.scope_value}  {name:<30}refused by {len(c.refusing)}/{c.coverage}"
            f"  {verdict}{tail}"
        )
        print(f"      {', '.join(c.refusing)}")
    print(
        "\n  Nothing here is applied. Unanimity across two lists means little; "
        "across\n  four it is a pattern. Promotion is a decision, and it is recorded "
        "as one."
    )


def cmd_overrides(args) -> None:
    log = OverrideLog.load()
    registry = Registry.load()
    if not log.overrides:
        print("No overrides recorded yet. Run review_restrictions.py first.")
        return

    print(f"{len(log.overrides)} overrides recorded\n")
    for outcome, n in sorted(log.outcome_counts().items(), key=lambda kv: -kv[1]):
        print(f"  {outcome:<12}{n:>5}")
    rate = log.rejection_rate
    if rate is not None:
        print(f"\n  {rate:.0%} of answered overrides were refused on the category.")

    print("\nBy entry")
    for entry_id in sorted({o.entry_id for o in log.overrides}):
        verdict = log.verdict_on(entry_id)
        try:
            reason = registry.get(entry_id).reason
        except KeyError:
            reason = "(entry no longer in the registry)"
        print(f"\n  {entry_id}")
        print(f"    {reason}")
        print(f"    {log.history_for(entry_id)}  ->  {verdict}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="show live entries")
    p.add_argument("--acquirer", help=f"one of {', '.join(sorted(ACQUIRERS))}, or {HOUSE}")
    p.add_argument("--enforcement", choices=[e.value for e in Enforcement])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="what every acquirer says about one MCC")
    p.add_argument("mcc")
    p.add_argument("--instruments", help="e.g. CC,DC,EMI")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("add", help="record a restriction")
    p.add_argument("acquirer", help=f"an acquirer code, or {HOUSE} for our own policy")
    p.add_argument("scope", choices=[s.value for s in Scope])
    p.add_argument("value", help="an MCC code, a category, or a super category")
    p.add_argument("--reason", required=True)
    p.add_argument("--source", required=True, choices=[s.value for s in Source])
    p.add_argument("--by", required=True, help="who is recording it")
    p.add_argument(
        "--enforcement",
        choices=[e.value for e in Enforcement],
        help="defaults to warn, or block for house policy",
    )
    p.add_argument("--instruments", help="limit it to these rails, e.g. CC,EMI")
    p.add_argument("--condition", default="", help="what would make it acceptable")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("retire", help="stop applying an entry, keeping the row")
    p.add_argument("entry_id")
    p.add_argument("--because", required=True)
    p.add_argument("--by", required=True)
    p.set_defaults(func=cmd_retire)

    p = sub.add_parser("promote", help="make a category our own policy, which blocks")
    p.add_argument("scope", choices=[s.value for s in Scope])
    p.add_argument("value")
    p.add_argument("--because", required=True)
    p.add_argument("--by", required=True)
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("candidates", help="what several acquirers refuse")
    p.add_argument("--min", type=int, default=2, help="minimum acquirers refusing")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("overrides", help="what happened to the overrides")
    p.set_defaults(func=cmd_overrides)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)
