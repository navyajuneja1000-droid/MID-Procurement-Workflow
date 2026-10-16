"""
Check the file before it is sent, not after the bank bounces it.

This is the cheapest check in the whole process and the one with the longest
feedback loop if it is skipped. A file rejected on upload comes back hours or
days later, and the SLA clock for that batch starts again from zero. Worse,
most of these errors are invisible to the person who made them: a pincode that
lost its leading zero, a date that reads as 4 March instead of 3 April, an
entity type typed as "Pvt Ltd" where the sheet's list says "Private Limited".

Findings are split into two kinds, because conflating them makes the check
useless. A blocker means the bank will refuse the file or read a value wrongly.
A warning means a person should look, but sending is still the right call.

The checks that earn their place are the ones a careful human cannot do: the
same merchant described two ways in two tabs, a value one character off a
dropdown list, a number Excel will reformat on save.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .fill import FilledFile
from .spec import Grain, Necessity, Source


class Severity(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"


@dataclass
class Finding:
    severity: Severity
    check: str
    message: str
    request_id: str = ""
    sheet: str = ""
    header: str = ""


@dataclass
class Preflight:
    acquirer_code: str
    findings: list[Finding]

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def can_send(self) -> bool:
        return not self.blockers

    def summary(self) -> str:
        if self.can_send and not self.warnings:
            return "Clean."
        bits = []
        if self.blockers:
            bits.append(f"{len(self.blockers)} blocking")
        if self.warnings:
            bits.append(f"{len(self.warnings)} to look at")
        verdict = "hold" if self.blockers else "send"
        return f"{', '.join(bits)}; {verdict}."


def _check_unfilled(f: FilledFile) -> list[Finding]:
    """Empty cells, reported by the team that owes them.

    One finding per column per team rather than per cell: forty merchants
    missing a settlement cycle is one conversation with sales, not forty
    findings that bury everything else.
    """
    out: list[Finding] = []
    grouped: dict[tuple[str, str, Source, Necessity], list[str]] = {}
    for u in f.unfilled:
        grouped.setdefault((u.sheet, u.header, u.owner, u.necessity), []).append(
            u.merchant_name
        )

    for (sheet, header, owner, necessity), names in sorted(grouped.items()):
        n = len(names)
        who = {
            Source.SALES_OWNED: "sales owns this",
            Source.FINANCE_OWNED: "finance owns this",
            Source.OPS_INPUT: "ops has to supply it for the batch",
            Source.FIELD: "missing from the merchant record",
            Source.DERIVED: "the rule produced nothing",
        }.get(owner, "nobody owns this yet")

        # A conditional column is only a blocker when the condition holds, and
        # the condition is not knowable here, so it is a warning with the
        # condition quoted. A mandatory blank is a refused file.
        severity = (
            Severity.BLOCKER if necessity is Necessity.MANDATORY else Severity.WARNING
        )
        out.append(
            Finding(
                severity=severity,
                check="empty cell",
                message=f"{header} empty on {n} row{'' if n == 1 else 's'} ({who})",
                sheet=sheet,
                header=header,
            )
        )
    return out


def _check_domains(f: FilledFile) -> list[Finding]:
    """A value one character off the sheet's own list.

    This is the check that pays for the whole module. The bank's upload
    compares the cell against its list exactly, so "Pvt Ltd" fails where
    "Private Limited" passes, and nothing about the file looks wrong.
    """
    out: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()

    for sheet in f.template.sheets:
        by_header = {c.header: c for c in sheet.columns if c.domain}
        for row in f.rows_for(sheet.name):
            for header, column in by_header.items():
                value = row.values.get(header, "")
                if not value:
                    continue
                domain = f.template.domains.get(column.domain)
                if domain is None:
                    out.append(
                        Finding(
                            severity=Severity.BLOCKER,
                            check="missing domain",
                            message=(
                                f"{header} is declared against dropdown "
                                f"{column.domain!r}, which this template does not "
                                "define, so the value cannot be checked at all"
                            ),
                            sheet=sheet.name,
                            header=header,
                        )
                    )
                    continue
                if domain.accepts(value):
                    continue
                key = (sheet.name, header, value)
                if key in seen:
                    continue
                seen.add(key)
                hint = domain.closest(value)
                tail = f"; closest permitted value is {hint!r}" if hint else ""
                out.append(
                    Finding(
                        # A strict dropdown refuses the file. A loose one takes
                        # the value and queries it later, which is a slower and
                        # cheaper failure.
                        severity=Severity.BLOCKER if domain.strict else Severity.WARNING,
                        check="off-list value",
                        message=f"{header} holds {value!r}, not in {domain.name}{tail}",
                        request_id=row.request_id,
                        sheet=sheet.name,
                        header=header,
                    )
                )
    return out


def _check_duplicates(f: FilledFile) -> list[Finding]:
    """The same application twice in one file.

    A bank that receives one merchant twice either rejects the file or issues
    two MIDs, and the second is live, unmonitored and billable.
    """
    out: list[Finding] = []
    for sheet in f.template.sheets:
        # What counts as a duplicate depends on what a row is. On the deal tab
        # one merchant may legitimately appear twice, once per use case. On an
        # identity tab the same appearance asks the bank to open two accounts
        # for one merchant.
        per_merchant = sheet.grain is Grain.MERCHANT
        counts: dict[str, int] = {}
        for row in f.rows_for(sheet.name):
            key = row.merchant_name if per_merchant else row.request_id
            counts[key] = counts.get(key, 0) + 1
        for key, n in counts.items():
            if n > 1:
                what = "the merchant" if per_merchant else "the application"
                out.append(
                    Finding(
                        severity=Severity.BLOCKER,
                        check="duplicate row",
                        message=(
                            f"{key} appears {n} times on {sheet.name}, which is "
                            f"one row per {what}"
                        ),
                        sheet=sheet.name,
                    )
                )
    return out


def _check_mirrors(f: FilledFile) -> list[Finding]:
    """Two tabs describing one merchant two ways.

    The generator writes both from one source, so this cannot fail here. It is
    checked anyway, because it is the error the hand-filled version makes most
    often, and a check that only passes is still the proof the file is
    internally consistent.
    """
    out: list[Finding] = []
    for sheet in f.template.sheets:
        if not sheet.mirrors:
            continue
        source_rows = {r.request_id: r for r in f.rows_for(sheet.mirrors)}
        shared = {c.header for c in sheet.columns} & {
            c.header for c in f.template.columns_for(sheet.mirrors)
        }
        for row in f.rows_for(sheet.name):
            other = source_rows.get(row.request_id)
            if other is None:
                out.append(
                    Finding(
                        severity=Severity.BLOCKER,
                        check="mirror gap",
                        message=(
                            f"{row.request_id} is on {sheet.name} but missing from "
                            f"{sheet.mirrors}"
                        ),
                        request_id=row.request_id,
                        sheet=sheet.name,
                    )
                )
                continue
            for header in sorted(shared):
                if row.values.get(header) != other.values.get(header):
                    out.append(
                        Finding(
                            severity=Severity.BLOCKER,
                            check="tabs disagree",
                            message=(
                                f"{header} differs between {sheet.mirrors} and "
                                f"{sheet.name} for {row.merchant_name}"
                            ),
                            request_id=row.request_id,
                            sheet=sheet.name,
                            header=header,
                        )
                    )
    return out


def _check_number_mangling(f: FilledFile) -> list[Finding]:
    """Values a spreadsheet will quietly rewrite.

    A pincode starting 0 loses the zero. A twelve-digit account number becomes
    6.95495E+11. The file looks right on screen and the bank reads the mangled
    value, so these columns are written as text and checked here.
    """
    out: list[Finding] = []
    flagged: set[tuple[str, str]] = set()
    for sheet in f.template.sheets:
        risky = [c for c in sheet.columns if c.text_format]
        for row in f.rows_for(sheet.name):
            for column in risky:
                value = row.values.get(column.header, "")
                if not value or not value.replace(" ", "").isdigit():
                    continue
                at_risk = value.startswith("0") or len(value) >= 12
                if not at_risk:
                    continue
                key = (sheet.name, column.header)
                if key in flagged:
                    continue
                flagged.add(key)
                out.append(
                    Finding(
                        severity=Severity.WARNING,
                        check="number formatting",
                        message=(
                            f"{column.header} holds values a spreadsheet would "
                            "reformat; written as text so the bank reads what was sent"
                        ),
                        sheet=sheet.name,
                        header=column.header,
                    )
                )
    return out


def _check_row_cap(f: FilledFile) -> list[Finding]:
    cap = f.template.max_rows
    if not cap:
        return []
    n = len(f.request_ids)
    if n <= cap:
        return []
    return [
        Finding(
            severity=Severity.BLOCKER,
            check="row cap",
            message=(
                f"{n} applications in one file, above this acquirer's cap of {cap}. "
                f"Split into {-(-n // cap)} files."
            ),
        )
    ]


def _check_csv_safety(f: FilledFile) -> list[Finding]:
    """A comma or a newline inside a value, in a format with no quoting rules
    the bank's importer is guaranteed to honour. One address with a line break
    shifts every column after it."""
    if f.template.file_format != "csv":
        return []
    out: list[Finding] = []
    for row in f.rows:
        for header, value in row.values.items():
            if "\n" in value or "\r" in value:
                out.append(
                    Finding(
                        severity=Severity.BLOCKER,
                        check="csv safety",
                        message=f"{header} contains a line break on {row.merchant_name}",
                        request_id=row.request_id,
                        sheet=row.sheet,
                        header=header,
                    )
                )
    return out


CHECKS = (
    _check_unfilled,
    _check_domains,
    _check_duplicates,
    _check_mirrors,
    _check_number_mangling,
    _check_row_cap,
    _check_csv_safety,
)


def preflight(f: FilledFile) -> Preflight:
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(f))
    findings.sort(key=lambda x: (x.severity is not Severity.BLOCKER, x.check, x.message))
    return Preflight(acquirer_code=f.acquirer_code, findings=findings)


def blocking_rows(f: FilledFile) -> dict[str, str]:
    """Which individual applications would stop this file, and why.

    The same principle as partial raising at intake: four merchants waiting on
    a settlement cycle should not hold the other fifty out of this morning's
    batch. Holding the whole file is the expensive default, because the next
    slot is tomorrow and the clock on all fifty keeps running.

    Row-level problems only. A row cap or a template with an undefined dropdown
    is a problem with the file, not with any particular merchant, and dropping
    rows would not fix it.
    """
    out: dict[str, str] = {}

    for u in f.unfilled:
        if u.blocks_the_file and u.request_id not in out:
            owner = {
                Source.SALES_OWNED: "waiting on sales",
                Source.FINANCE_OWNED: "waiting on finance",
                Source.OPS_INPUT: "waiting on a batch value from ops",
            }.get(u.owner, "missing from the merchant record")
            out[u.request_id] = f"{u.header}: {owner}"

    for sheet in f.template.sheets:
        by_header = {c.header: c for c in sheet.columns if c.domain}
        for row in f.rows_for(sheet.name):
            if row.request_id in out:
                continue
            for header, column in by_header.items():
                value = row.values.get(header, "")
                domain = f.template.domains.get(column.domain)
                if not value or domain is None or not domain.strict:
                    continue
                if not domain.accepts(value):
                    out[row.request_id] = f"{header}: {value!r} is not a permitted value"
                    break
    return out
