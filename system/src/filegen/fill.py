"""
Turn one request into the rows a bank's template wants.

The mapping is the whole job, and it is not a rename. A record holds
`state: "Karnataka"`; one bank wants that, another wants `29`. A record holds
ISO dates; the banks want four different formats between them. A record holds
instruments as a list; the forms want `CC,DC`. The catalogue holds a category
tree; the forms want an industry from a dropdown of twelve words.

Two rules here matter more than the code.

**A derived value has a named rule, and an unknown rule raises.** Defaulting
to a blank would put an empty cell in a mandatory column and the file would
bounce at the bank, a week later, with no indication of which rule was never
written. Failing at build time costs a minute.

**Nothing is invented to fill a cell.** A value ops does not hold is left
empty and reported, with the team that owns it named. Guessing a settlement
cycle or carrying over last quarter's MDR is how a merchant goes live on the
wrong commercials, and it is the kind of error nobody traces back to the
template.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from config.templates import INDUSTRY_BY_SUPER_CATEGORY, TEMPLATES
from ..catalogue.eligibility import EligibilityResult
from ..catalogue.mcc import CATALOGUE, Instrument, MccEntry
from ..model import ProcurementRequest
from .spec import Column, Grain, Necessity, Sheet, Source, Template, format_date

# How instruments are written in these files. Order is fixed so the same set
# never produces two different strings.
INSTRUMENT_CODE: dict[Instrument, str] = {
    Instrument.CREDIT_CARD: "CC",
    Instrument.DEBIT_CARD: "DC",
    Instrument.EMI: "EMI",
    Instrument.CREDIT_LINE_UPI: "UPI",
    Instrument.OVERDRAFT: "OD",
}
_ORDER = list(INSTRUMENT_CODE)


def instrument_set(instruments: tuple[Instrument, ...]) -> str:
    return ",".join(
        INSTRUMENT_CODE[i] for i in _ORDER if i in instruments
    )


@dataclass
class BatchMeta:
    """What ops supplies once for the whole batch, not per merchant."""

    lot_no: str
    login_date: date
    login_mode: str = "Bulk Login"
    remark: str = ""
    licence_refs: dict[str, str] = field(default_factory=dict)  # request_id -> reference


@dataclass
class Context:
    request: ProcurementRequest
    entry: MccEntry
    eligibility: EligibilityResult
    meta: BatchMeta
    # The commercial terms BD agreed. A separate record on purpose: ops cannot
    # fill these in, and a blank here is a chase to another team rather than a
    # gap in the merchant's own data.
    deal: dict[str, str] = field(default_factory=dict)

    @property
    def fields(self) -> dict[str, str]:
        """The merchant record plus the few values that belong to the request.

        The MCC is the one that matters: it is a property of the use case, not
        of the merchant, so reading it off the merchant record puts a blank in
        a mandatory column on every row. The same mistake, in the same place,
        as the phase 5 bug.
        """
        return {
            **self.request.merchant.fields,
            "mcc": self.request.mcc,
            "acquirer_code": self.request.acquirer.code,
            "acquirer_name": self.request.acquirer.name,
        }


@dataclass
class Unfilled:
    """A cell left empty, and who has to fill it.

    The owner is the useful half. "17 empty cells" tells ops nothing; "sales
    owes a settlement cycle on 4 merchants" is a message that can be sent.
    """

    sheet: str
    header: str
    necessity: Necessity
    owner: Source
    request_id: str
    merchant_name: str
    note: str = ""

    @property
    def blocks_the_file(self) -> bool:
        return self.necessity is Necessity.MANDATORY


# --- derivations -----------------------------------------------------------
#
# Each one is a named rule. Adding a derived column to a template without
# adding its rule here raises on build rather than writing a blank.


def _industry(c: Context) -> str:
    return INDUSTRY_BY_SUPER_CATEGORY[c.entry.super_category]


def _sub_industry(c: Context) -> str:
    return c.entry.category


def _nature_of_business(c: Context) -> str:
    return c.entry.description


def _category_description(c: Context) -> str:
    """For the acquirer whose own policy is prose: the business in words.

    Sending only a code to a bank that does not work in codes guarantees a
    round trip, and a round trip here costs the full response SLA.
    """
    return f"{c.entry.sub_category} ({c.entry.category}) - {c.entry.description}"


def _instruments(c: Context) -> str:
    return instrument_set(c.request.instruments)


def _instruments_permitted(c: Context) -> str:
    """What this acquirer will actually run, which is not always what was asked.

    A conditional verdict can block one rail and allow the rest. Writing the
    requested set into a file the bank will partly refuse produces a rejection
    on the whole row; stating both gets the MID for the rails that are open.
    """
    blocked = set(c.eligibility.blocked_instruments)
    allowed = tuple(i for i in c.request.instruments if i not in blocked)
    return instrument_set(allowed)


def _risk_label(c: Context) -> str:
    return c.entry.risk.value.replace("_", " ")


def _deal_name(c: Context) -> str:
    return f"{c.request.merchant.legal_name} - {c.request.mcc}"


def _business_model_long(c: Context) -> str:
    """One acquirer's dropdown wants a sentence, not initials."""
    return "Business to Consumer"


