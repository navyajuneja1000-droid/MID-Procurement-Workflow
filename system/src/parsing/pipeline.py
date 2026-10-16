"""
Read an inbox of bank replies and turn it into events.

The decision this module makes, row by row, is whether to apply the update or
put it in front of a human.

Rows the parser is sure about are applied the moment they are read, because
those clocks should stop immediately. Rows it is unsure about go to a review
queue. Holding the confident rows back until someone checks the doubtful ones
would delay thirty-five correct updates for the sake of five uncertain ones.

A wrong auto-applied row is worse than a slow one: it writes a wrong event into
the clock, and every TAT figure downstream inherits the error. So the bar for
applying without review is high, and anything ambiguous fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..model import EventType
from .classify import classify
from .extract import ParsedEmail, parse_email
from .match import MatchResult, RequestIndex

# Outcomes that close a request, after which later replies about it are stale.
TERMINAL = {EventType.ACQUIRER_REJECTED, EventType.MID_RECEIVED}


@dataclass
class Update:
    """One proposed change to one request."""

    email_file: str
    acquirer: str
    row_name: str
    row_mcc: str
    request_id: str | None
    outcome: EventType | None
    rejection_reason: str
    mid: str = ""
    confidence: float = 0.0
    match_reason: str = ""
    classify_reason: str = ""
    status: str = "review"  # "applied" | "review"
    review_reason: str = ""

    @property
    def applied(self) -> bool:
        return self.status == "applied"


@dataclass
class InboxResult:
    updates: list[Update] = field(default_factory=list)
    emails_read: int = 0
    emails_failed: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def applied(self) -> list[Update]:
        return [u for u in self.updates if u.applied]

    @property
    def review(self) -> list[Update]:
        return [u for u in self.updates if not u.applied]

    @property
    def auto_rate(self) -> float:
        return len(self.applied) / len(self.updates) if self.updates else 0.0


def read_inbox(folder: Path) -> list[tuple[str, str, str | None]]:
    """Return (filename, body, attachment) for every email in the folder.

    An email names its attachment in a header; the CSV sits beside it, exactly
    as it would after someone saved the inbox to disk.
    """
    out = []
    for path in sorted(folder.glob("*.txt")):
        text = path.read_text()
        attachment = None
        for line in text.splitlines():
            if line.lower().startswith("attachment:"):
                name = line.split(":", 1)[1].strip()
                sidecar = folder / name
                if sidecar.exists():
                    attachment = sidecar.read_text()
                break
        out.append((path.name, text, attachment))
    return out


def _acquirer_for(parsed: ParsedEmail, known: set[str]) -> str:
    """Work out which acquirer sent this, from the address and the subject."""
    hint = parsed.acquirer_hint.upper()
    if hint in known:
        return hint
    subject = parsed.subject.upper()
    for code in known:
        if code in subject:
            return code
    # NORTHWAY_CYBS and NORTHWAY_FSS share a prefix, so a partial hint is not
    # enough to tell them apart: better to fail than to pick the wrong rail.
    partial = [c for c in known if hint and c.startswith(hint)]
    return partial[0] if len(partial) == 1 else ""


def process_email(
    filename: str,
    text: str,
    attachment: str | None,
    index: RequestIndex,
    known_acquirers: set[str],
) -> tuple[list[Update], list[str]]:
    parsed = parse_email(filename, text, attachment)
    acquirer = _acquirer_for(parsed, known_acquirers)

    if not acquirer:
        return [], [f"could not identify the acquirer from '{parsed.subject}'"]
    if parsed.kind == "unknown":
        return [], ["unrecognised email type"]

    updates: list[Update] = []

    if parsed.kind == "mid":
        for block in parsed.mids:
            name = block.get("merchant", "")
            mcc = block.get("mcc", "")
            m: MatchResult = index.match(acquirer, name, mcc)
            u = Update(
                email_file=filename,
                acquirer=acquirer,
                row_name=name,
                row_mcc=mcc,
                request_id=m.request_id,
                outcome=EventType.MID_RECEIVED,
                rejection_reason="",
                mid=block.get("mid", ""),
                confidence=m.confidence,
                match_reason=m.reason,
                classify_reason="credential email",
            )
            if not m.auto:
                u.review_reason = m.reason
            elif not u.mid:
                u.review_reason = "no MID found in the block"
            else:
                u.status = "applied"
            updates.append(u)
        return updates, parsed.problems

    for row in parsed.rows:
        m = index.match(acquirer, row.name, row.mcc)
        c = classify(row.status, row.remarks)

        u = Update(
            email_file=filename,
            acquirer=acquirer,
            row_name=row.name,
            row_mcc=row.mcc,
            request_id=m.request_id,
            outcome=c.outcome,
            rejection_reason=c.reason.value if c.reason else "",
            confidence=min(m.confidence, c.outcome_confidence),
            match_reason=m.reason,
            classify_reason=c.why,
        )

        if c.outcome is None:
            u.review_reason = "status wording not recognised"
        elif not m.auto:
            u.review_reason = m.reason
        elif c.outcome is EventType.ACQUIRER_REJECTED and c.reason_confidence < 0.5:
            # The decision is clear but the reason is not. Apply the rejection
            # so the clock stops, and flag the reason: a mis-bucketed reason
            # quietly corrupts the rejection-reasons report that ops relies on.
            u.status = "applied"
            u.review_reason = "rejection applied, reason needs confirming"
        else:
            u.status = "applied"

        updates.append(u)

    return updates, parsed.problems


def process_inbox(folder: Path, requests: list[dict]) -> InboxResult:
    index = RequestIndex(requests)
    known = {r["acquirer"] for r in requests}
    result = InboxResult()

    for filename, text, attachment in read_inbox(folder):
        result.emails_read += 1
        updates, problems = process_email(filename, text, attachment, index, known)
        result.updates.extend(updates)
        if problems:
            result.emails_failed.append((filename, problems))

    return result
