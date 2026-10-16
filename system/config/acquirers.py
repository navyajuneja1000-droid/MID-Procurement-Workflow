"""
Invented acquirers. Six rails, deliberately differing on SLA, field
requirements and holiday calendar, so the engine is exercised properly.

Two of them (NORTHWAY_CYBS and NORTHWAY_FSS) are the same bank on two
processing platforms. They are modelled as separate acquirers because they
are separate applications with separate configuration and separate clocks.

No real bank names anywhere. Swap SLAs and field lists for real ones only in
a private working copy, never in anything published.
"""

from src.model import Acquirer

# Fields every acquirer wants
BASE_FIELDS = [
    "entity_legal_name",
    "company_pan",
    "registered_address",
    "website",
    "mcc",
]

ACQUIRERS: dict[str, Acquirer] = {
    "ALDRIDGE": Acquirer(
        code="ALDRIDGE",
        name="Aldridge Bank",
        status_sla_days=5,
        mid_sla_days=3,
        required_fields=BASE_FIELDS
        + ["signatory_pan", "company_gst", "registration_number"],
    ),
    "BRINDLE": Acquirer(
        code="BRINDLE",
        name="Brindle Financial",
        status_sla_days=7,
        mid_sla_days=4,
        required_fields=BASE_FIELDS + ["signatory_pan", "signatory_address"],
    ),
    "CASTERLY": Acquirer(
        code="CASTERLY",
        name="Casterly Bank",
        status_sla_days=4,
        mid_sla_days=2,
        required_fields=BASE_FIELDS + ["company_gst"],
    ),
    "NORTHWAY_CYBS": Acquirer(
        code="NORTHWAY_CYBS",
        name="Northway Bank (CYBS platform)",
        status_sla_days=6,
        mid_sla_days=5,
        required_fields=BASE_FIELDS
        + ["signatory_pan", "signatory_address", "company_gst"],
    ),
    "NORTHWAY_FSS": Acquirer(
        code="NORTHWAY_FSS",
        name="Northway Bank (FSS platform)",
        status_sla_days=6,
        mid_sla_days=5,
        required_fields=BASE_FIELDS + ["signatory_pan", "company_gst"],
    ),
    "VERRAN": Acquirer(
        code="VERRAN",
        name="Verran Bank",
        status_sla_days=8,
        mid_sla_days=4,
        required_fields=BASE_FIELDS
        + ["signatory_pan", "signatory_address", "registration_number"],
        holiday_calendar="verran",  # observes a few extra holidays
    ),
}

# Holiday calendars. "default" covers the shared list; a named calendar
# overrides it for one acquirer.
HOLIDAYS: dict[str, list[str]] = {
    "default": [
        "2026-01-26",  # Republic Day
        "2026-03-04",  # Holi
        "2026-08-15",  # Independence Day
        "2026-10-02",  # Gandhi Jayanti
        "2026-11-09",  # Diwali
        "2026-12-25",  # Christmas
    ],
    "verran": [
        "2026-01-26",
        "2026-03-04",
        "2026-04-01",  # bank closing of accounts
        "2026-08-15",
        "2026-10-02",
        "2026-11-09",
        "2026-11-10",
        "2026-12-25",
    ],
}
