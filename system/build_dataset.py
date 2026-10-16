"""
Build the synthetic dataset and write it to data/.

    python build_dataset.py

Outputs:
    data/merchants.csv        one row per merchant, with the field gaps intact
    data/requests.csv         one row per merchant + MCC + acquirer
    data/events.csv           the event log every TAT figure is derived from
    data/tat.csv              computed TAT per request, for the dashboard
    data/emails/*.txt         bank replies as they would land in the inbox
    data/emails/*.csv         attachments some acquirers send instead
    data/summary.txt          what was generated, for a sanity check
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config.acquirers import ACQUIRERS, HOLIDAYS
from src.calendar_rules import load_calendar
from src.emails import build_mid_delivery, build_status_reply
from src.generator import DataGenerator
from src.model import EventType, ProcurementRequest
from src.tat import TatEngine

AS_OF = datetime(2026, 10, 1, 18, 0)
SEED = 42

OUT = Path("data")
EMAILS = OUT / "emails"

MERCHANT_FIELDS = [
    "entity_legal_name",
    "company_pan",
    "company_gst",
    "registered_address",
    "registration_number",
    "signatory_name",
    "signatory_pan",
    "signatory_address",
    "website",
    "state",
]


def write_merchants(data) -> None:
    with (OUT / "merchants.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["merchant_id", "legal_name", "segment", "priority_tier"] + MERCHANT_FIELDS)
        for m in data.merchants:
            w.writerow(
                [m.merchant_id, m.legal_name, m.segment.value, m.priority_tier.value]
                + [m.fields.get(k, "") for k in MERCHANT_FIELDS]
            )


def write_requests(data) -> None:
    with (OUT / "requests.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "request_id",
                "merchant_id",
                "legal_name",
                "mcc",
                "acquirer",
                "stream",
                "segment",
                "priority_tier",
                "status",
                "scenario",
            ]
        )
        for r in data.requests:
            w.writerow(
                [
                    r.request_id,
                    r.merchant.merchant_id,
                    r.merchant.legal_name,
                    r.mcc,
                    r.acquirer.code,
                    r.stream.value,
                    r.merchant.segment.value,
                    r.merchant.priority_tier.value,
                    r.status.value,
                    getattr(r, "scenario", ""),
                ]
            )


def write_events(data) -> None:
    with (OUT / "events.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["request_id", "at", "event_type", "bounce_reason", "rejection_reason"])
        for r in data.requests:
            for e in r.events:
                w.writerow(
                    [
                        r.request_id,
                        e.at.isoformat(timespec="minutes"),
                        e.event_type.value,
                        e.bounce_reason.value if e.bounce_reason else "",
                        e.rejection_reason.value if e.rejection_reason else "",
                    ]
                )


def write_tat(data, engine: TatEngine) -> list:
    rows = []
    with (OUT / "tat.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "request_id",
                "legal_name",
                "mcc",
                "acquirer",
                "stream",
                "segment",
                "priority_tier",
                "status",
                "ops_episodes",
                "ops_days_by_episode",
                "ops_solo_days",
                "ops_overlap_days",
                "ops_total_days",
                "bd_days",
                "onboarding_days",
                "acquirer_status_days",
                "acquirer_status_rounds",
                "acquirer_worst_round",
                "acquirer_mid_days",
                "acquirer_total_days",
                "consolidated_days",
                "status_sla",
                "status_sla_breach",
                "deprioritised",
            ]
        )
        for r in data.requests:
            t = engine.compute(r, as_of=AS_OF)
            breach = t.sla_breach(r.acquirer.status_sla_days, r.acquirer.mid_sla_days)
            row = [
                r.request_id,
                r.merchant.legal_name,
                r.mcc,
                r.acquirer.code,
                r.stream.value,
                r.merchant.segment.value,
                r.merchant.priority_tier.value,
                r.status.value,
                t.ops_episode_count,
                "|".join(str(d) for d in t.ops_days_by_episode),
                t.ops_solo_days,
                t.ops_overlap_days,
                t.ops_total_days,
                t.bd_days,
                t.onboarding_days,
                t.acquirer_status_days,
                t.acquirer_status_rounds,
                t.acquirer_worst_status_round,
                t.acquirer_mid_days,
                t.acquirer_total_days,
                t.consolidated_days,
                r.acquirer.status_sla_days,
                int(breach["status"]),
                int(t.deprioritised),
            ]
            w.writerow(row)
            rows.append((r, t))
    return rows


def write_emails(data, rng: random.Random) -> int:
    """Group bank responses into the emails they would have arrived in.

    Replies come in dribs and drabs, so responses are bucketed by acquirer and
    date rather than by the batch that was sent. That is what makes a reply
    cover only part of a batch.
    """
    status_buckets: dict[tuple[str, str], list] = defaultdict(list)
    mid_buckets: dict[tuple[str, str], list] = defaultdict(list)

    response_types = {
        EventType.ACQUIRER_APPROVED,
        EventType.ACQUIRER_REJECTED,
        EventType.ACQUIRER_REQUESTED_DOCS,
        EventType.ACQUIRER_REQUESTED_CLARIFICATION,
    }

    for r in data.requests:
        for e in r.events:
            key = (r.acquirer.code, e.at.date().isoformat())
            if e.event_type in response_types:
                status_buckets[key].append((r, e.event_type, e.rejection_reason))
            elif e.event_type is EventType.MID_RECEIVED:
                mid_buckets[key].append(r)

    # Clear the inbox before refilling it. A rebuild with different parameters
    # otherwise leaves the previous run's emails sitting there, and the parser
    # scores itself against an inbox that is part current dataset and part
    # fiction: the stale replies name merchants and MCCs no open request
    # carries, so they go to review and the review rate looks like a parser
    # regression. This cost an afternoon once, and the fix is three lines.
    if EMAILS.exists():
        for stale in EMAILS.iterdir():
            if stale.is_file():
                stale.unlink()
    EMAILS.mkdir(parents=True, exist_ok=True)
    written = 0

    # Ground truth, written outside the inbox. The parser never reads this;
    # only the scorer does. Keeping it out of data/emails/ is what makes the
    # accuracy figure mean anything: the parser sees exactly what a real
    # inbox would hand it, with no request ids hidden in the files.
    truth: list[tuple[str, str, str, str]] = []

    for (code, day), batch in sorted(status_buckets.items()):
        # Cap an email at a believable size; a long day splits into two mails.
        for part, chunk in enumerate(_chunks(batch, 18), start=1):
            when = datetime.fromisoformat(day + "T" + f"{rng.randint(10, 18):02d}:{rng.choice(['05','20','35','50'])}")
            mail = build_status_reply(rng, code, chunk, when)
            stem = f"{day}_{code}_status_{part}"
            _write_mail(mail, stem)
            for request, event_type, reason in chunk:
                truth.append(
                    (
                        f"{stem}.txt",
                        request.request_id,
                        event_type.value,
                        reason.value if reason else "",
                    )
                )
            written += 1

    for (code, day), reqs in sorted(mid_buckets.items()):
        for part, chunk in enumerate(_chunks(reqs, 10), start=1):
            when = datetime.fromisoformat(day + "T" + f"{rng.randint(10, 18):02d}:00")
            mail = build_mid_delivery(rng, code, chunk, when)
            stem = f"{day}_{code}_mid_{part}"
            _write_mail(mail, stem)
            for request in chunk:
                truth.append((f"{stem}.txt", request.request_id, "mid_received", ""))
            written += 1

    with (OUT / "email_truth.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["email_file", "request_id", "outcome", "rejection_reason"])
        w.writerows(truth)

    return written


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _write_mail(mail, stem: str) -> None:
    header = (
        f"From: {mail.acquirer_code.lower()}.ops@example-acquirer.test\n"
        f"To: midops@example-pa.test\n"
        f"Subject: {mail.subject}\n"
        f"Date: {mail.sent_at:%a, %d %b %Y %H:%M:%S +0530}\n"
    )
    if mail.attachment_name:
        # Name the attachment after the email, not after the day. An acquirer
        # that sends twice in one day would otherwise have its second sheet
        # overwrite its first, and the rows in it would vanish without any
        # error at all.
        suffix = Path(mail.attachment_name).suffix
        mail.attachment_name = f"{stem}{suffix}"
        header += f"Attachment: {mail.attachment_name}\n"
    (EMAILS / f"{stem}.txt").write_text(header + "\n" + mail.body + "\n")
    if mail.attachment_csv:
        (EMAILS / mail.attachment_name).write_text(mail.attachment_csv + "\n")


def write_summary(data, rows, email_count: int) -> str:
    by_status: dict[str, int] = defaultdict(int)
    by_acquirer: dict[str, int] = defaultdict(int)
    by_stream: dict[str, int] = defaultdict(int)
    bounced = 0

    for r, t in rows:
        by_status[r.status.value] += 1
        by_acquirer[r.acquirer.code] += 1
        by_stream[r.stream.value] += 1
        if t.ops_episode_count > 1:
            bounced += 1

    # Requests still sitting in the queue have an open clock, so their ops
    # days keep climbing. Blending them into the average would make the
    # headline move with backlog size rather than with how ops is performing.
    # Both figures are reported, and the gap between them is the backlog.
    live = [(r, t) for r, t in rows if not t.deprioritised]
    closed = [t for r, t in live if getattr(r, "scenario", "") != "in_ops_queue"]
    queued = [t for r, t in live if getattr(r, "scenario", "") == "in_ops_queue"]

    ops_solo = [t.ops_solo_days for t in closed] or [0]
    queue_days = [t.ops_solo_days for t in queued]
    acq_days = [t.acquirer_total_days for _, t in live if t.acquirer_total_days > 0]
    answered = [
        (r, t) for r, t in live if t.acquirer_status_days > 0
    ]
    breaches = sum(
        1
        for r, t in answered
        if t.acquirer_worst_status_round > r.acquirer.status_sla_days
    )

    lines = [
        "SYNTHETIC DATASET",
        f"generated as of {AS_OF:%d %b %Y}, seed {SEED}",
        "",
        f"merchants           {len(data.merchants)}",
        f"requests            {len(data.requests)}",
        f"events              {sum(len(r.events) for r in data.requests)}",
        f"bank emails         {email_count}",
        "",
        "By stream",
    ]
    for k, v in sorted(by_stream.items()):
        lines.append(f"  {k:<18}{v}")

    lines += ["", "By acquirer"]
    for k, v in sorted(by_acquirer.items()):
        lines.append(f"  {k:<18}{v}")

    lines += ["", "By status"]
    for k, v in sorted(by_status.items(), key=lambda x: -x[1]):
        lines.append(f"  {k:<34}{v:>5}  {v / len(rows) * 100:5.1f}%")

    lines += [
        "",
        "Headline figures",
        f"  requests that bounced        {bounced} ({bounced / len(rows) * 100:.0f}%)",
        f"  avg ops solo days            {sum(ops_solo) / len(ops_solo):.1f}   (excludes the unraised queue)",
    ]
    if acq_days:
        lines.append(f"  avg acquirer days            {sum(acq_days) / len(acq_days):.1f}")
    if answered:
        lines.append(
            f"  acquirer SLA breaches        {breaches} of {len(answered)} answered "
            f"({breaches / len(answered) * 100:.0f}%)"
        )
    if queue_days:
        queue_days.sort()
        lines += [
            "",
            "The unraised queue, reported separately",
            f"  requests waiting             {len(queue_days)}",
            f"  median days waiting          {queue_days[len(queue_days) // 2]}",
            f"  oldest                       {queue_days[-1]} days",
            f"  waiting over 5 days          {sum(1 for d in queue_days if d > 5)}",
        ]
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    calendar = load_calendar(HOLIDAYS)
    engine = TatEngine(calendar)

    gen = DataGenerator(ACQUIRERS, calendar, seed=SEED, as_of=AS_OF)
    data = gen.generate()

    write_merchants(data)
    write_requests(data)
    write_events(data)
    rows = write_tat(data, engine)

    rng = random.Random(SEED + 1)
    email_count = write_emails(data, rng)

    summary = write_summary(data, rows, email_count)
    (OUT / "summary.txt").write_text(summary + "\n")
    print(summary)


if __name__ == "__main__":
    main()
