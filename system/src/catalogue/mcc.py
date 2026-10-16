"""
The MCC catalogue: what a merchant sells, and how risky that is.

A payment aggregator does not keep a flat list of MCCs. It keeps a tree:
super category, category, sub category, with a risk rating against each code
and, for a few, a restriction on specific payment instruments rather than the
whole code.

Three things about this shape drive the design, and all three are easy to get
wrong:

1. **Risk is not binary.** It runs across five levels, and the two middle ones
   matter most. "Restricted with licence" means the merchant can be onboarded
   if it produces a regulator's licence, which is a document request rather
   than a refusal. "Restricted" means no. Collapsing these into
   allowed/blocked turns a solvable document gap into a lost merchant.

2. **Some restrictions are per instrument, not per MCC.** A code can be fine
   on a debit card and blocked on credit, overdraft and credit line on UPI.
   The eligible answer therefore depends on what the merchant asked for.

3. **The catalogue changes.** Network and regulator circulars add codes and
   move existing ones between risk levels. So entries carry a review note, and
   the change log is part of the model rather than an afterthought.

Every code, description and rating here is invented for this project. The
shape follows ordinary Indian acquiring practice; the content is not any
organisation's list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Risk(str, Enum):
    """How an acquirer's risk team treats a category.

    Ordered from freely acceptable to refused. RESTRICTED_WITH_LICENCE sits
    deliberately between MEDIUM and HIGH: it is not a refusal, it is a
    condition, and treating it as a refusal loses merchants who only needed to
    send a licence.
    """

    LOW = "low"
    MEDIUM = "medium"
    RESTRICTED_WITH_LICENCE = "restricted_with_licence"
    HIGH = "high"
    RESTRICTED = "restricted"

    @property
    def needs_licence(self) -> bool:
        return self is Risk.RESTRICTED_WITH_LICENCE

    @property
    def refused_outright(self) -> bool:
        return self is Risk.RESTRICTED


class Instrument(str, Enum):
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    CREDIT_LINE_UPI = "credit_line_upi"
    OVERDRAFT = "overdraft"
    EMI = "emi"


@dataclass(frozen=True)
class MccEntry:
    code: str
    super_category: str
    category: str
    sub_category: str
    description: str
    risk: Risk
    # Instruments this code may NOT be used with, whatever the acquirer says.
    # A network or regulator rule, not a commercial policy.
    instrument_restrictions: tuple[Instrument, ...] = ()
    # Licence a merchant must produce when risk is RESTRICTED_WITH_LICENCE.
    licence_required: str = ""
    note: str = ""


@dataclass
class CatalogueChange:
    """A row of the change log. Kept because the catalogue is a living
    document: a code's risk rating moving is the kind of change that silently
    invalidates decisions made a month earlier."""

    date: str
    code: str
    was: str
    now: str
    reason: str


# Invented catalogue. Codes are fictional four-digit values chosen to avoid
# collision with the real published MCC ranges where practical; descriptions
# and ratings are this project's own.
_ENTRIES: list[MccEntry] = [
    # --- Retail and shopping -----------------------------------------
    MccEntry("7101", "Retail and Shopping", "Apparel", "Family clothing",
             "General clothing and accessories retail", Risk.LOW),
    MccEntry("7104", "Retail and Shopping", "Apparel", "Ethnic wear",
             "Traditional and occasion wear", Risk.LOW),
    MccEntry("7112", "Retail and Shopping", "Home", "Furnishings",
             "Soft furnishing, decor and homeware", Risk.LOW),
    MccEntry("7120", "Retail and Shopping", "Electronics", "Consumer electronics",
             "Phones, audio, small appliances", Risk.MEDIUM,
             note="Higher ticket sizes draw more chargebacks"),
    MccEntry("7134", "Retail and Shopping", "Speciality", "Jewellery and precious metals",
             "Gold, silver and gemstone retail", Risk.HIGH,
             note="High value, high dispute, frequent laundering vector"),
    MccEntry("7140", "Retail and Shopping", "Speciality", "Second-hand goods",
             "Resale and refurbished goods marketplaces", Risk.HIGH),
    MccEntry("7155", "Retail and Shopping", "Books and media", "Book shops",
             "Books, stationery and periodicals", Risk.LOW),

    # --- Food and beverages ------------------------------------------
    MccEntry("7210", "Food and Beverages", "Restaurants", "Restaurants and cafes",
             "Dine-in and takeaway food service", Risk.LOW),
    MccEntry("7216", "Food and Beverages", "Grocery", "Speciality grocery",
             "Packaged and fresh grocery retail", Risk.LOW),
    MccEntry("7224", "Food and Beverages", "Alcohol", "Packaged liquor",
             "Beer, wine and spirits retail", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="State excise licence",
             note="State-by-state; several states prohibit online sale entirely"),
    MccEntry("7229", "Food and Beverages", "Tobacco", "Tobacco products",
             "Cigarettes and tobacco retail", Risk.RESTRICTED),

    # --- Travel -------------------------------------------------------
    MccEntry("7301", "Travel", "Agencies", "Travel agencies and tour operators",
             "Packaged travel, ticketing and tours", Risk.MEDIUM,
             note="Long fulfilment window, so refund exposure is high"),
    MccEntry("7308", "Travel", "Transport", "Airlines",
             "Scheduled passenger air transport", Risk.MEDIUM),
    MccEntry("7315", "Travel", "Accommodation", "Hotels and lodging",
             "Hotels, homestays and serviced apartments", Risk.LOW),
    MccEntry("7322", "Travel", "Transport", "Bus and rail ticketing",
             "Surface transport ticketing", Risk.LOW),

    # --- Healthcare and wellness --------------------------------------
    MccEntry("7402", "Healthcare and Wellness", "Clinical", "Hospitals and clinics",
             "Inpatient and outpatient medical services", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="Clinical establishment registration"),
    MccEntry("7407", "Healthcare and Wellness", "Pharmacy", "Pharmacy and chemists",
             "Prescription and OTC medicine retail", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="Drug licence (Form 20/21)"),
    MccEntry("7414", "Healthcare and Wellness", "Veterinary", "Veterinary services",
             "Animal clinics and veterinary practice", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="Veterinary council registration"),
    MccEntry("7420", "Healthcare and Wellness", "Personal care", "Salons and spas",
             "Hair, beauty and grooming services", Risk.LOW),
    MccEntry("7428", "Healthcare and Wellness", "Supplements", "Nutraceuticals",
             "Dietary supplements and nutrition products", Risk.MEDIUM,
             note="Claims on the site are what draws the scrutiny"),

    # --- Financial services -------------------------------------------
    MccEntry("7501", "Financial Services", "Lending", "Non-banking lenders",
             "Consumer and SME lending", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="RBI NBFC certificate of registration",
             instrument_restrictions=(Instrument.CREDIT_CARD, Instrument.CREDIT_LINE_UPI,
                                      Instrument.OVERDRAFT),
             note="Credit instruments barred: credit repaying credit"),
    MccEntry("7508", "Financial Services", "Insurance", "Insurance premium collection",
             "Life, general and health premium collection", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="IRDAI registration or corporate agent licence"),
    MccEntry("7515", "Financial Services", "Investments", "Securities and brokerage",
             "Equity, derivatives and mutual fund platforms", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="SEBI registration",
             instrument_restrictions=(Instrument.CREDIT_CARD, Instrument.CREDIT_LINE_UPI,
                                      Instrument.OVERDRAFT)),
    MccEntry("7521", "Financial Services", "Transfers", "Money transfer and quasi-cash",
             "Remittance, wallet top-up and quasi-cash", Risk.RESTRICTED,
             note="Classic laundering route; refused across the board"),
    MccEntry("7527", "Financial Services", "Crypto", "Virtual digital assets",
             "Crypto and digital asset exchanges", Risk.RESTRICTED),

    # --- Entertainment -------------------------------------------------
    MccEntry("7602", "Entertainment", "Media", "Streaming and subscriptions",
             "Video, audio and content subscriptions", Risk.LOW),
    MccEntry("7609", "Entertainment", "Events", "Ticketing and events",
             "Event, cinema and venue ticketing", Risk.MEDIUM),
    MccEntry("7616", "Entertainment", "Gaming", "Real money gaming",
             "Skill gaming with stakes", Risk.RESTRICTED,
             note="State-by-state legality; the acquirers that allow it treat it "
                  "as a separate commercial agreement, not a standard MID"),
    MccEntry("7623", "Entertainment", "Gaming", "Casual gaming",
             "In-app purchases, no stakes", Risk.MEDIUM),
    MccEntry("7630", "Entertainment", "Adult", "Adult content and services",
             "Adult entertainment", Risk.RESTRICTED),

    # --- Services and utilities ----------------------------------------
    MccEntry("7701", "Service and Utilities", "Software", "Software and SaaS",
             "Licensed software and hosted services", Risk.LOW),
    MccEntry("7708", "Service and Utilities", "Education", "Educational services",
             "Schools, colleges and tutoring", Risk.LOW),
    MccEntry("7712", "Service and Utilities", "Education", "Skills and test prep",
             "Vocational training and exam preparation", Risk.MEDIUM,
             note="Prepaid long-duration courses carry refund risk"),
    MccEntry("7719", "Service and Utilities", "Utilities", "Utility bill collection",
             "Electricity, water, gas and municipal dues", Risk.LOW),
    MccEntry("7726", "Service and Utilities", "Property", "Rent collection",
             "Residential and commercial rent", Risk.HIGH,
             instrument_restrictions=(Instrument.CREDIT_CARD, Instrument.CREDIT_LINE_UPI),
             note="The business is acceptable; paying rent on credit is the problem, "
                  "because it is a known cash-out route. So this is a high-risk "
                  "category with credit instruments barred, not a refused one: a "
                  "merchant collecting rent on debit and UPI can still go live"),
    MccEntry("7733", "Service and Utilities", "Professional", "Consulting and agencies",
             "Professional and marketing services", Risk.MEDIUM,
             note="Vague service descriptions are the usual rejection reason"),
    MccEntry("7740", "Service and Utilities", "Logistics", "Courier and delivery",
             "Parcel, courier and last-mile delivery", Risk.LOW),
    MccEntry("7747", "Service and Utilities", "Charity", "Donations and charity",
             "Charitable and religious donations", Risk.RESTRICTED_WITH_LICENCE,
             licence_required="12A/80G registration or FCRA where applicable"),

    # --- Agriculture and wholesale --------------------------------------
    MccEntry("7801", "Agriculture", "Inputs", "Fertiliser and agri-chemicals",
             "Fertiliser, pesticide and seed retail", Risk.MEDIUM),
    MccEntry("7808", "Agriculture", "Services", "Agricultural co-operatives",
             "Farm management and co-operative services", Risk.LOW),
    MccEntry("7815", "Wholesale and Manufacturing", "Distribution", "B2B distribution",
             "Wholesale distribution and manufacturing supply", Risk.MEDIUM),
    MccEntry("7822", "Wholesale and Manufacturing", "Industrial", "Industrial equipment",
             "Machinery, tools and industrial supply", Risk.LOW),
]

CATALOGUE: dict[str, MccEntry] = {e.code: e for e in _ENTRIES}


CHANGE_LOG: list[CatalogueChange] = [
    CatalogueChange("2026-04-03", "7527", "high", "restricted",
                    "Regulator circular on virtual digital assets"),
    CatalogueChange("2026-05-18", "7726", "medium", "restricted",
                    "Rent-on-credit-card identified as a cash-out route"),
    CatalogueChange("2026-06-02", "7428", "low", "medium",
                    "Health claim complaints across the category"),
    CatalogueChange("2026-08-11", "7616", "restricted_with_licence", "restricted",
                    "Withdrawn from standard onboarding pending legal review"),
]


@dataclass
class MccResolution:
    """What the catalogue says about one code, for one set of instruments."""

    entry: MccEntry
    instruments_requested: tuple[Instrument, ...]
    blocked_instruments: tuple[Instrument, ...] = field(default_factory=tuple)

    @property
    def allowed_instruments(self) -> tuple[Instrument, ...]:
        return tuple(i for i in self.instruments_requested if i not in self.blocked_instruments)

    @property
    def refused(self) -> bool:
        """Refused outright by the catalogue, before any acquirer policy."""
        return self.entry.risk.refused_outright

    @property
    def fully_blocked_on_instruments(self) -> bool:
        """Every instrument the merchant asked for is barred for this code.
        Distinct from a category refusal: the business is acceptable, the
        payment method is not, and the merchant may still go live on
        something else."""
        return bool(self.instruments_requested) and not self.allowed_instruments

    def explain(self) -> str:
        e = self.entry
        bits = [f"MCC {e.code} ({e.sub_category}), risk {e.risk.value}"]
        if e.risk.needs_licence and e.licence_required:
            bits.append(f"requires {e.licence_required}")
        if self.blocked_instruments:
            names = ", ".join(i.value.replace("_", " ") for i in self.blocked_instruments)
            bits.append(f"not permitted on {names}")
        return "; ".join(bits)


def lookup(code: str) -> MccEntry | None:
    return CATALOGUE.get(str(code).strip())


def resolve(code: str, instruments: tuple[Instrument, ...] = ()) -> MccResolution | None:
    """What this code means for a merchant asking for these instruments."""
    entry = lookup(code)
    if entry is None:
        return None
    blocked = tuple(i for i in instruments if i in entry.instrument_restrictions)
    return MccResolution(entry=entry, instruments_requested=instruments,
                         blocked_instruments=blocked)


def by_risk(risk: Risk) -> list[MccEntry]:
    return [e for e in CATALOGUE.values() if e.risk is risk]


def super_categories() -> dict[str, int]:
    out: dict[str, int] = {}
    for e in CATALOGUE.values():
        out[e.super_category] = out.get(e.super_category, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