def _contact_first_name(c: Context) -> str:
    return c.fields.get("contact_name", "").split(" ")[0]


def _contact_last_name(c: Context) -> str:
    parts = c.fields.get("contact_name", "").split(" ")
    return parts[-1] if len(parts) > 1 else ""


def _incorporation_date(c: Context) -> str:
    raw = c.fields.get("incorporation_date", "")
    fmt = TEMPLATES[c.request.acquirer.code].date_format
    return format_date(raw, fmt) if raw else ""


def _login_date(c: Context) -> str:
    fmt = TEMPLATES[c.request.acquirer.code].date_format
    return format_date(c.meta.login_date, fmt)


def _login_mode(c: Context) -> str:
    return c.meta.login_mode


def _lot_no(c: Context) -> str:
    return c.meta.lot_no


def _remark(c: Context) -> str:
    return c.meta.remark


def _licence_ref(c: Context) -> str:
    return c.meta.licence_refs.get(c.request.request_id, "")


DERIVATIONS = {
    "industry": _industry,
    "sub_industry": _sub_industry,
    "nature_of_business": _nature_of_business,
    "category_description": _category_description,
    "instrument_set": _instruments,
    "instrument_set_permitted": _instruments_permitted,
    "risk_label": _risk_label,
    "deal_name": _deal_name,
    "business_model_long": _business_model_long,
    "contact_first_name": _contact_first_name,
    "contact_last_name": _contact_last_name,
    "incorporation_date": _incorporation_date,
    # Supplied by ops per batch rather than computed, but resolved the same way
    # so a template can ask for them by name.
    "login_date": _login_date,
    "login_mode": _login_mode,
    "lot_no": _lot_no,
    "remark": _remark,
    "licence_ref": _licence_ref,
}


def _cell(column: Column, c: Context) -> str:
    if column.source is Source.CONSTANT:
        return column.key
    if column.source is Source.FIELD:
        return str(c.fields.get(column.key, "") or "")
    if column.source in (Source.DERIVED, Source.OPS_INPUT):
        if not column.key:
            return ""
        try:
            rule = DERIVATIONS[column.key]
        except KeyError as exc:
            # Loud on purpose. A template asking for a value with no rule to
            # produce it is a configuration error, and a blank in its place is
            # a file the bank bounces for a reason nobody can trace.
            raise KeyError(
                f"{c.request.acquirer.code}: column {column.header!r} wants "
                f"derived value {column.key!r}, which has no rule in DERIVATIONS"
            ) from exc
        return str(rule(c) or "")
    if column.source in (Source.SALES_OWNED, Source.FINANCE_OWNED):
        # Another team's data. Taken from their record if they have supplied
        # it, and left empty if not: never inferred, because a plausible
        # settlement cycle or rate put in by ops is a commercial term nobody
        # agreed, and it goes live.
        return str(c.deal.get(column.key, "") or "") if column.key else ""
    return ""


