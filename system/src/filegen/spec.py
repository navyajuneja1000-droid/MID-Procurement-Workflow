"""
What a bank submission file looks like, described as data.

Every acquirer hands over a workbook of its own. They differ in the number of
tabs, the column headers, the wording their dropdowns accept, the date format,
and whether a field is wanted at all. Today a person opens the template and
fills it in by hand, which is why the same merchant can end up described two
different ways in two tabs of one file.

Three ideas make the rest of this phase work.

**The template is config, not code.** A bank adding a column is an edit to a
list, not a change to a program. Banks change these templates with no notice
and no version number, so the cost of absorbing a change decides whether the
file is generated or typed.

**Every column declares who owns it.** A column is filled from the record, or
derived from it, or constant, or owned by someone outside ops (sales rates,
finance details). An unowned column that is also mandatory is the single most
useful thing a pre-flight check can report, because it is the thing that will
bounce the whole file.

**Dropdown values belong to a domain.** A bank's upload rejects the entire
file for one cell holding "Pvt Ltd" where the sheet's list says "Private
Limited". A human filling the form cannot see the difference; a check against
the domain can.

All templates here are invented. The shape follows the real article: several
tabs, an identity block repeated across two of them, paired type-and-value
columns for commercial rates, and a dropdown reference sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Source(str, Enum):
    """Where a cell's value comes from. The point is accountability: a file
    that cannot be completed should name who has to supply the rest."""

    FIELD = "field"  # straight off the merchant or request record
    DERIVED = "derived"  # computed from the record by a named rule
    CONSTANT = "constant"  # the same on every row we ever send
    OPS_INPUT = "ops_input"  # ops types it per batch (lot number, login date)
    SALES_OWNED = "sales_owned"  # commercials: rates, subvention, settlement terms
    FINANCE_OWNED = "finance_owned"  # billing and invoicing details
    NOT_APPLICABLE = "not_applicable"  # the bank's column, not ours to fill


class Necessity(str, Enum):
    MANDATORY = "mandatory"  # the bank rejects the file without it
    CONDITIONAL = "conditional"  # mandatory only when a condition holds
    OPTIONAL = "optional"


@dataclass(frozen=True)
class Domain:
    """A dropdown's permitted values.

    `strict` is the difference between a bank whose upload validates the cell
    and one that accepts anything and queries it a week later. Both exist, and
    they deserve different handling: the first is a hard stop before sending,
    the second is a warning.
    """

    name: str
    values: tuple[str, ...]
    strict: bool = True

    def accepts(self, value: str) -> bool:
        return value in self.values

    def closest(self, value: str) -> str:
        """The value the person probably meant. Used in the pre-flight message,
        because "Pvt Ltd is not in the list" is less useful than "Pvt Ltd is
        not in the list; did you mean Private Limited"."""
        from difflib import get_close_matches

        hit = get_close_matches(value, self.values, n=1, cutoff=0.5)
        return hit[0] if hit else ""


@dataclass(frozen=True)
class Column:
    header: str
    source: Source
    necessity: Necessity = Necessity.OPTIONAL
    # FIELD: the record key. DERIVED: the rule name. CONSTANT: the value.
    key: str = ""
    domain: str = ""
    # Identifiers Excel will otherwise reformat: a pincode loses its leading
    # zero, a long account number becomes 6.95495E+11, and the bank reads the
    # mangled value, not what was typed.
    text_format: bool = False
    # For a conditional column: the named condition under which it is actually
    # required. Left empty, a conditional column is reported on every row it is
    # blank on, which is noise when one merchant in forty needs it.
    applies_when: str = ""
    note: str = ""

    @property
    def ops_can_fill(self) -> bool:
        return self.source in (
            Source.FIELD,
            Source.DERIVED,
            Source.CONSTANT,
            Source.OPS_INPUT,
        )


class Grain(str, Enum):
    """What one row of a sheet is.

    Not cosmetic. The identity tabs of these forms are one row per merchant,
    while the deal tab is one row per application. A merchant running two use
    cases therefore belongs on the identity tab once and on the deal tab twice.
    Writing it twice on the identity tab asks the bank to create the same
    account twice, and what comes back is two customer ids for one merchant,
    which is the kind of mess that takes months to unpick.
    """

    APPLICATION = "application"  # one row per merchant + MCC + acquirer
    MERCHANT = "merchant"  # one row per merchant, however many use cases


@dataclass(frozen=True)
class Sheet:
    name: str
    columns: tuple[Column, ...]
    grain: Grain = Grain.APPLICATION
    # Sheets that restate the same merchant block. The real form repeats the
    # identity block in a second tab, and when a person fills both by hand the
    # two can disagree. Generating both from one row is the whole point.
    mirrors: str = ""


@dataclass(frozen=True)
class Template:
    acquirer_code: str
    file_format: str  # "xlsx" or "csv"
    sheets: tuple[Sheet, ...]
    domains: dict[str, Domain] = field(default_factory=dict)
    date_format: str = "%d-%m-%Y"
    # Some banks want one file per batch; one wants a row cap and a second file
    # after it, which is a real constraint and silently truncates otherwise.
    max_rows: int = 0
    notes: str = ""

    @property
    def primary(self) -> Sheet:
        return self.sheets[0]

    def columns_for(self, sheet_name: str) -> tuple[Column, ...]:
        for s in self.sheets:
            if s.name == sheet_name:
                return s.columns
        raise KeyError(f"{self.acquirer_code}: no sheet named {sheet_name!r}")

    @property
    def mandatory_gaps(self) -> tuple[Column, ...]:
        """Mandatory columns ops cannot fill from anything it holds.

        Worth having as a property rather than a comment: it is the list of
        things that will stop a file going out, known before a single merchant
        is loaded, and it is the honest answer to "can this be automated".
        """
        return tuple(
            c
            for s in self.sheets
            for c in s.columns
            if c.necessity is Necessity.MANDATORY and not c.ops_can_fill
        )


def format_date(value: str | date | datetime, fmt: str) -> str:
    """ISO in the record, the bank's wording in the file.

    Dates are the quietest failure in this whole phase: 03-04-2026 is read as
    3 April by one bank and 4 March by another, and nothing in either file
    looks wrong.
    """
    if isinstance(value, str):
        if not value:
            return ""
        value = date.fromisoformat(value[:10])
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime(fmt)
