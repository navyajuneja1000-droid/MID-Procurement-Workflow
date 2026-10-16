"""
Invented names and reference data for the synthetic dataset.

Nothing here comes from a real company. Merchant names are built from
word pools so they read like real Indian entity names without being any.
"""

from __future__ import annotations

from .catalogue.mcc import CATALOGUE, Risk

PREFIXES = [
    "Harrowgate", "Verdant", "Silverline", "Kalpavriksha", "Northfield",
    "Ambervale", "Trimurti", "Clearwater", "Ashvattha", "Brightmoor",
    "Suvarna", "Ironwood", "Meghdoot", "Larkspur", "Chandrika",
    "Westbrook", "Nilgiri", "Grainstone", "Prabhat", "Redstone",
    "Vasundhara", "Oakhurst", "Shrishti", "Falconridge", "Anantara",
    "Blueharbour", "Devaki", "Stonebridge", "Rukmini", "Greenfell",
    "Tattvam", "Copperleaf", "Sahasra", "Windermere", "Nirvaan",
    "Ellsworth", "Pushpak", "Fernhill", "Samarth", "Thornbury",
]

MIDDLES = [
    "Trading", "Retail", "Ventures", "Commerce", "Enterprises",
    "Solutions", "Industries", "Services", "Traders", "Exports",
    "Logistics", "Technologies", "Lifestyle", "Mercantile", "Agencies",
]

SUFFIXES = [
    "Private Limited",
    "Pvt Ltd",
    "LLP",
    "Limited",
    "India Private Limited",
]

# Suffix variants a bank might type instead. Used to make reply matching
# realistically messy for the parser in a later phase.
SUFFIX_VARIANTS = {
    "Private Limited": ["Pvt Ltd", "Pvt. Ltd.", "Private Ltd", "P Ltd"],
    "Pvt Ltd": ["Private Limited", "Pvt. Ltd.", "Pvt Ltd."],
    "LLP": ["L.L.P.", "LLP.", "Llp"],
    "Limited": ["Ltd", "Ltd.", "Limtied"],
    "India Private Limited": ["India Pvt Ltd", "India Pvt. Ltd.", "(India) Private Limited"],
}

# The two MCC pools the generator draws from are derived from the catalogue
# rather than listed again here. Keeping a second list would let the dataset
# drift away from the policy module: a request could carry a code no acquirer
# has a view on, and every eligibility answer in the dataset would come back
# unresolved while looking fine. One source, so the two cannot disagree.
#
# Safe means a category that clears underwriting without argument. Restricted
# means one that needs a licence, is capped to certain instruments, or is
# refused outright: the kinds that produce real rejections and conditions.
SAFE_MCCS: dict[str, str] = {
    e.code: e.sub_category
    for e in CATALOGUE.values()
    if e.risk in (Risk.LOW, Risk.MEDIUM) and not e.instrument_restrictions
}

RESTRICTED_MCCS: dict[str, str] = {
    e.code: e.sub_category
    for e in CATALOGUE.values()
    if e.code not in SAFE_MCCS
}

STATES = [
    ("Karnataka", "29", "Bengaluru", "560001"),
    ("Maharashtra", "27", "Mumbai", "400001"),
    ("Delhi", "07", "New Delhi", "110001"),
    ("Tamil Nadu", "33", "Chennai", "600001"),
    ("Telangana", "36", "Hyderabad", "500001"),
    ("Gujarat", "24", "Ahmedabad", "380001"),
    ("West Bengal", "19", "Kolkata", "700001"),
    ("Rajasthan", "08", "Jaipur", "302001"),
    ("Uttar Pradesh", "09", "Noida", "201301"),
    ("Haryana", "06", "Gurugram", "122001"),
]

STREETS = [
    "Mill Road", "Residency Lane", "Sector 14", "Linking Road", "MG Road",
    "Church Street", "Park Avenue", "Industrial Estate", "Ring Road",
    "Commercial Complex", "Tech Park", "Station Road",
]

# People who sign on behalf of a merchant. First names only ever appear
# as an authorised signatory field, never as a merchant name.
SIGNATORY_FIRST = [
    "Aarav", "Diya", "Rohan", "Meera", "Kabir", "Ananya", "Vikram",
    "Ishita", "Arjun", "Nandini", "Siddharth", "Priya", "Rehan", "Tara",
]
SIGNATORY_LAST = [
    "Sharma", "Iyer", "Banerjee", "Patel", "Reddy", "Nair", "Chauhan",
    "Mehta", "Rao", "Kulkarni", "Das", "Joshi", "Menon", "Gupta",
]


# Invented settlement banks and IFSC prefixes. The submission form asks for
# settlement details, and a wrong IFSC is one of the few errors that survives
# every check and only surfaces when the first payout fails.
SETTLEMENT_BANKS = [
    "Aldridge Bank", "Brindle Financial", "Casterly Bank", "Northway Bank",
    "Verran Bank", "Thornfield Bank", "Marlowe Co-operative Bank",
]

IFSC_PREFIXES = ["ALDR", "BRND", "CSTL", "NRTH", "VRRN", "THNF", "MRLW"]

# The entity types the form's dropdown accepts, keyed by the suffix the legal
# name carries. The mapping matters because the form will not take the name's
# own wording: a company called "... Private Limited" has to be filed as
# "Private Limited", and an LLP as "Limited Liability Company", which is not
# what anyone would type unprompted.
ENTITY_TYPE_BY_SUFFIX: dict[str, str] = {
    "Private Limited": "Private Limited",
    "LLP": "Limited Liability Company",
    "Limited": "Public Limited",
    "Trust": "Trust",
    "Society": "Society",
}