# Conditions that decide whether a conditional column is actually required on
# a given row. Without these, a column needed by one merchant in forty is
# reported as missing on all forty, and the real findings are buried.
CONDITIONS = {
    "needs_licence": lambda c: c.entry.risk.needs_licence,
    "instrument_restricted": lambda c: bool(c.eligibility.blocked_instruments),
}


def _applies(column: Column, c: Context) -> bool:
    if column.necessity is not Necessity.CONDITIONAL or not column.applies_when:
        return True
    try:
        return bool(CONDITIONS[column.applies_when](c))
    except KeyError as exc:
        raise KeyError(
            f"column {column.header!r} is conditional on "
            f"{column.applies_when!r}, which is not defined in CONDITIONS"
        ) from exc


@dataclass
class FilledRow:
    sheet: str
    request_id: str
    merchant_name: str
    values: dict[str, str]


@dataclass
class FilledFile:
    acquirer_code: str
    template: Template
    rows: list[FilledRow]
    unfilled: list[Unfilled]

    @property
    def request_ids(self) -> list[str]:
        return sorted({r.request_id for r in self.rows})

    def rows_for(self, sheet_name: str) -> list[FilledRow]:
        return [r for r in self.rows if r.sheet == sheet_name]

    @property
    def owed_by_team(self) -> dict[str, int]:
        """Empty mandatory cells, counted by the team that owes them."""
        out: dict[str, int] = {}
        for u in self.unfilled:
            if u.blocks_the_file:
                out[u.owner.value] = out.get(u.owner.value, 0) + 1
        return out


def _for_grain(sheet: Sheet, contexts: list[Context]) -> list[Context]:
    """One context per row this sheet should hold.

    For a merchant-grain sheet, the first use case wins and the rest are
    dropped: the identity block is identical across them by construction, so
    there is nothing to choose between them.
    """
    if sheet.grain is Grain.APPLICATION:
        return contexts
    seen: set[str] = set()
    out: list[Context] = []
    for c in contexts:
        mid = c.request.merchant.merchant_id
        if mid in seen:
            continue
        seen.add(mid)
        out.append(c)
    return out


def fill_sheet(
    sheet: Sheet, contexts: list[Context], template: Template
) -> tuple[list[FilledRow], list[Unfilled]]:
    rows: list[FilledRow] = []
    unfilled: list[Unfilled] = []

    for c in _for_grain(sheet, contexts):
        values: dict[str, str] = {}
        for col in sheet.columns:
            value = _cell(col, c)
            values[col.header] = value
            if not value and col.necessity is not Necessity.OPTIONAL and _applies(col, c):
                unfilled.append(
                    Unfilled(
                        sheet=sheet.name,
                        header=col.header,
                        necessity=col.necessity,
                        owner=col.source,
                        request_id=c.request.request_id,
                        merchant_name=c.request.merchant.legal_name,
                        note=col.note,
                    )
                )
        rows.append(
            FilledRow(
                sheet=sheet.name,
                request_id=c.request.request_id,
                merchant_name=c.request.merchant.legal_name,
                values=values,
            )
        )
    return rows, unfilled


def fill(
    acquirer_code: str, requests: list[ProcurementRequest], meta: BatchMeta,
    eligibility: dict[str, EligibilityResult],
    deals: dict[str, dict[str, str]] | None = None,
) -> FilledFile:
    """Build every sheet of one acquirer's file from the requests going to it.

    The identity block is resolved once per request and written to each sheet
    that asks for it, which is what stops two tabs of the same file describing
    the same merchant differently.
    """
    template = TEMPLATES[acquirer_code]
    deals = deals or {}
    contexts = [
        Context(
            request=r,
            entry=CATALOGUE[r.mcc],
            eligibility=eligibility[r.request_id],
            meta=meta,
            deal=deals.get(r.merchant.merchant_id, {}),
        )
        for r in requests
    ]

    rows: list[FilledRow] = []
    unfilled: list[Unfilled] = []
    for sheet in template.sheets:
        sheet_rows, sheet_unfilled = fill_sheet(sheet, contexts, template)
        rows.extend(sheet_rows)
        unfilled.extend(sheet_unfilled)

    return FilledFile(
        acquirer_code=acquirer_code,
        template=template,
        rows=rows,
        unfilled=unfilled,
    )
