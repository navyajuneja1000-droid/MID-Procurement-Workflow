"""
One submission template per acquirer, described as config.

The shapes here are drawn from how these forms are actually built, not
invented freely:

- The largest is a multi-tab workbook whose merchant identity block appears in
  two tabs, with a dropdown reference sheet and paired type-and-value columns
  for commercial rates. Most of its columns belong to sales or finance, not to
  ops, which is the honest answer to how much of it can be automated.
- Two are single-sheet workbooks with a few dozen columns.
- One is a CSV, because one always is.
- They disagree on date format, on whether the state goes in as a name or a
  GST state code, and on the exact wording their dropdowns accept.

No real bank's template is reproduced. Headers follow the conventions these
forms use so the mapping problem is real, and the dropdown wordings differ
between acquirers on purpose, because that is the failure the pre-flight check
is there to catch.
"""

from __future__ import annotations

from src.filegen.spec import Column, Domain, Grain, Necessity, Sheet, Source, Template

M = Necessity.MANDATORY
C = Necessity.CONDITIONAL
O = Necessity.OPTIONAL


# --- dropdown domains ------------------------------------------------------
#
# Deliberately not shared between acquirers. Two banks asking for the same
# thing in different words is the normal case, and a single shared domain
# would hide the mapping problem instead of modelling it.

INDUSTRY = Domain(
    "industry",
    (
        "Retail", "E-com", "Education", "BFSI", "Travel & Hospitality",
        "Healthcare", "Food & Beverage", "Government", "Entertainment",
        "Wholesale & Manufacturing", "Agriculture", "Services",
    ),
)

ENTITY_TYPE = Domain(
    "entity_type",
    (
        "Private Limited", "Public Limited", "Limited Liability Company",
        "Partnership", "Proprietor", "Trust", "Society", "HUF", "Government",
    ),
)

# The same question, three wordings. One acquirer wants the long form, one
# wants initials, one does not ask at all.
BUSINESS_MODEL = Domain("business_model", ("B2C", "B2B", "Both", "NA"))
BUSINESS_MODEL_LONG = Domain(
    "business_model_long",
    ("Business to Consumer", "Business to Business", "Mixed"),
)

SETTLEMENT = Domain("settlement", ("Daily", "Weekly", "Monthly"))
MID_SCOPE = Domain("mid_scope", ("Domestic", "International"))
YES_NO = Domain("yes_no", ("Yes", "No"))
# Non-strict on purpose: this acquirer's upload accepts anything in the cell
# and queries it later, so an off-list value is a warning rather than a stop.
POINT_OF_CONTACT = Domain(
    "point_of_contact", ("Merchant", "Reseller", "RM", "NA"), strict=False
)

INSTRUMENT_SET = Domain(
    "instrument_set",
    ("CC", "DC", "CC,DC", "CC,DC,EMI", "CC,DC,UPI", "CC,DC,EMI,UPI"),
)


# --- the identity block, written once --------------------------------------


def _identity_block(*, state_as_code: bool = False) -> tuple[Column, ...]:
    """The merchant description every template asks for in some form.

    It exists as a function because the largest template asks for it twice, in
    two tabs. Hand-filling both is how one file ends up describing two
    different companies; generating both from one call cannot.
    """
    return (
        Column("Account Name (Unique)", Source.FIELD, M, key="entity_legal_name"),
        Column("Entity Type", Source.FIELD, M, key="entity_type", domain="entity_type"),
        Column("Firm PAN No", Source.FIELD, M, key="company_pan", text_format=True),
        Column("GSTIN No", Source.FIELD, C, key="company_gst", text_format=True,
               note="mandatory unless the merchant is GST exempt"),
        Column("Registration No", Source.FIELD, O, key="registration_number"),
        Column("Business Address", Source.FIELD, M, key="registered_address"),
        Column("City", Source.FIELD, M, key="city"),
        Column(
            "State Code" if state_as_code else "State",
            Source.FIELD, M,
            key="state_code" if state_as_code else "state",
            text_format=state_as_code,
            note="GST state code, not the state name" if state_as_code else "",
        ),
        Column("Zipcode", Source.FIELD, M, key="pincode", text_format=True),
        Column("Country", Source.CONSTANT, M, key="India"),
        Column("Website", Source.FIELD, M, key="website"),
        Column("Business Registration Date", Source.DERIVED, O, key="incorporation_date"),
        Column("Authorized Signer Name", Source.FIELD, M, key="signatory_name"),
        Column("Authorized Signer PAN No", Source.FIELD, M, key="signatory_pan",
               text_format=True),
        Column("Authorized Signer Address", Source.FIELD, C, key="signatory_address"),
        Column("Email", Source.FIELD, M, key="contact_email"),
        Column("Mobile", Source.FIELD, M, key="contact_mobile", text_format=True),
    )


