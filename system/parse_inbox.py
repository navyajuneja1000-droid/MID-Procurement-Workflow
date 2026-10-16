"""
Run the parser over the inbox and score it against ground truth.

    python parse_inbox.py

The parser reads only data/emails/, exactly what a real inbox would hand it.
The truth file lives outside that folder and is opened here, after parsing, so
the accuracy figure means something.

Three numbers matter, and they are not the same thing:

  coverage   how many rows the parser was confident enough to apply
  precision  how many of those it got right
  recall     how much of the inbox it handled correctly end to end

A parser that applies everything scores high coverage and low precision, which
is the worst outcome: wrong events in the clock, silently.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from src.parsing.pipeline import process_inbox

DATA = Path("data")
EMAILS = DATA / "emails"


def load_requests() -> list[dict]:
    """The open book, as the system would know it when the replies arrive.

    A request is a candidate while it is still awaiting a decision. Ones
    already procured, rejected or deprioritised are closed, and a reply cannot
    be about them.
    """
    closed = {"procured", "rejected_by_bank", "deprioritised"}
    rows = []
    for r in csv.DictReader((DATA / "requests.csv").open()):
        rows.append(
            {
                "request_id": r["request_id"],
                "name": r["legal_name"],
                "mcc": r["mcc"],
                "acquirer": r["acquirer"],
                # Replies arrive while a request is live, so the index has to
                # hold everything that ever reached a bank, not only what is
                # still open at the as-of date.
                "open": r["status"] not in closed or True,
            }
        )
    return rows


def load_truth() -> dict[tuple[str, str], tuple[str, str]]:
    truth = {}
    for r in csv.DictReader((DATA / "email_truth.csv").open()):
        truth[(r["email_file"], r["request_id"])] = (r["outcome"], r["rejection_reason"])
    return truth


def main() -> None:
    requests = load_requests()
    result = process_inbox(EMAILS, requests)
    truth = load_truth()

    total = len(result.updates)
    applied = result.applied
    review = result.review

    correct_match = 0
    wrong_match = 0
    correct_outcome = 0
    wrong_outcome = 0
    correct_reason = 0
    wrong_reason = 0
    reason_misses: Counter = Counter()

    for u in applied:
        key = (u.email_file, u.request_id or "")
        if key not in truth:
            wrong_match += 1
            continue
        correct_match += 1
        want_outcome, want_reason = truth[key]
        got = u.outcome.value if u.outcome else ""
        if got == want_outcome:
            correct_outcome += 1
        else:
            wrong_outcome += 1
        if want_reason:
            if u.rejection_reason == want_reason:
                correct_reason += 1
            else:
                wrong_reason += 1
                reason_misses[(want_reason, u.rejection_reason or "none")] += 1

    rows_in_truth = len(truth)
    fully_right = correct_outcome - wrong_match

    def pct(a: int, b: int) -> str:
        return f"{a / b * 100:5.1f}%" if b else "    n/a"

    lines = [
        "PARSER RESULTS",
        f"read {result.emails_read} emails from {EMAILS}",
        "",
        f"rows extracted          {total}",
        f"rows in ground truth    {rows_in_truth}",
        "",
        "Coverage",
        f"  applied automatically {len(applied):>5}  {pct(len(applied), total)}",
        f"  sent for review       {len(review):>5}  {pct(len(review), total)}",
        "",
        "Precision, on the rows it applied",
        f"  matched to the right request  {correct_match:>5}  {pct(correct_match, len(applied))}",
        f"  wrong request                 {wrong_match:>5}",
        f"  correct outcome               {correct_outcome:>5}  {pct(correct_outcome, len(applied))}",
        f"  wrong outcome                 {wrong_outcome:>5}",
        "",
        "Recall, end to end",
        f"  rows handled correctly        {fully_right:>5}  {pct(fully_right, rows_in_truth)}",
        "",
        "Rejection reasons, where a reason was expected",
        f"  bucketed correctly            {correct_reason:>5}  "
        f"{pct(correct_reason, correct_reason + wrong_reason)}",
        f"  bucketed wrongly              {wrong_reason:>5}",
    ]

    if reason_misses:
        lines.append("")
        lines.append("  most common reason confusions")
        for (want, got), n in reason_misses.most_common(5):
            lines.append(f"    {want} read as {got}  ({n})")

    why = Counter(u.review_reason.split(" — ")[0].split(":")[0] for u in review)
    if why:
        lines += ["", "Why rows went to review"]
        for reason, n in why.most_common(8):
            lines.append(f"  {reason[:52]:<54}{n:>5}")

    if result.emails_failed:
        lines += ["", f"Emails with extraction problems: {len(result.emails_failed)}"]
        for name, problems in result.emails_failed[:5]:
            lines.append(f"  {name}: {problems[0][:70]}")

    report = "\n".join(lines)
    (DATA / "parser_report.txt").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
