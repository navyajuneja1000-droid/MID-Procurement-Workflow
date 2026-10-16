"""
Pull rows out of a bank reply, whatever shape it arrived in.

Three shapes turn up: a pipe table pasted into the email body, a numbered list
in the body, and a CSV the acquirer attached instead. Column names differ per
acquirer and none of them agree.

The extractor is deliberately format-driven rather than acquirer-driven. Going
by the acquirer code would mean a bank that changes its layout silently
produces zero rows, and nobody would notice until a week of TATs were wrong.
Sniffing the shape means a changed layout still parses if it is one of the
shapes we know, and fails loudly if it is not.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

MCC_RE = re.compile(r"\b(\d{4})\b")

# Column headings seen across acquirers, grouped by what they mean.
NAME_HEADS = {"merchant name", "legal name", "entity", "merchant", "merchant legal name"}
MCC_HEADS = {"mcc", "mcc code"}
STATUS_HEADS = {"status", "application status", "decision", "current status"}
REMARK_HEADS = {"remarks", "comments", "reason", "observation", "note"}

# Lines that are never data.
BOILERPLATE = re.compile(
    r"^(dear|hi|hello|please find|please refer|regards|thanks|thank you|"
    r"remaining|balance|status update|from:|to:|subject:|date:|attachment:)",
    re.IGNORECASE,
)


@dataclass
class RawRow:
    """One line of a bank reply, before anything is interpreted."""

    name: str
    mcc: str
    status: str
    remarks: str
    source_line: str = ""


@dataclass
class ParsedEmail:
    path: str
    subject: str
    acquirer_hint: str
    sent_at: str
    kind: str  # "status" | "mid" | "unknown"
    shape: str  # "body_table" | "body_list" | "csv" | "mid_block" | "none"
    rows: list[RawRow] = field(default_factory=list)
    mids: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _headers(line: str, sep: str) -> dict[str, int] | None:
    """Map our four meanings onto this acquirer's column positions."""
    cells = [c.strip().lower() for c in line.split(sep)]
    found: dict[str, int] = {}
    for i, c in enumerate(cells):
        if c in NAME_HEADS and "name" not in found:
            found["name"] = i
        elif c in MCC_HEADS and "mcc" not in found:
            found["mcc"] = i
        elif c in STATUS_HEADS and "status" not in found:
            found["status"] = i
        elif c in REMARK_HEADS and "remarks" not in found:
            found["remarks"] = i
    # A name and a status are the minimum for a row to mean anything.
    return found if "name" in found and "status" in found else None


def _rows_from_delimited(text: str, sep: str) -> tuple[list[RawRow], list[str]]:
    rows: list[RawRow] = []
    problems: list[str] = []
    cols: dict[str, int] | None = None

    reader = (
        csv.reader(io.StringIO(text))
        if sep == ","
        else ([c for c in line.split(sep)] for line in text.splitlines())
    )

    for cells in reader:
        if not cells:
            continue
        line = sep.join(cells)
        if set(line.strip()) <= {"-", " ", "|"}:
            continue  # the rule under a table header

        if cols is None:
            cols = _headers(line, sep)
            continue  # whether or not it was the header, it is not data

        if BOILERPLATE.match(line.strip()):
            continue
        if len(cells) <= max(cols.values()):
            if line.strip():
                problems.append(f"short row: {line.strip()[:60]}")
            continue

        name = cells[cols["name"]].strip()
        status = cells[cols["status"]].strip()
        if not name or not status:
            continue

        rows.append(
            RawRow(
                name=name,
                mcc=cells[cols["mcc"]].strip() if "mcc" in cols else "",
                status=status,
                remarks=cells[cols["remarks"]].strip() if "remarks" in cols else "",
                source_line=line.strip(),
            )
        )

    if cols is None:
        problems.append("no header row recognised")
    return rows, problems


def _rows_from_list(text: str) -> tuple[list[RawRow], list[str]]:
    """A numbered list: `1. Name (MCC 5812) - Status. Remarks`"""
    pattern = re.compile(
        r"^\s*\d+\.\s*(?P<name>.+?)\s*\(MCC\s*(?P<mcc>\d{4})\)\s*[-–]\s*(?P<rest>.+)$",
        re.IGNORECASE,
    )
    rows: list[RawRow] = []
    problems: list[str] = []

    for line in text.splitlines():
        if not line.strip() or BOILERPLATE.match(line.strip()):
            continue
        m = pattern.match(line)
        if not m:
            if re.match(r"^\s*\d+\.", line):
                problems.append(f"numbered line did not parse: {line.strip()[:60]}")
            continue
        rest = m.group("rest").strip()
        # The status runs up to the first full stop; the remark is the rest.
        status, _, remarks = rest.partition(". ")
        rows.append(
            RawRow(
                name=m.group("name").strip(),
                mcc=m.group("mcc"),
                status=status.strip().rstrip("."),
                remarks=remarks.strip(),
                source_line=line.strip(),
            )
        )

    return rows, problems


MID_FIELD = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<value>.+?)\s*$")
MID_KEYS = {"merchant", "mcc", "mid", "tid"}


def _mids_from_block(text: str) -> tuple[list[dict], list[str]]:
    """Credential emails are key-value blocks separated by blank lines, and the
    key names differ per acquirer (access key, api key, working key...)."""
    blocks: list[dict] = []
    problems: list[str] = []
    current: dict = {}

    for line in text.splitlines():
        if not line.strip():
            if current.get("merchant") and current.get("mid"):
                blocks.append(current)
            elif current:
                problems.append("credential block missing merchant or MID")
            current = {}
            continue
        m = MID_FIELD.match(line)
        if not m:
            continue
        key = m.group("key").strip().lower()
        value = m.group("value").strip()
        if key in MID_KEYS:
            current[key] = value
        elif "key" in key or key == "salt":
            current.setdefault("credentials", {})[key] = value

    if current.get("merchant") and current.get("mid"):
        blocks.append(current)

    return blocks, problems


def parse_email(path: str, text: str, attachment: str | None = None) -> ParsedEmail:
    """Read one email, plus its attachment if it had one."""
    head, _, body = text.partition("\n\n")
    headers = dict(
        (k.strip().lower(), v.strip())
        for k, _, v in (line.partition(":") for line in head.splitlines())
        if k
    )
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    acquirer_hint = sender.split(".")[0].upper() if "." in sender else ""

    out = ParsedEmail(
        path=path,
        subject=subject,
        acquirer_hint=acquirer_hint,
        sent_at=headers.get("date", ""),
        kind="unknown",
        shape="none",
    )

    # Which inbound stream is this? Credentials and statuses arrive on
    # different subjects, and they mean different things to the clock.
    low = subject.lower()
    if "credential" in low or "mid credentials" in low:
        out.kind = "mid"
        out.shape = "mid_block"
        out.mids, out.problems = _mids_from_block(body)
        if not out.mids:
            out.problems.append("credential email yielded no MIDs")
        return out

    out.kind = "status"

    if attachment:
        out.shape = "csv"
        out.rows, out.problems = _rows_from_delimited(attachment, ",")
    elif "|" in body:
        out.shape = "body_table"
        out.rows, out.problems = _rows_from_delimited(body, "|")
    else:
        out.shape = "body_list"
        out.rows, out.problems = _rows_from_list(body)

    if not out.rows:
        out.problems.append("status email yielded no rows")

    return out