def _rate_pair(label: str) -> tuple[Column, ...]:
    """A type selector and a value, which is how these forms carry rates.

    Both belong to sales. Ops filling them in from last quarter's sheet is how
    a merchant goes live on the wrong MDR, so they are left blank here and the
    covering email says who owes them.
    """
    return (
        Column(f"Select type for {label}", Source.SALES_OWNED, C,
               note="rate type: percentage or flat"),
        Column(label, Source.SALES_OWNED, C, note="rate value, owned by sales"),
    )


# --- the big one: five tabs, most of it not ops ----------------------------

_CYBS_ACCOUNTS = Sheet(
    "Accounts",
    _identity_block()
    + (
        Column("Industry", Source.DERIVED, M, key="industry", domain="industry"),
        Column("Sub-Industry", Source.DERIVED, O, key="sub_industry"),
        Column("B2C or B2B", Source.SALES_OWNED, O, key="business_model", domain="business_model"),
        Column("Point of contact", Source.SALES_OWNED, O, key="point_of_contact", domain="point_of_contact"),
        Column("Relationship Manager", Source.SALES_OWNED, M, key="relationship_manager"),
        Column("Zonal Head", Source.SALES_OWNED, O, key="zonal_head"),
        Column("Chargeback Contact", Source.FINANCE_OWNED, O),
        Column("Finance Contact", Source.FINANCE_OWNED, O),
        Column("Technical Contact", Source.CONSTANT, O, key="pg-tech@example-pa.test"),
    ),
    grain=Grain.MERCHANT,
)

_CYBS_DEALS = Sheet(
    "Deals",
    (
        Column("Account name", Source.FIELD, M, key="entity_legal_name"),
        Column("Deal Name", Source.DERIVED, M, key="deal_name"),
        Column("MCC required", Source.FIELD, M, key="mcc", text_format=True),
        Column("Nature of Business", Source.DERIVED, M, key="nature_of_business"),
        Column("Product requirement", Source.DERIVED, M, key="instrument_set",
               domain="instrument_set"),
        Column("MID Requirement", Source.CONSTANT, M, key="Domestic", domain="mid_scope"),
        Column("Merchant Settlement", Source.SALES_OWNED, M, key="settlement_cycle", domain="settlement"),
        Column("Login Date", Source.OPS_INPUT, M, key="login_date"),
        Column("Lot No", Source.OPS_INPUT, M, key="lot_no"),
        Column("Bulk Login / Individual Login", Source.DERIVED, M, key="login_mode"),
        Column("Expected Transaction", Source.SALES_OWNED, M, key="expected_transaction",
               note="monthly volume estimate, owned by sales"),
        Column("Beneficiary Bank Name", Source.FIELD, M, key="settlement_bank"),
        Column("Account Holder Name", Source.FIELD, M, key="settlement_account_name"),
        Column("Account Number", Source.FIELD, M, key="settlement_account_number",
               text_format=True),
        Column("IFSC CODE", Source.FIELD, M, key="settlement_ifsc", text_format=True),
        Column("Domain Check Option", Source.CONSTANT, O, key="Yes", domain="yes_no"),
        Column("Live URL", Source.FIELD, O, key="website"),
        Column("Platform (Integration)", Source.CONSTANT, O, key="Standard checkout"),
        Column("Aggregator Type", Source.CONSTANT, M, key="PSP"),
    )
    # The rate card. Fourteen paired columns, none of them ops'.
    + _rate_pair("Credit card MDR")
    + _rate_pair("Debit card lesser than 2000")
    + _rate_pair("Debit card greater than 2000")
    + _rate_pair("UPI")
    + _rate_pair("EMI Conversion")
    + _rate_pair("International MSF")
    + _rate_pair("Corporate Card")
    + (
        Column("Set Up Fees", Source.FINANCE_OWNED, O),
        Column("AMC", Source.FINANCE_OWNED, O),
        Column("Bill to", Source.FINANCE_OWNED, O),
        Column("SBO Remark", Source.OPS_INPUT, O, key="remark"),
    ),
)

