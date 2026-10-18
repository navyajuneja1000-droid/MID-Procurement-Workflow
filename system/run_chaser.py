"""
Today's chase list, the drafts, and whether chasing is doing anything.

    python run_chaser.py

Three parts:

  1. What every acquirer is sitting on past the agreed response time, judged on
     the round they are holding now and ranked by what is worth chasing.
  2. One draft email per acquirer, plus an escalation note for the requests
     that have been chased twice and have not moved. Nothing is sent.
  3. A fair comparison of chased against unchased rounds, printed next to the
     naive one that points the other way.

Writes data/chase_report.txt and data/outbox_chases/.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.chaser.draft import draft_for_acquirer, escalation_note
from src.chaser.due import Owes, find_overdue, owed_by, rank
from src.chaser.effect import (
    Round,
    compare_from_day,
    compare_stratified,
    naive_compare,
)
from src.chaser.holdout import HOLDOUT_SHARE, in_holdout
from src.chaser.ladder import ChaseLog, Stage
from src.generator import DataGenerator
from src.model import EventType, Party
from src.tat import STATUS, TatEngine

AS_OF = datetime(2026, 10, 1, 18, 0)
TODAY = date(2026, 10, 1)
OUT = Path("data/outbox_chases")


def acquirer_email(code: str) -> str:
    return f"{code.lower().replace('_', '.')}.ops@example-acquirer.test"


def build_rounds(data, engine, cal) -> list[Round]:
    """Every closed acquirer status round in the dataset, with whether a chase
    would have gone out and on which day of that round.

    Reconstructed rather than recorded, because nothing in this dataset was
    ever actually chased. That is stated in the report: the comparison shows
    the method works and what it would report, not a result about real banks.
    """
    rounds: list[Round] = []
    for r in data.requests:
        tat = engine.compute(r, as_of=AS_OF)
        sla = r.acquirer.status_sla_days
        for ep in tat.episodes:
            if ep.party is not Party.ACQUIRER or ep.phase != STATUS:
                continue
            if ep.open_at_cutoff:
                continue  # still running; it has no answer day yet
            held = ep.working_days
            # A chase goes out the working day after the SLA passes, unless
            # this request is in the holdout, which is the only reason any
            # eligible round goes unchased and therefore the only thing that
            # makes the comparison below possible at all.
            eligible = held > sla
            held_out = in_holdout(r.request_id, r.merchant.priority_tier)
            chase_day = sla + 1 if (eligible and not held_out) else None
            rounds.append(
                Round(
                    request_id=r.request_id,
                    acquirer_code=r.acquirer.code,
                    days_held_total=held,
                    chased_on_day=chase_day,
                )
            )
    return rounds


def main() -> None:
    cal = load_calendar(HOLIDAYS)
    gen = DataGenerator(ACQUIRERS, cal, seed=42, as_of=AS_OF)
    data = gen.generate()
    engine = TatEngine(cal)
    log = ChaseLog.load()

    # --- who is late -----------------------------------------------------

    overdue = []
    waiting_on_us = 0
    open_with_bank = 0
    for r in data.requests:
        owes = owed_by(r)
        if owes is Owes.US:
            waiting_on_us += 1
        if owes in (Owes.ACQUIRER_STATUS, Owes.ACQUIRER_MID):
            open_with_bank += 1
        tat = engine.compute(r, as_of=AS_OF)
        item = find_overdue(r, tat, r.acquirer, AS_OF)
        if item:
            overdue.append(item)

    overdue = rank(overdue)

    lines = [
        "CHASE LIST",
        f"as of {TODAY.strftime('%d-%b-%Y')}",
        "",
        f"{open_with_bank} applications sitting with an acquirer",
        f"{len(overdue)} of those are past the agreed response time",
        f"{waiting_on_us} are waiting on us, and are not chased",
        "",
        "  That last line is the one worth keeping. A request late because a bank",
        "  asked us for a document and we have not sent it is not the bank's fault.",
        "  Chasing it spends credibility needed for the ones that genuinely are late,",
        "  and the reply is a screenshot of their own email.",
        "",
    ]

    by_tier = Counter(o.tier.value for o in overdue)
    lines += ["Overdue by priority tier"]
    for tier in ("p0", "p1", "p2", "p3", "p4"):
        if by_tier.get(tier):
            lines.append(f"  {tier.upper():<6}{by_tier[tier]:>5}")

    by_acq: dict[str, list] = defaultdict(list)
    for o in overdue:
        by_acq[o.acquirer.code].append(o)

    lines += ["", "Overdue by acquirer", ""]
    for code, items in sorted(by_acq.items(), key=lambda kv: -len(kv[1])):
        worst = max(i.days_over for i in items)
        lines.append(
            f"  {ACQUIRERS[code].name:<34}{len(items):>4} open   "
            f"worst {worst} day{'' if worst == 1 else 's'} over "
            f"(SLA {ACQUIRERS[code].status_sla_days})"
        )

    lines += [
        "",
        "Top of the queue",
        "",
        "  Ranked by days over weighted by priority tier, so a P0 one day late",
        "  outranks a P4 six days late. The weighting is stated rather than hidden,",
        "  because a ranking nobody can explain is one ops reorders by hand.",
        "",
    ]
    for o in overdue[:8]:
        lines.append(
            f"  {o.tier.value.upper():<4}{o.request.merchant.legal_name:<44}"
            f"{o.acquirer.code:<16}{o.days_held}d held / {o.sla_days} agreed   "
            f"round {o.round_index}   score {o.score:.1f}"
        )

    # --- the drafts ------------------------------------------------------

    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.iterdir():
        if stale.is_file():
            stale.unlink()

    drafted = 0
    escalations = []
    held_by_cooldown = 0
    in_holdout_now = 0

    lines += ["", "=" * 70, "DRAFTS", "=" * 70, ""]

    for code, items in sorted(by_acq.items()):
        sendable, escalate = [], []
        for o in items:
            if in_holdout(o.request.request_id, o.tier):
                in_holdout_now += 1
                continue
            if log.in_cooldown(o.request.request_id, TODAY, cal.working_days_between):
                held_by_cooldown += 1
                continue
            stage = log.next_stage(o.request.request_id, o.round_index)
            (escalate if stage is Stage.ESCALATE else sendable).append((stage, o))

        escalations.extend(o for _s, o in escalate)
        if not sendable:
            continue

        # One email per acquirer per stage: a first chase and a second chase
        # read differently, so they cannot share a message.
        for stage in (Stage.FIRST, Stage.SECOND):
            group = [o for s, o in sendable if s is stage]
            if not group:
                continue
            group = rank(group)
            note = draft_for_acquirer(group, stage, TODAY, acquirer_email(code))
            path = OUT / f"{TODAY.isoformat()}_{code}_{stage.value}.txt"
            path.write_text(
                f"To: {note.to}\nSubject: {note.subject}\n\n{note.body}\n"
            )
            for o in group:
                log.record(
                    request_id=o.request.request_id,
                    acquirer_code=code,
                    stage=stage,
                    round_index=o.round_index,
                    days_over=o.days_over,
                    on=TODAY,
                )
            drafted += 1
            lines.append(
                f"  {path.name:<44}{len(group):>3} application"
                f"{'' if len(group) == 1 else 's'}, {stage.value} chase"
            )

    if in_holdout_now:
        lines.append(
            f"  {in_holdout_now} deliberately not chased: the holdout, which is how "
            "the question below gets answered"
        )
    if held_by_cooldown:
        lines.append(
            f"  {held_by_cooldown} held back by the two-day cooldown, so nothing is "
            "chased twice in a row"
        )

    if escalations:
        path = OUT / f"{TODAY.isoformat()}_ESCALATE.txt"
        path.write_text(escalation_note(escalations) + "\n")
        lines.append(
            f"  {path.name:<44}{len(escalations):>3} chased twice already, "
            "no third email"
        )

    lines += [
        "",
        "  Nothing is sent from here. The drafts sit in data/outbox_chases/ for a",
        "  person to review, the same checkpoint as the bank files.",
    ]

    # --- does chasing work ----------------------------------------------

    rounds = build_rounds(data, engine, cal)
    naive = naive_compare(rounds)
    # Each acquirer is compared on its own chase day, the working day after
    # its SLA, because that is where the two arms become comparable.
    chase_day = {c: a.status_sla_days + 1 for c, a in ACQUIRERS.items()}
    strat = compare_stratified(rounds, chase_day)

    lines += [
        "",
        "=" * 70,
        "IS THE CHASING WORTH IT",
        "=" * 70,
        "",
        f"  {len(rounds)} completed acquirer response rounds in the dataset.",
        f"  {HOLDOUT_SHARE:.0%} of eligible rounds are held out and never chased,",
        "  which is the only reason this question can be answered at all.",
        "",
        "First, the comparison that looks obvious and is wrong",
        "",
    ]
    if naive.mean_chased is not None and naive.mean_not_chased is not None:
        lines += [
            f"  chased rounds        {naive.n_chased:>5}   mean "
            f"{naive.mean_chased:.1f} working days held",
            f"  not chased           {naive.n_not_chased:>5}   mean "
            f"{naive.mean_not_chased:.1f} working days held",
        ]
    lines += [
        "",
        "  Read straight, this says chasing makes banks slower. It does not. Every",
        "  chased round had already run past the SLA; the unchased group is mostly",
        "  banks that answered on day two. It measures which rounds were eligible",
        "  to be chased, not what the chasing did to them.",
        "",
        "Second, why comparing across acquirers does not fix it",
        "",
        "  The chase goes out the day after each bank's own SLA, so the chased",
        "  group on any given day is the banks with short SLAs and the unchased",
        "  group is the banks with long ones. That difference is bank mix.",
        "",
        "Third, the comparison that is actually fair",
        "",
        "  Inside each acquirer, on that bank's own chase day, chased against held",
        "  out. Same bank, same clock, both already past the SLA, and the only",
        "  difference between the two arms is whether an email went.",
        "",
    ]
    for code, c in sorted(strat.per_acquirer.items()):
        if c.n_chased == 0 and c.n_not_chased == 0:
            continue
        detail = (
            f"day {c.day}   chased {c.n_chased:>3} (mean {c.mean_chased:.1f}d more)   "
            f"held out {c.n_not_chased:>3} (mean {c.mean_not_chased:.1f}d more)"
            if c.mean_chased is not None and c.mean_not_chased is not None
            else f"day {c.day}   chased {c.n_chased:>3}   held out {c.n_not_chased:>3}"
        )
        flag = "" if c.usable else "   too few to use"
        lines.append(f"  {ACQUIRERS[code].name:<34}{detail}{flag}")

    held_out_now = sum(c.n_not_chased for c in strat.per_acquirer.values())
    chased_now = sum(c.n_chased for c in strat.per_acquirer.values())
    needed = max(0, 20 - max((c.n_not_chased for c in strat.per_acquirer.values()), default=0))

    lines += [
        "",
        f"  {strat.verdict()}",
        "",
        "What it would take to get an answer",
        "",
        f"  {chased_now} chased rounds against {held_out_now} held out, spread across six",
        "  acquirers. The chased side is fine; the holdout is the constraint, because",
        f"  it only ever collects {HOLDOUT_SHARE:.0%} of what runs late.",
        "",
        f"  The largest acquirer stratum is still {needed} held-out rounds short of the",
        "  twenty-a-side bar, and that bar is the floor for noticing anything at all,",
        "  not for measuring a day. Two ways to shorten it, and they trade off against",
        "  each other: raise the holdout share, which answers the question sooner and",
        "  makes more merchants wait, or pool strata and accept that the banks differ.",
        "",
        "  Saying this is the point. A chaser that reported a number here would be",
        "  reporting noise, and a team would act on it.",
        "",
        "  And the caveat that matters most: nothing in this dataset was ever",
        "  actually chased. The chases are reconstructed from the rule, so what is",
        "  shown here is that the method works and what it would report. The number",
        "  becomes real once live chases are logged against live rounds, which is",
        "  what the chase log and the holdout are for.",
    ]

    report = "\n".join(l for l in lines if l is not None)
    Path("data").mkdir(exist_ok=True)
    (Path("data") / "chase_report.txt").write_text(report + "\n")
    log.save()
    print(report)


if __name__ == "__main__":
    main()
