"""
Export everything the dashboard needs as one compact JSON file.

    python export_dashboard.py

Writes dashboard_data.json. Per-request rows plus their episodes, so filtering
and the drill-down timeline both run in the page with no server.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.generator import DataGenerator
from src.model import EventType, Party
from src.tat import MID_DELIVERY, SOLO, STATUS, WITH_ACQUIRER, TatEngine

AS_OF = datetime(2026, 10, 1, 18, 0)
SEED = 42

PARTY_CODE = {Party.OPS: "ops", Party.BD: "bd", Party.ONBOARDING: "onb", Party.ACQUIRER: "acq"}
PHASE_CODE = {SOLO: "solo", WITH_ACQUIRER: "overlap", STATUS: "status", MID_DELIVERY: "mid", "": ""}


def inbox_summary() -> dict:
    """Run the parser over the inbox so the dashboard can show what came in
    automatically and what still needs a person.

    The review queue is an ops work item, not a diagnostic: those rows are
    updates nobody has applied yet, so the clocks behind them are still running.
    """
    from csv import DictReader
    from pathlib import Path

    from src.parsing.pipeline import process_inbox

    emails = Path("data/emails")
    if not emails.exists():
        return {}

    closed = {"procured", "rejected_by_bank", "deprioritised"}
    requests = [
        {
            "request_id": r["request_id"],
            "name": r["legal_name"],
            "mcc": r["mcc"],
            "acquirer": r["acquirer"],
            "open": True,
        }
        for r in DictReader(Path("data/requests.csv").open())
        if r["status"] not in closed or True
    ]

    result = process_inbox(emails, requests)
    return {
        "emails": result.emails_read,
        "rows": len(result.updates),
        "applied": len(result.applied),
        "review": [
            {
                "name": u.row_name,
                "mcc": u.row_mcc,
                "acq": u.acquirer,
                "conf": round(u.confidence, 2),
                "why": u.review_reason,
                "file": u.email_file,
            }
            for u in result.review
        ][:20],
    }


def main() -> None:
    calendar = load_calendar(HOLIDAYS)
    engine = TatEngine(calendar)
    gen = DataGenerator(ACQUIRERS, calendar, seed=SEED, as_of=AS_OF)
    data = gen.generate()

    rows = []
    for r in data.requests:
        t = engine.compute(r, as_of=AS_OF)
        breach = t.sla_breach(r.acquirer.status_sla_days, r.acquirer.mid_sla_days)

        episodes = [
            {
                "p": PARTY_CODE[e.party],
                "ph": PHASE_CODE.get(e.phase, e.phase),
                "s": e.start.strftime("%Y-%m-%d"),
                "e": e.end.strftime("%Y-%m-%d") if e.end else "",
                "d": e.working_days,
                "o": 1 if e.open_at_cutoff else 0,
            }
            for e in t.episodes
        ]

        events = [
            {
                "t": e.event_type.value,
                "at": e.at.strftime("%Y-%m-%d %H:%M"),
                "r": (e.bounce_reason.value if e.bounce_reason else "")
                or (e.rejection_reason.value if e.rejection_reason else ""),
            }
            for e in r.events
        ]

        first = r.events[0].at if r.events else AS_OF

        rows.append(
            {
                "id": r.request_id,
                "name": r.merchant.legal_name,
                "mcc": r.mcc,
                "acq": r.acquirer.code,
                "stream": r.stream.value,
                "seg": r.merchant.segment.value,
                "tier": r.merchant.priority_tier.value,
                "status": r.status.value,
                "opsEp": t.ops_episode_count,
                "opsByEp": t.ops_days_by_episode,
                "opsSolo": t.ops_solo_days,
                "opsOverlap": t.ops_overlap_days,
                "bd": t.bd_days,
                "onb": t.onboarding_days,
                "acqStatus": t.acquirer_status_days,
                "acqWorst": t.acquirer_worst_status_round,
                "acqRounds": t.acquirer_status_rounds,
                "acqMid": t.acquirer_mid_days,
                "elapsed": t.consolidated_days,
                "sla": r.acquirer.status_sla_days,
                "breach": 1 if breach["status"] else 0,
                "depr": 1 if t.deprioritised else 0,
                "month": first.strftime("%Y-%m"),
                "open": 1 if any(e.open_at_cutoff for e in t.episodes) else 0,
                "ev": events,
                "eps": episodes,
            }
        )

    payload = {
        "asOf": AS_OF.strftime("%d %b %Y"),
        "acquirers": {
            code: {"name": a.name, "sla": a.status_sla_days, "midSla": a.mid_sla_days}
            for code, a in ACQUIRERS.items()
        },
        "requests": rows,
        "inbox": inbox_summary(),
    }

    out = Path("dashboard_data.json")
    out.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"{out}  {out.stat().st_size / 1024:.0f} KB  {len(rows)} requests")


if __name__ == "__main__":
    main()