# The identity block again, in a second tab. Same source row, so the two tabs
# cannot disagree; filled by hand, they routinely do.
_CYBS_DISCREPANCY = Sheet(
    "Discrepancy",
    _identity_block() + (
        Column("Nature of Business", Source.DERIVED, M, key="nature_of_business"),
        Column("Sales HOD", Source.SALES_OWNED, O, key="sales_hod"),
        Column("SBO Remark", Source.OPS_INPUT, O, key="remark"),
    ),
    grain=Grain.MERCHANT,
    mirrors="Accounts",
)

_CYBS_CONTACTS = Sheet(
    "Contacts",
    (
        Column("First name", Source.DERIVED, M, key="contact_first_name"),
        Column("Last name", Source.DERIVED, M, key="contact_last_name"),
        Column("Mobile", Source.FIELD, M, key="contact_mobile", text_format=True),
        Column("Emails", Source.FIELD, M, key="contact_email"),
        Column("Products Required", Source.DERIVED, M, key="instrument_set",
               domain="instrument_set"),
    ),
    grain=Grain.MERCHANT,
)


TEMPLATES: dict[str, Template] = {
    # The heavy one. Five tabs, an identity block in two of them, and a rate
    # card ops does not own.
    "NORTHWAY_CYBS": Template(
        acquirer_code="NORTHWAY_CYBS",
        file_format="xlsx",
        sheets=(_CYBS_ACCOUNTS, _CYBS_DEALS, _CYBS_DISCREPANCY, _CYBS_CONTACTS),
        domains={
            d.name: d
            for d in (INDUSTRY, ENTITY_TYPE, BUSINESS_MODEL, POINT_OF_CONTACT,
                      SETTLEMENT, MID_SCOPE, YES_NO, INSTRUMENT_SET)
        },
        date_format="%d-%m-%Y",
        notes="Same bank as NORTHWAY_FSS and a completely different template.",
    ),

    # Same bank, other platform, nothing shared. Worth keeping side by side:
    # the instinct to reuse one template per bank is wrong, and this is why.
    "NORTHWAY_FSS": Template(
        acquirer_code="NORTHWAY_FSS",
        file_format="xlsx",
        sheets=(
            Sheet(
                "MID Request",
                _identity_block(state_as_code=True)
                + (
                    Column("MCC", Source.FIELD, M, key="mcc", text_format=True),
                    Column("Industry", Source.DERIVED, M, key="industry",
                           domain="industry"),
                    Column("Instruments", Source.DERIVED, M, key="instrument_set",
                           domain="instrument_set"),
                    Column("Settlement Cycle", Source.SALES_OWNED, M, key="settlement_cycle",
                           domain="settlement"),
                    Column("Batch Reference", Source.OPS_INPUT, M, key="lot_no"),
                    Column("Submitted On", Source.OPS_INPUT, M, key="login_date"),
                ),
            ),
        ),
        domains={d.name: d for d in (INDUSTRY, ENTITY_TYPE, SETTLEMENT, INSTRUMENT_SET)},
        date_format="%Y-%m-%d",
        notes="Wants the GST state code, not the state name, and ISO dates.",
    ),

    "ALDRIDGE": Template(
        acquirer_code="ALDRIDGE",
        file_format="xlsx",
        sheets=(
            Sheet(
                "Onboarding",
                _identity_block()
                + (
                    Column("MCC", Source.FIELD, M, key="mcc", text_format=True),
                    Column("Industry", Source.DERIVED, M, key="industry",
                           domain="industry"),
                    Column("Business Model", Source.DERIVED, M, key="business_model_long",
                           domain="business_model_long"),
                    Column("Card Products", Source.DERIVED, M, key="instrument_set",
                           domain="instrument_set"),
                    Column("Risk Category", Source.DERIVED, M, key="risk_label"),
                    Column("Licence Reference", Source.OPS_INPUT, C, key="licence_ref",
                           applies_when="needs_licence",
                           note="mandatory when the category needs a licence"),
                    Column("Settlement Cycle", Source.SALES_OWNED, M, key="settlement_cycle", domain="settlement"),
                    Column("Batch No", Source.OPS_INPUT, M, key="lot_no"),
                ),
            ),
        ),
        domains={
            d.name: d
            for d in (INDUSTRY, ENTITY_TYPE, BUSINESS_MODEL_LONG, SETTLEMENT,
                      INSTRUMENT_SET)
        },
        date_format="%d/%m/%Y",
        notes="Asks for the risk category and a licence reference where one applies.",
    ),

    "BRINDLE": Template(
        acquirer_code="BRINDLE",
        file_format="xlsx",
        sheets=(
            Sheet(
                "Applications",
                _identity_block()
                + (
                    Column("MCC", Source.FIELD, M, key="mcc", text_format=True),
                    Column("Instruments Requested", Source.DERIVED, M,
                           key="instrument_set", domain="instrument_set"),
                    Column("Instruments Permitted", Source.DERIVED, M,
                           key="instrument_set_permitted", domain="instrument_set",
                           note="what this acquirer will actually run for the category"),
                    Column("Settlement Cycle", Source.SALES_OWNED, M, key="settlement_cycle", domain="settlement"),
                    Column("Batch No", Source.OPS_INPUT, M, key="lot_no"),
                ),
            ),
        ),
        domains={d.name: d for d in (ENTITY_TYPE, SETTLEMENT, INSTRUMENT_SET)},
        date_format="%d-%b-%Y",
        # The one that caps rows. A batch above the cap needs splitting, and a
        # file that silently holds more is rejected in full.
        max_rows=40,
        notes="Rate-restricted by instrument, so the file states both what was "
              "asked for and what the policy permits.",
    ),

    "CASTERLY": Template(
        acquirer_code="CASTERLY",
        file_format="csv",
        sheets=(
            Sheet(
                "merchants",
                (
                    Column("legal_name", Source.FIELD, M, key="entity_legal_name"),
                    Column("pan", Source.FIELD, M, key="company_pan", text_format=True),
                    Column("gstin", Source.FIELD, C, key="company_gst", text_format=True),
                    Column("mcc", Source.FIELD, M, key="mcc", text_format=True),
                    Column("entity_type", Source.FIELD, M, key="entity_type",
                           domain="entity_type"),
                    Column("address", Source.FIELD, M, key="registered_address"),
                    Column("city", Source.FIELD, M, key="city"),
                    Column("state", Source.FIELD, M, key="state"),
                    Column("pincode", Source.FIELD, M, key="pincode", text_format=True),
                    Column("website", Source.FIELD, M, key="website"),
                    Column("signatory_name", Source.FIELD, M, key="signatory_name"),
                    Column("instruments", Source.DERIVED, M, key="instrument_set",
                           domain="instrument_set"),
                    Column("batch", Source.OPS_INPUT, M, key="lot_no"),
                ),
            ),
        ),
        domains={d.name: d for d in (ENTITY_TYPE, INSTRUMENT_SET)},
        date_format="%Y-%m-%d",
        notes="A CSV, so there is no dropdown validation at all: an off-list "
              "value is accepted by the file and rejected by the bank.",
    ),

    "VERRAN": Template(
        acquirer_code="VERRAN",
        file_format="xlsx",
        sheets=(
            Sheet(
                "Sheet1",
                _identity_block()
                + (
                    Column("MCC", Source.FIELD, M, key="mcc", text_format=True),
                    Column("Category Description", Source.DERIVED, M,
                           key="category_description",
                           note="this acquirer's policy is prose, so it wants the "
                                "business described in words as well as a code"),
                    Column("Instruments", Source.DERIVED, M, key="instrument_set",
                           domain="instrument_set"),
                    Column("Settlement Cycle", Source.SALES_OWNED, M, key="settlement_cycle", domain="settlement"),
                    Column("Remarks", Source.OPS_INPUT, O, key="remark"),
                ),
            ),
        ),
        domains={d.name: d for d in (ENTITY_TYPE, SETTLEMENT, INSTRUMENT_SET)},
        date_format="%d-%m-%Y",
        notes="Policy is prose, so the file carries a written category description.",
    ),
}


# Which of our MCC super categories maps to which industry wording. Kept here
# rather than in the catalogue because it is the *form's* vocabulary, not ours,
# and a bank changing its dropdown should not touch the category tree.
INDUSTRY_BY_SUPER_CATEGORY: dict[str, str] = {
    "Retail and Shopping": "Retail",
    "Food and Beverages": "Food & Beverage",
    "Travel": "Travel & Hospitality",
    "Healthcare and Wellness": "Healthcare",
    "Financial Services": "BFSI",
    "Entertainment": "Entertainment",
    "Service and Utilities": "Services",
    "Wholesale and Manufacturing": "Wholesale & Manufacturing",
    "Agriculture": "Agriculture",
}
