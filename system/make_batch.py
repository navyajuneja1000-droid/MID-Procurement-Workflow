"""
Build today's batch files, one per acquirer, and a draft email for each.

    python make_batch.py

What this replaces: a person opening each bank's template, copying merchant
details across from the tracker, filling what they can, and sending it. That is
where most of an ops day goes, and it is where the errors are introduced that
come back as a refused file several days later.

The sequence matters and is the whole design:

  1. Decide what is even eligible to be raised today. A request blocked on a
     document or refused on category never reaches a file. Putting it in and
     letting the bank refuse it burns a response round.
  2. Fill each acquirer's own template from one resolved row per request, so
     tabs that repeat the merchant cannot disagree.
  3. Pre-flight the result. A file that will be refused on upload is caught
     here, in a second, rather than by the bank in a day.
  4. Draft the covering email and stop. A person sends it.

Output lands in data/outbox/.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from config.acquirers import ACQUIRERS, HOLIDAYS
from config.templates import TEMPLATES
from src.calendar_rules import load_calendar
from src.catalogue.eligibility import check
from src.catalogue.mcc import CATALOGUE
from src.catalogue.restrictions import Registry
from src.filegen.cover import draft
from src.filegen.fill import BatchMeta, fill
from src.filegen.preflight import blocking_rows, preflight
from src.filegen.write import write
from src.generator import DataGenerator
from src.intake.readiness import check_intake
from src.model import ProcurementRequest, RequestStatus

AS_OF = datetime(2026, 10, 1, 18, 0)
LOGIN_DATE = date(2026, 10, 1)
OUT = Path("data/outbox")


def _eligible_to_raise(
    requests: list[ProcurementRequest],
    registry: Registry,
) -> tuple[list[ProcurementRequest], dict[str, list[str]]]:
    """Keep only what can actually go out, and say why the rest cannot.

    Readiness is per acquirer and per use case, so this cannot be a merchant
    level filter: the same merchant can be in today's Casterly file and held
    back from Aldridge's.
    """
    keep: list[ProcurementRequest] = []
    held: dict[str, list[str]] = defaultdict(list)

    # Group by use case so the intake check runs once per merchant and MCC,
    # against every acquirer it is going to, which is how it reports a single
    # consolidated bounce instead of one per acquirer.
    by_case: dict[tuple[str, str], list[ProcurementRequest]] = defaultdict(list)
    for r in requests:
        by_case[(r.merchant.merchant_id, r.mcc)].append(r)

    for (_merchant_id, mcc), group in by_case.items():
        if mcc not in CATALOGUE:
            held["mcc not in the catalogue"].extend(r.request_id for r in group)
            continue
        result = check_intake(
            group[0].merchant,
            [r.acquirer for r in group],
            mcc=mcc,
            instruments=group[0].instruments,
            registry=registry,
        )
        ready = set(result.ready_now)
        refused = set(result.ineligible)
        awaiting = set(result.awaiting_decision)
        for r in group:
            if r.acquirer.code in ready:
                keep.append(r)
            elif r.acquirer.code in refused:
                held["blocked by our own policy"].append(r.request_id)
            elif r.acquirer.code in awaiting:
                # Not refused. A restriction is on file and somebody has to
                # decide, so it stays out of the automatic batch and goes out
                # the moment the decision is recorded.
                held["waiting on an ops decision about a restriction"].append(r.request_id)
            else:
                held["blocked on documents"].append(r.request_id)
    return keep, dict(held)


def main() -> None:
    cal = load_calendar(HOLIDAYS)
    gen = DataGenerator(ACQUIRERS, cal, seed=42, as_of=AS_OF)
    data = gen.generate()

    # Today's work: requests still sitting in the ops queue, unraised.
    waiting = [
        r
        for r in data.requests
        if r.status is RequestStatus.TO_BE_RAISED and not r.is_deprioritised
    ]
    registry = Registry.load()
    raiseable, held = _eligible_to_raise(waiting, registry)

    by_acquirer: dict[str, list[ProcurementRequest]] = defaultdict(list)
    for r in raiseable:
        by_acquirer[r.acquirer.code].append(r)

    date_stamp = LOGIN_DATE.strftime("%Y-%m-%d")
    lines = [
        "BATCH BUILD",
        f"login date {LOGIN_DATE.strftime('%d-%b-%Y')}",
        "",
        f"{len(waiting)} requests waiting in the ops queue",
        f"{len(raiseable)} can go out today",
    ]
    for reason, ids in sorted(held.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"{len(ids):>5} held: {reason}")
    lines.append("")

    sent = 0
    built = 0
    for code in sorted(by_acquirer):
        requests = by_acquirer[code]
        template = TEMPLATES[code]
        acquirer = ACQUIRERS[code]

        # Respect the row cap by splitting rather than truncating. A file one
        # row over the cap is refused in full, and the quiet version of this
        # bug drops the overflow and nobody notices which merchants went.
        cap = template.max_rows or len(requests)
        chunks = [requests[i : i + cap] for i in range(0, len(requests), cap)]

        for part, chunk in enumerate(chunks, start=1):
            lot_no = f"{date_stamp.replace('-', '')}-{code[:4]}-{part}"
            meta = BatchMeta(lot_no=lot_no, login_date=LOGIN_DATE)
            eligibility = {
                r.request_id: check(code, r.mcc, r.instruments) for r in chunk
            }

            filled = fill(code, chunk, meta, eligibility, deals=data.deals)

            # Drop the rows that would stop the file and send the rest. Same
            # reasoning as partial raising at intake: four merchants waiting on
            # a settlement cycle should not hold the other fifty until
            # tomorrow's slot.
            dropped = blocking_rows(filled)
            if dropped:
                chunk = [r for r in chunk if r.request_id not in dropped]
                for reason in set(dropped.values()):
                    held.setdefault(reason, []).extend(
                        rid for rid, why in dropped.items() if why == reason
                    )
                if not chunk:
                    lines += [
                        f"{acquirer.name}",
                        f"  nothing to send: all {len(dropped)} applications held",
                        "",
                    ]
                    continue
                filled = fill(code, chunk, meta, eligibility, deals=data.deals)

            checked = preflight(filled)
            path = write(filled, OUT, lot_no, date_stamp)

            note = draft(
                filled,
                checked,
                attachment=path.name,
                lot_no=lot_no,
                on=LOGIN_DATE,
                eligibility=eligibility,
                acquirer_name=acquirer.name,
                acquirer_email=f"{code.lower().replace('_', '.')}.ops@example-acquirer.test",
            )
            (OUT / f"{path.stem}_draft.txt").write_text(
                f"To: {note.to}\nSubject: {note.subject}\n"
                f"Attachment: {note.attachment}\n\n{note.body}\n"
            )

            owed = filled.owed_by_team
            lines += [
                f"{acquirer.name}",
                f"  file              {path.name}",
                f"  applications      {len(chunk)}"
                + (f" (part {part} of {len(chunks)}, cap {template.max_rows})"
                   if len(chunks) > 1 else ""),
                f"  sheets            {', '.join(s.name for s in template.sheets)}",
                f"  pre-flight        {checked.summary()}",
            ]
            if dropped:
                lines.append(
                    f"  held back         {len(dropped)}, sent separately once supplied"
                )
            for f_ in checked.blockers[:4]:
                lines.append(f"    blocker   {f_.message}")
            for f_ in checked.warnings[:3]:
                lines.append(f"    look at   {f_.message}")
            if owed:
                lines.append(
                    "  waiting on        "
                    + ", ".join(f"{team} ({n})" for team, n in sorted(owed.items()))
                )
            lines.append(
                f"  draft email       {path.stem}_draft.txt  (a person sends it)"
            )
            lines.append("")
            built += 1
            if checked.can_send:
                sent += 1

    lines += [
        f"{sent} of {built} files clear to send.",
        "",
        "Nothing is sent from here. The files and drafts sit in data/outbox/ for",
        "a person to review, which is the one checkpoint at the external boundary.",
    ]

    report = "\n".join(lines)
    OUT.mkdir(parents=True, exist_ok=True)
    (Path("data") / "batch_report.txt").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
