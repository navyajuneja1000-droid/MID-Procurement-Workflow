"""
A worked example: one merchant, one MCC, two acquirers, a bounce, a bank
document request, and a MID at the end.

Run it with:  python example.py

The point of this file is to let the numbers be checked by hand. Every figure
printed is derived from the events above it.
"""

from datetime import datetime

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.model import (
    BounceReason,
    Event,
    EventType,
    Merchant,
    PriorityTier,
    ProcurementRequest,
    Segment,
)
from src.tat import TatEngine

AS_OF = datetime(2026, 10, 1, 18, 0)

merchant = Merchant(
    merchant_id="M-4471",
    legal_name="Harrowgate Trading Private Limited",
    segment=Segment.PREMIUM_SME,
    priority_tier=PriorityTier.P2,
    website="https://harrowgate.test",
    fields={
        "entity_legal_name": "Harrowgate Trading Private Limited",
        "company_pan": "AABCH1234K",
        "registered_address": "14 Mill Road, Bengaluru 560001",
        "website": "https://harrowgate.test",
        "mcc": "5399",
        "company_gst": "29AABCH1234K1Z5",
    },
)


def aldridge_request() -> ProcurementRequest:
    """The messy one: bounced for a missing signatory PAN, then the bank asks
    for a document, then approved."""
    r = ProcurementRequest(
        request_id="REQ-0001-ALDRIDGE",
        merchant=merchant,
        mcc="5399",
        acquirer=ACQUIRERS["ALDRIDGE"],
    )
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 7, 10, 15)))  # Mon
    r.add(
        Event(
            EventType.BOUNCED_TO_BD,
            datetime(2026, 9, 8, 16, 40),  # Tue
            bounce_reason=BounceReason.MISSING_SIGNATORY_PAN,
        )
    )
    r.add(Event(EventType.RETURNED_TO_OPS, datetime(2026, 9, 14, 11, 0)))  # Mon
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 15, 9, 30)))  # Tue
    r.add(Event(EventType.ACQUIRER_REQUESTED_DOCS, datetime(2026, 9, 18, 15, 0)))
    r.add(Event(EventType.RESPONDED_TO_ACQUIRER, datetime(2026, 9, 23, 10, 0)))
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 28, 12, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 30, 11, 0)))
    return r


def casterly_request() -> ProcurementRequest:
    """The clean one: raised the same day, approved, MID in hand. Same
    merchant, same MCC, different acquirer, entirely separate clock."""
    r = ProcurementRequest(
        request_id="REQ-0001-CASTERLY",
        merchant=merchant,
        mcc="5399",
        acquirer=ACQUIRERS["CASTERLY"],
    )
    r.add(Event(EventType.ASSIGNED_TO_OPS, datetime(2026, 9, 7, 10, 15)))
    r.add(Event(EventType.RAISED_TO_ACQUIRER, datetime(2026, 9, 7, 16, 20)))  # after 2pm
    r.add(Event(EventType.ACQUIRER_APPROVED, datetime(2026, 9, 11, 14, 0)))
    r.add(Event(EventType.MID_RECEIVED, datetime(2026, 9, 15, 10, 0)))
    return r


def report(engine: TatEngine, r: ProcurementRequest) -> None:
    tat = engine.compute(r, as_of=AS_OF)
    acq = r.acquirer
    breach = tat.sla_breach(acq.status_sla_days, acq.mid_sla_days)

    print(f"\n{'=' * 68}")
    print(f"{r.request_id}   {r.merchant.legal_name}")
    print(f"MCC {r.mcc}  |  {acq.name}  |  status {r.status.value}")
    print("=" * 68)

    print("\nTimeline")
    for e in r.events:
        extra = e.bounce_reason.value if e.bounce_reason else ""
        print(f"  {e.at:%d %b %H:%M}   {e.event_type.value:<32} {extra}")

    print("\nWho held it")
    for ep in tat.episodes:
        phase = f" ({ep.phase})" if ep.phase else ""
        open_mark = "  [still open]" if ep.open_at_cutoff else ""
        print(
            f"  {ep.party.value:<11}{phase:<17} "
            f"{ep.start:%d %b} to {ep.end:%d %b}   {ep.working_days} wd{open_mark}"
        )

    print("\nOps")
    print(f"  episodes              {tat.ops_episode_count}  {tat.ops_days_by_episode}")
    print(f"  solo days             {tat.ops_solo_days}   <- efficiency figure")
    print(f"  alongside acquirer    {tat.ops_overlap_days}")
    print(f"  total                 {tat.ops_total_days}")

    print("\nOther parties")
    print(f"  BD                    {tat.bd_days}")
    print(f"  onboarding            {tat.onboarding_days}")

    print("\nAcquirer")
    print(
        f"  status response       {tat.acquirer_status_days} "
        f"(SLA {acq.status_sla_days}){'  BREACH' if breach['status'] else ''}"
    )
    print(
        f"  approval to MID       {tat.acquirer_mid_days} "
        f"(SLA {acq.mid_sla_days}){'  BREACH' if breach['mid_delivery'] else ''}"
    )
    print(f"  total                 {tat.acquirer_total_days}")

    summed = (
        tat.ops_total_days
        + tat.bd_days
        + tat.onboarding_days
        + tat.acquirer_total_days
    )
    print("\nConsolidated")
    print(f"  elapsed working days  {tat.consolidated_days}")
    print(f"  sum of party days     {summed}  <- larger, because clocks overlap")


def main() -> None:
    engine = TatEngine(load_calendar(HOLIDAYS))
    requests = [aldridge_request(), casterly_request()]
    for r in requests:
        report(engine, r)

    print(f"\n{'=' * 68}")
    print("Merchant roll-up")
    print("=" * 68)
    tats = [engine.compute(r, as_of=AS_OF) for r in requests]
    print(f"  requests                 {len(tats)}")
    print(f"  avg ops solo days        {sum(t.ops_solo_days for t in tats) / len(tats):.1f}")
    print(
        f"  avg ops days per episode "
        f"{sum(sum(t.ops_days_by_episode) for t in tats) / sum(t.ops_episode_count for t in tats):.1f}"
    )
    print(f"  avg acquirer days        {sum(t.acquirer_total_days for t in tats) / len(tats):.1f}")
    print(
        "\n  Note: there is no single TAT for this merchant. The two acquirers"
        "\n  are reported separately, as they are measured separately."
    )


if __name__ == "__main__":
    main()
